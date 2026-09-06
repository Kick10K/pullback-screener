#!/usr/bin/env python3
"""Build the live dashboard payload from the KR pullback screener engine.

Reuses every rule from kr_pullback_screener.py (universe, trend filter,
retracement definition, scoring) unchanged, and additionally packages a
per-symbol chart series (OHLCV + MA5/20/60/120 + MACD) for the dashboard's
candlestick panel. Writes two local files:
  outputs/live/kr_dashboard_summary.json  -> {payload: {...}}  (no bars)
  outputs/live/kr_dashboard_bars/<code>.json -> {code, as_of, bars}

Does not touch the Artifact DB itself -- that's a separate step (see
scripts/push_dashboard_db.py) so this can be re-run and inspected locally
before anything goes live.
"""

from __future__ import annotations

import importlib.util
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "outputs" / "live"
BARS_DIR = OUT_DIR / "kr_dashboard_bars"

spec = importlib.util.spec_from_file_location("krs", ROOT / "scripts" / "kr_pullback_screener.py")
krs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(krs)

CHART_BARS = 130  # enough trailing sessions that MA120 is populated for the whole visible window


def row_for_dashboard(feat: pd.DataFrame, meta: dict, date: pd.Timestamp) -> dict | None:
    if date not in feat.index:
        return None
    j = feat.index.get_loc(date)
    row = feat.iloc[j]
    avg_val20 = feat["trading_value_eok"].iloc[max(0, j - 19):j + 1].mean()
    liquidity_ok = avg_val20 >= krs.CONFIG["MIN_AVG_TRADING_VALUE_EOK"]
    band_ok = (not pd.isna(row["retrace_ratio"])) and (krs.CONFIG["RETRACE_MIN"] <= row["retrace_ratio"] <= krs.CONFIG["RETRACE_MAX"])
    is_uptrend = bool(row["is_uptrend"])

    # INV-4: 고점 경과일 조건. screen_on_date()와 같은 기준을 써야 CSV와 대시보드 판정이 갈라지지 않는다.
    dsh = row["days_since_high20"]
    dsh_int = None if pd.isna(dsh) else int(dsh)
    days_ok = dsh_int is not None and (
        krs.CONFIG["DAYS_SINCE_HIGH_MIN"] <= dsh_int <= krs.CONFIG["DAYS_SINCE_HIGH_MAX"]
    )
    # INV-7: 시계열 구조 조건. screen_on_date()와 같은 함수를 호출해 판정이 갈라지지 않게 한다.
    struct = krs.structure_verdict(row)
    is_candidate = bool(is_uptrend and liquidity_ok and band_ok and days_ok and struct["ok"])

    reasons = []
    if not is_uptrend:
        reasons.append("정배열 아님")
    if not liquidity_ok:
        reasons.append(f"거래대금 부족 ({avg_val20:.0f}억)")
    if not days_ok:
        reasons.append(f"고점경과일 {dsh_int}일 (허용 {krs.CONFIG['DAYS_SINCE_HIGH_MIN']}~{krs.CONFIG['DAYS_SINCE_HIGH_MAX']}일)")
    if not band_ok:
        reasons.append("되돌림비율 범위 밖")
    reasons.extend(struct["reasons"])

    # 경과일 필터에 걸린 종목은 점수 계산에 도달하지 않는다(§4).
    sc = krs.score_row(feat, j) if (is_uptrend and days_ok and not pd.isna(row["retrace_ratio"])) else None

    def nn(v):
        return None if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v

    h20, l20 = row["h20"], row["l20"]
    depth_pct = (h20 - l20) / h20 * 100 if not pd.isna(h20) and h20 > 0 and not pd.isna(l20) else np.nan
    peak_date = krs.peak_date_of(feat, j)

    return dict(
        code=meta["code"], name=meta["name"], market=meta["market"],
        date=date.strftime("%Y-%m-%d"),
        close=nn(float(row["close"])), mcap_eok=nn(float(meta.get("mcap_eok", np.nan))),
        is_uptrend=is_uptrend,
        is_candidate=is_candidate,
        passes_days_since_high=bool(days_ok),
        passes_structure=bool(struct["ok"]),
        exclude_reason="; ".join(reasons) if reasons else "",
        retrace_ratio=nn(round(float(row["retrace_ratio"]), 3)) if not pd.isna(row["retrace_ratio"]) else None,
        retrace_ratio_legacy=nn(round(float(row["retrace_ratio_legacy"]), 3)) if not pd.isna(row["retrace_ratio_legacy"]) else None,
        days_since_pullback_low=None if pd.isna(row["days_since_pullback_low"]) else int(row["days_since_pullback_low"]),
        bounce_from_low=nn(round(float(row["bounce_from_low"]) * 100, 2)) if not pd.isna(row["bounce_from_low"]) else None,
        dd_from_high=nn(round(float(row["dd_from_high"]) * 100, 2)) if not pd.isna(row["dd_from_high"]) else None,
        range_pct=nn(round(float(row["range_pct"]) * 100, 2)) if not pd.isna(row["range_pct"]) else None,
        l_leg=nn(round(float(row["l_leg"]), 2)) if not pd.isna(row["l_leg"]) else None,
        l_pull=nn(round(float(row["l_pull"]), 2)) if not pd.isna(row["l_pull"]) else None,
        disparity_vs_ma20=nn(round(float(row["disparity"]), 3)) if not pd.isna(row["disparity"]) else None,
        ma5=nn(round(float(row["ma5"]), 2)) if not pd.isna(row["ma5"]) else None,
        ma20=nn(round(float(row["ma20"]), 2)) if not pd.isna(row["ma20"]) else None,
        ma60=nn(round(float(row["ma60"]), 2)) if not pd.isna(row["ma60"]) else None,
        ma120=nn(round(float(row["ma120"]), 2)) if not pd.isna(row["ma120"]) else None,
        h20=nn(round(float(h20), 2)) if not pd.isna(h20) else None,
        l20=nn(round(float(l20), 2)) if not pd.isna(l20) else None,
        depth_pct=nn(round(float(depth_pct), 2)) if not pd.isna(depth_pct) else None,
        days_since_high20=None if pd.isna(row["days_since_high20"]) else int(row["days_since_high20"]),
        avg_trading_value20_eok=nn(round(float(avg_val20), 1)) if not pd.isna(avg_val20) else None,
        score=sc["score"] if sc else None,
        value_growth_pct=sc["value_growth_pct"] if sc else None,
        vol_dryup_ratio=sc["vol_dryup_ratio"] if sc else None,
        peak_date=peak_date,
    )


def build_bars(feat: pd.DataFrame) -> list[dict]:
    tail = feat.tail(CHART_BARS).reset_index()

    def r(v, d=2):
        return None if pd.isna(v) else round(float(v), d)

    bars = []
    for _, b in tail.iterrows():
        bars.append({
            "time": b["date"].strftime("%Y-%m-%d"),
            "open": r(b["open"], 2), "high": r(b["high"], 2), "low": r(b["low"], 2), "close": r(b["close"], 2),
            "volume": int(b["volume"]),
            "ma5": r(b["ma5"]), "ma20": r(b["ma20"]), "ma60": r(b["ma60"]), "ma120": r(b["ma120"]),
            "macd": r(b["macd"], 3), "macd_signal": r(b["macd_signal"], 3), "macd_hist": r(b["macd_hist"], 3),
        })
    return bars


def main():
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BARS_DIR.mkdir(parents=True, exist_ok=True)

    print("[1/3] 유니버스 구성...")
    universe, funnel = krs.build_universe()
    print(f"      {funnel['final_universe_size']}종목 (시총 {krs.CONFIG['MCAP_MIN_EOK']:,}억+)")

    print("[2/3] 상태 플래그 + 3년치 캐시 갱신...")
    metas: dict[str, dict] = {}
    histories: dict[str, pd.DataFrame] = {}
    status_excluded, failed, discontinuity_excluded = [], [], []
    for i, (_, urow) in enumerate(universe.iterrows(), 1):
        code = urow["code"]
        flags = krs.fetch_status_flags(code)
        if flags:
            status_excluded.append(urow["name"])
            continue
        try:
            hist = krs.update_cache(code)
            if len(hist) < 130:
                failed.append(urow["name"])
                continue
            gaps = krs.detect_price_discontinuity(hist)      # INV-6 가드
            if gaps:
                discontinuity_excluded.append((urow["name"], code, gaps[:3]))
                continue
            histories[code] = krs.compute_features(hist)
            metas[code] = urow.to_dict()
        except Exception:
            failed.append(urow["name"])
        if i % 30 == 0:
            print(f"      {i}/{len(universe)}...")
        time.sleep(0.05)
    print(f"      상태플래그 제외 {len(status_excluded)}, 데이터실패 {len(failed)}, "
          f"가격불연속 제외 {len(discontinuity_excluded)}, 분석대상 {len(histories)}")
    if discontinuity_excluded:
        print(f"      가격불연속(미조정 분할 의심): {discontinuity_excluded[:5]}")

    last_dates = pd.Series([h.index.max() for h in histories.values()])
    run_date = last_dates.mode().iloc[0]
    stale = {c for c, h in histories.items() if h.index.max() < run_date}

    print(f"[3/3] {run_date.date()} 기준 대시보드 데이터 생성...")
    results = []
    for code, feat in histories.items():
        if code in stale:
            continue
        r = row_for_dashboard(feat, metas[code], run_date)
        if r is None:
            continue
        results.append(r)
        bars = build_bars(feat)
        (BARS_DIR / f"{code}.json").write_text(
            json.dumps({"code": code, "as_of": run_date.strftime("%Y-%m-%d"), "bars": bars}, ensure_ascii=False)
        )

    n_uptrend = sum(1 for r in results if r["is_uptrend"])
    n_candidate = sum(1 for r in results if r["is_candidate"])
    payload = {
        "payload": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_date": run_date.strftime("%Y-%m-%d"),
            "universe_count": funnel["final_universe_size"],
            "analyzed_count": len(results),
            "uptrend_count": n_uptrend,
            "candidate_count": n_candidate,
            "results": results,
        }
    }
    (OUT_DIR / "kr_dashboard_summary.json").write_text(json.dumps(payload, ensure_ascii=False))
    print(f"완료 ({time.time()-t0:.0f}초): 종목 {len(results)} / 정배열 {n_uptrend} / 후보 {n_candidate}")
    print(f"summary: {OUT_DIR / 'kr_dashboard_summary.json'}")
    print(f"bars dir: {BARS_DIR} ({len(list(BARS_DIR.glob('*.json')))} files)")


if __name__ == "__main__":
    main()
