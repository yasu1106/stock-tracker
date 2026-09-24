#!/usr/bin/env python3
"""
株価取得スクリプト（GitHub Actionsから毎日実行される）
watchlist.json の銘柄を Yahoo Finance のチャートAPI（非公式・キー不要）から取得し、
prices.json に書き出す。

【注意】非公式APIのため、仕様変更やアクセス制限で急に動かなくなる可能性がある。
その場合は SOURCE_* の部分だけ差し替えれば他の処理は変えずに済むよう、取得部分を fetch_one() に分離してある。
"""
import json, sys, time, urllib.request, urllib.parse
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
            out["prices"][key] = build_entry(meta, closes)
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
