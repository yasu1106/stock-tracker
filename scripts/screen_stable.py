#!/usr/bin/env python3
"""
「1000円以下・52週で値動きが安定している日本株」を探すスクリーニングスクリーニング。
watchlist.json（自分の気になる銘柄）とは別枠。universe_jp225.json（日経225の構成銘柄）の中から、
条件に合う銘柄を screener.json に書き出す。

【重要】ここでの「安定」は、過去52週の値動きの幅と変動の荒さが小さいという意味であり、
今後も株価が下がらないことを保証するものではない。下落中の銘柄が一時的に値動きだけ
穏やかに見えることもあるため、長期線を大きく下回っている銘柄は候補から外している。

対象を日経225（225銘柄）に絞っているのは、無料のGitHub Actionsで現実的な時間で終わらせるため。
東証の全銘柄（約4000）を対象にしたい場合は universe_jp225.json を差し替えるか、
このスクリプトの UNIVERSE_FILE を変更する。
"""
import json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_prices import fetch_one, to_yahoo_symbol, avg, USER_AGENT  # noqa: E402

# ===== 設定（調整したい値はここに集約） =====
ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = ROOT / "universe_jp225.json"
OUTPUT_FILE = ROOT / "screener.json"
PRICE_MAX = 1000              # これ以下の株価だけを対象にする（円）
CHART_RANGE = "1y"
SLEEP_SEC = 1.0                # 銘柄ごとの待ち時間
MA_LONG = 75                   # 長期移動平均（日数）。下落トレンド判定に使う
DOWNTREND_FLOOR = 0.85         # 株価が長期線のこの割合を下回ったら「下落中」として除外
VOL_DAYS = 60                  # 値動きの荒さを測る日数
VOL_BEST = 15.0                # 年率換算の変動がこれ以下なら満点（％）
VOL_WORST = 45.0               # これ以上なら0点
RANGE_BEST_PCT = 20.0          # 52週高安の値幅(高値-安値)/中心値 がこれ以下なら満点（％）
RANGE_WORST_PCT = 60.0         # これ以上なら0点
STABILITY_WEIGHTS = {"low_vol": 55, "narrow_range": 45}  # 合計100
STABLE_MIN_SCORE = 60          # この点数以上を「安定候補」として表示する
RESULT_MAX = 30                # 出力する最大件数（点数が高い順）


def clamp01(x):
    return max(0.0, min(1.0, x))


def evaluate(closes, price):
    if not price or len(closes) < MA_LONG + 5:
        return None
    ma_l = avg(closes[-MA_LONG:])
    if price < ma_l * DOWNTREND_FLOOR:
        return None  # 下落トレンド中とみなし、対象から外す

    high = max(closes[-252:] if len(closes) >= 252 else closes)
    low = min(closes[-252:] if len(closes) >= 252 else closes)
    mid = (high + low) / 2 if (high + low) else price
    range_pct = (high - low) / mid * 100 if mid else 999

    recent = closes[-VOL_DAYS - 1:]
    rets = [recent[i] / recent[i - 1] - 1 for i in range(1, len(recent))]
    mean = sum(rets) / len(rets)
    vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * (252 ** 0.5) * 100

    vol_frac = clamp01((VOL_WORST - vol) / (VOL_WORST - VOL_BEST))
    range_frac = clamp01((RANGE_WORST_PCT - range_pct) / (RANGE_WORST_PCT - RANGE_BEST_PCT))
    score = round(STABILITY_WEIGHTS["low_vol"] * vol_frac + STABILITY_WEIGHTS["narrow_range"] * range_frac)

    return {
        "score": score,
        "volAnnualPct": round(vol, 1),
        "rangePct": round(range_pct, 1),
        "high52": round(high, 2),
        "low52": round(low, 2),
        "ma_long": round(ma_l, 2),
        "reasons": [
            {"text": f"値動きの荒さ（年率換算 {vol:.0f}%）", "pts": round(STABILITY_WEIGHTS['low_vol'] * vol_frac, 1), "max": STABILITY_WEIGHTS["low_vol"]},
            {"text": f"52週の値幅（中心値の{range_pct:.0f}%）", "pts": round(STABILITY_WEIGHTS['narrow_range'] * range_frac, 1), "max": STABILITY_WEIGHTS["narrow_range"]},
        ],
    }


def main():
    universe = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))["symbols"]
    results, errors, skipped_price, skipped_trend = [], [], 0, 0

    for item in universe:
        code, name = item["code"], item["name"]
        try:
            meta, closes = fetch_one(to_yahoo_symbol("JP", code))
            price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            if not price or price > PRICE_MAX:
                skipped_price += 1
                continue
            ev = evaluate(closes, price)
            if ev is None:
                skipped_trend += 1
                continue
            if ev["score"] >= STABLE_MIN_SCORE:
                results.append({"market": "JP", "code": code, "name": name, "price": round(price, 1), **ev})
        except Exception as e:
            errors.append(f"{code} {name}: {e}")
        time.sleep(SLEEP_SEC)

    results.sort(key=lambda r: r["score"], reverse=True)

    from datetime import datetime, timezone
    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "priceMax": PRICE_MAX,
        "universeCount": len(universe),
        "matchCount": len(results),
        "results": results[:RESULT_MAX],
        "errors": errors,
    }
    OUTPUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: 対象{len(universe)}銘柄中、{len(results)}銘柄が条件に合致（価格超過で除外{skipped_price} / 下落中で除外{skipped_trend} / エラー{len(errors)}）")
    if errors and not results and skipped_price == 0 and skipped_trend == 0:
        sys.exit(1)  # 全滅時のみ失敗扱い


if __name__ == "__main__":
    main()
