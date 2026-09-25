#!/usr/bin/env python3
"""
株価取得スクリプト（GitHub Actionsから毎日実行される）
watchlist.json の銘柄を Yahoo Finance のチャートAPI（非公式・キー不要）から取得し、
prices.json に書き出す。

【注意】非公式APIのため、仕様変更やアクセス制限で急に動かなくなる可能性がある。
その場合は SOURCE_* の部分だけ差し替えれば他の処理は変えずに済むよう、取得部分を fetch_one() に分離してある。
"""
import json, re, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ===== 設定（調整したい値はここに集約） =====
ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_FILE = ROOT / "watchlist.json"
OUTPUT_FILE = ROOT / "prices.json"
CHART_RANGE = "1y"            # 取得する期間（移動平均・52週高安の計算に使う）
CHART_INTERVAL = "1d"
MA_SHORT = 25                 # 短期移動平均（日数）
MA_LONG = 75                  # 長期移動平均（日数）
SLEEP_SEC = 1.0               # 銘柄ごとの待ち時間（連続アクセスで弾かれないため）
RETRY = 2                     # 失敗時のリトライ回数
TIMEOUT_SEC = 20
FX_SYMBOL = "USDJPY=X"        # ドル円レート
USER_AGENT = "Mozilla/5.0 (compatible; stock-tracker/1.0)"  # 未指定だと拒否されることがある
BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

# ===== スコアリング設定（100点満点。配点や基準はここだけ変えればOK） =====
# 【重要】これは「過去の株価の形」を点数にしただけで、将来の値上がりを保証・予測するものではない。
SCORE_WEIGHTS = {             # 合計が100になるようにする
    "ma_alignment": 25,       # 株価 > 短期線 > 長期線 と並んでいるか（上昇トレンドの形）
    "ma_slope": 15,           # 長期線が上向きか
    "near_high": 20,          # 52週高値にどれだけ近いか
    "momentum": 20,           # 半年間の値上がり率
    "stability": 20,          # 値動きの安定度（荒い銘柄は減点）
}
RATING_CANDIDATE = 70         # この点数以上 → 「候補」
RATING_WATCH = 40             # この点数以上 → 「様子見」、未満 → 「注意」
SLOPE_LOOKBACK = 20           # 長期線の傾きを見る日数（20営業日前と比較）
SLOPE_FULL_PCT = 3.0          # 長期線が上記期間で +3% 上がれば満点
NEAR_HIGH_FLOOR = 0.70        # 高値の70%以下なら0点、100%なら満点
MOMENTUM_DAYS = 126           # 「半年」の営業日数
MOMENTUM_FULL_PCT = 30.0      # 半年で +30% 以上上がれば満点
VOL_DAYS = 60                 # 値動きの荒さを測る日数
VOL_BEST = 20.0               # 年率換算の変動が20%以下なら満点
VOL_WORST = 60.0              # 60%以上なら0点
# 警告（点数とは別に、気をつけたい点として表示する）
WARN_DROP_DAYS = 20           # 直近何営業日で
WARN_DROP_PCT = -10.0         # 何%以上下がったら警告
WARN_OVERHEAT_RATIO = 1.3     # 株価が長期線の1.3倍を超えたら「過熱」警告
WARN_VOL = 50.0               # 年率変動がこれ以上なら「値動きが荒い」警告

# ===== ニュース確認設定（②現在のニュースの情勢から落ちそうな銘柄を除外） =====
# 【重要】見出しに含まれる単語だけで機械的に拾う簡易チェックであり、内容を理解して判定しているわけではない。
# 「除外」ではなく「要チェック」の警告として扱い、最終判断は必ず自分でニュース本文を確認すること。
NEWS_ENABLED = True                   # ニュース確認そのもののON/OFF
NEWS_ITEMS_PER_SYMBOL = 8             # 銘柄ごとに見出しをいくつ確認するか
NEWS_TIMEOUT_SEC = 15
NEWS_SLEEP_SEC = 1.0                  # 銘柄ごとの待ち時間
# 見出しにこの単語が入っていたら「悪材料の疑い」として警告する（増減は自由。誤検知はあり得る前提で）
NEWS_NEGATIVE_KEYWORDS = [
    "下方修正", "赤字", "減益", "減収", "最終赤字", "特別損失", "減配", "無配",
    "不祥事", "粉飾", "捜査", "逮捕", "上場廃止", "監理銘柄", "行政処分",
    "リコール", "回収", "事故", "火災", "訴訟", "提訴", "賠償",
    "撤退", "希望退職", "リストラ", "工場閉鎖", "生産停止", "供給停止",
    "格下げ", "自己資本比率", "債務超過", "経営再建", "私的整理",
]
# 見出しにこの単語が入っていたら「好材料」として参考表示する（除外判定には使わない）
NEWS_POSITIVE_KEYWORDS = ["上方修正", "最高益", "増配", "自社株買い", "黒字転換", "提携", "受賞"]


def fetch_news(query, max_items=NEWS_ITEMS_PER_SYMBOL):
    """Googleニュース検索のRSSから見出しを取得する（非公式の使い方。取れない場合は空リストを返す）。"""
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=NEWS_TIMEOUT_SEC) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall("./channel/item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "pubDate": pub})
    return items


def analyze_news(items):
    hits, positives = [], []
    for it in items:
        for kw in NEWS_NEGATIVE_KEYWORDS:
            if kw in it["title"]:
                hits.append({"keyword": kw, "title": it["title"], "link": it["link"]})
                break
        else:
            for kw in NEWS_POSITIVE_KEYWORDS:
                if kw in it["title"]:
                    positives.append({"keyword": kw, "title": it["title"], "link": it["link"]})
                    break
    return {
        "checked": True,
        "count": len(items),
        "warnings": hits,
        "positives": positives,
        "riskFlag": len(hits) > 0,   # True の銘柄は画面で「ニュース要チェック」表示にする
    }


def to_yahoo_symbol(market, code):
    # 日本株は末尾に .T を付ける（例: 7203 -> 7203.T）。米国株はそのまま。
    return f"{code}.T" if market == "JP" else code


def fetch_one(symbol):
    url = f"{BASE_URL}{urllib.parse.quote(symbol)}?range={CHART_RANGE}&interval={CHART_INTERVAL}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
                data = json.load(r)
            res = data["chart"]["result"][0]
            meta = res["meta"]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            return meta, closes
        except Exception as e:  # ネットワーク・形式エラー全般
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_err}")


def avg(xs):
    return sum(xs) / len(xs) if xs else None


def clamp01(x):
    return max(0.0, min(1.0, x))


def compute_score(closes, price):
    """終値の配列と現在値から、100点満点のスコアと理由・警告を作る（株価の形だけを見る）。"""
    need = MA_LONG + SLOPE_LOOKBACK
    if not price or len(closes) < need:
        return {"score": None, "rating": "unknown", "reasons": [],
                "warnings": ["データ不足（上場から日が浅い等）のため点数を付けられません"]}

    ma_s = avg(closes[-MA_SHORT:])
    ma_l = avg(closes[-MA_LONG:])
    ma_l_past = avg(closes[-MA_LONG - SLOPE_LOOKBACK:-SLOPE_LOOKBACK])
    reasons, total = [], 0.0

    def add(key, frac, text):
        nonlocal total
        pts = SCORE_WEIGHTS[key] * clamp01(frac)
        total += pts
        reasons.append({"text": text, "pts": round(pts, 1), "max": SCORE_WEIGHTS[key]})

    # 1) 移動平均の並び: 株価>短期線(1/5) + 短期線>長期線(2/5) + 株価>長期線(2/5)
    frac = (0.2 if price > ma_s else 0) + (0.4 if ma_s > ma_l else 0) + (0.4 if price > ma_l else 0)
    add("ma_alignment", frac,
        f"株価と{MA_SHORT}日線・{MA_LONG}日線の並び（株価{'>' if price > ma_s else '<'}{MA_SHORT}日線、"
        f"{MA_SHORT}日線{'>' if ma_s > ma_l else '<'}{MA_LONG}日線）")

    # 2) 長期線の傾き
    slope = (ma_l / ma_l_past - 1) * 100
    add("ma_slope", slope / SLOPE_FULL_PCT, f"{MA_LONG}日線の傾き（{SLOPE_LOOKBACK}日で{slope:+.1f}%）")

    # 3) 52週高値との近さ
    high = max(max(closes), price)
    ratio = price / high
    add("near_high", (ratio - NEAR_HIGH_FLOOR) / (1 - NEAR_HIGH_FLOOR), f"52週高値の{ratio * 100:.0f}%の位置")

    # 4) 半年の値上がり率
    base = closes[-MOMENTUM_DAYS - 1] if len(closes) > MOMENTUM_DAYS else closes[0]
    ret = (price / base - 1) * 100
    add("momentum", ret / MOMENTUM_FULL_PCT, f"半年の値上がり率 {ret:+.1f}%")

    # 5) 値動きの安定度（日次騰落率の標準偏差を年率換算）
    recent = closes[-VOL_DAYS - 1:]
    rets = [recent[i] / recent[i - 1] - 1 for i in range(1, len(recent))]
    mean = sum(rets) / len(rets)
    vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * (252 ** 0.5) * 100
    add("stability", (VOL_WORST - vol) / (VOL_WORST - VOL_BEST), f"値動きの荒さ（年率換算 {vol:.0f}%）")

    warnings = []
    if price < ma_l:
        warnings.append(f"株価が{MA_LONG}日線を下回っています")
    if len(closes) > WARN_DROP_DAYS:
        drop = (price / closes[-WARN_DROP_DAYS - 1] - 1) * 100
        if drop <= WARN_DROP_PCT:
            warnings.append(f"直近{WARN_DROP_DAYS}営業日で {drop:.1f}% 下落しています")
    if price > ma_l * WARN_OVERHEAT_RATIO:
        warnings.append(f"{MA_LONG}日線から離れすぎ（過熱の可能性）")
    if vol >= WARN_VOL:
        warnings.append("値動きが荒い銘柄です")

    score = round(total)
    rating = "candidate" if score >= RATING_CANDIDATE else "watch" if score >= RATING_WATCH else "caution"
    return {"score": score, "rating": rating, "reasons": reasons, "warnings": warnings}


def build_entry(meta, closes):
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    # 前日終値: 終値配列の末尾が最新日の値なので、その1つ前を使う。
    # （meta.chartPreviousClose は「期間の開始直前の終値」で前日ではないので使わない）
    prev = closes[-2] if len(closes) >= 2 else None
    ma_s = avg(closes[-MA_SHORT:]) if len(closes) >= MA_SHORT else None
    ma_l = avg(closes[-MA_LONG:]) if len(closes) >= MA_LONG else None
    return {
        "price": price,
        "prevClose": prev,
        "changePct": round((price - prev) / prev * 100, 2) if price and prev else None,
        "currency": meta.get("currency"),
        "ma_short": round(ma_s, 2) if ma_s else None,
        "ma_long": round(ma_l, 2) if ma_l else None,
        "high52": round(max(closes), 2) if closes else None,
        "low52": round(min(closes), 2) if closes else None,
        "asOf": datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc).isoformat()
                if meta.get("regularMarketTime") else None,
    }


def main():
    wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    # 前回の結果を引き継ぐ（今回取れなかった銘柄が消えないように）
    old = {}
    if OUTPUT_FILE.exists():
        try:
            old = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    out = {"updated": datetime.now(timezone.utc).isoformat(),
           "fx": old.get("fx", {}), "prices": old.get("prices", {}), "errors": []}

    try:
        meta, _ = fetch_one(FX_SYMBOL)
        out["fx"]["USDJPY"] = meta["regularMarketPrice"]
    except Exception as e:
        out["errors"].append(str(e))

    for item in wl["symbols"]:
        market, code = item["market"], str(item["code"]).upper()
        key = f"{market}:{code}"
        try:
            meta, closes = fetch_one(to_yahoo_symbol(market, code))
            entry = build_entry(meta, closes)
            entry["name"] = item.get("name", "")                        # 画面に出す銘柄名（任意）
            entry["yuutaiWarning"] = bool(item.get("yuutaiWarning"))    # 優待改悪が心配な銘柄は候補から除外して表示
            entry["analysis"] = compute_score(closes, entry["price"])   # 100点満点のスコアと理由

            # ニュース見出しの簡易チェック（会社名が無い銘柄はニュース検索できないのでスキップ）
            name = item.get("name")
            if NEWS_ENABLED and name:
                try:
                    news_items = fetch_news(name)
                    entry["news"] = analyze_news(news_items)
                except Exception as e:
                    entry["news"] = {"checked": False, "error": str(e)}
                time.sleep(NEWS_SLEEP_SEC)
            else:
                entry["news"] = {"checked": False, "error": "銘柄名(name)が未設定のためニュース検索していません"}

            out["prices"][key] = entry
        except Exception as e:
            out["errors"].append(str(e))
        time.sleep(SLEEP_SEC)

    # watchlistから消した銘柄は出力からも消す
    wanted = {f"{i['market']}:{str(i['code']).upper()}" for i in wl["symbols"]}
    out["prices"] = {k: v for k, v in out["prices"].items() if k in wanted}

    OUTPUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {len(out['prices'])}銘柄 / エラー {len(out['errors'])}件")
    for e in out["errors"]:
        print("  ERR", e)
    # 全滅のときだけ失敗扱いにして、Actions側で気づけるようにする
    if out["errors"] and not out["prices"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
