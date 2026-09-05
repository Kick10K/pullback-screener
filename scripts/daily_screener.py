#!/usr/bin/env python3
"""Live pullback (되돌림) screener.

Fetches fresh daily OHLCV for the configured universe, finds the most
recent local peak per symbol, measures how far price pulled back from
that peak and how much it has rebounded off the pullback low, and
writes one JSON blob the dashboard artifact can consume.

This is a *live snapshot* tool -- separate from scripts/pullback_backtest.py,
which is a frozen historical study (2015-2025). Nothing here should change
the backtest's inputs or outputs.
"""

from __future__ import annotations

import csv
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_CSV = ROOT / "config" / "pullback_universe.csv"
OUT_JSON = ROOT / "outputs" / "live" / "screener_latest.json"

LOOKBACK_DAYS = 500          # history to fetch per symbol
PEAK_WINDOW = 20             # sessions to search for the local peak
MAX_DURATION = 10            # peak -> today, in sessions
DEPTH_MIN = 0.05
DEPTH_MAX = 0.15
CHART_BARS = 90              # bars sent to the dashboard for charting


def fetch_yahoo(symbol: str) -> dict | None:
    now = int(time.time())
    period1 = now - LOOKBACK_DAYS * 86400
    safe = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{safe}"
        f"?period1={period1}&period2={now}&interval=1d"
        f"&events=div%2Csplits&includeAdjustedClose=true"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt == 3:
                return None
            time.sleep(1.5)
    return None


def to_frame(obj: dict) -> pd.DataFrame | None:
    try:
        result = obj["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None
    ts = result.get("timestamp")
    if not ts:
        return None
    quote = result["indicators"]["quote"][0]
    adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose", quote["close"])
    df = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize(),
        "open_raw": quote["open"], "high_raw": quote["high"], "low_raw": quote["low"],
        "close_raw": quote["close"], "volume": quote["volume"], "adj_close": adj,
    }).dropna(subset=["open_raw", "high_raw", "low_raw", "close_raw", "adj_close"])
    if df.empty:
        return None
    ratio = df["adj_close"] / df["close_raw"]
    for col in ["open", "high", "low", "close"]:
        df[col] = df[f"{col}_raw"] * ratio
    df = df.set_index("date").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]
    return df[~df.index.duplicated(keep="last")]


def analyze(df: pd.DataFrame) -> dict | None:
    if len(df) < PEAK_WINDOW + 5:
        return None
    df = df.copy()
    df["ma50"] = df["close"].rolling(50).mean()
    df["ma200"] = df["close"].rolling(200).mean()
    df["vol_avg20"] = df["volume"].rolling(20).median()

    h, l, c, v = (df[k].to_numpy(float) for k in ["high", "low", "close", "volume"])
    n = len(df)
    j = n - 1
    w0 = max(0, j - PEAK_WINDOW)
    peak_i = w0 + int(np.nanargmax(h[w0:j + 1]))
    duration = j - peak_i
    peak_high = h[peak_i]
    pull_low = float(np.nanmin(l[peak_i:j + 1]))
    depth_pct = (peak_high - pull_low) / peak_high if peak_high else np.nan
    close_now = c[j]
    rebound_pct = (close_now - pull_low) / pull_low if pull_low else np.nan
    drawdown_from_peak_pct = (peak_high - close_now) / peak_high if peak_high else np.nan
    vol_avg20 = df["vol_avg20"].iloc[-1]
    vol_ratio = v[j] / vol_avg20 if vol_avg20 and not math.isnan(vol_avg20) and vol_avg20 > 0 else np.nan
    ma50_now = df["ma50"].iloc[-1]
    ma200_now = df["ma200"].iloc[-1]

    is_candidate = bool(
        duration >= 2 and duration <= MAX_DURATION
        and DEPTH_MIN <= depth_pct <= DEPTH_MAX
        and not math.isnan(ma200_now) and close_now > ma200_now
    )

    chart = df.tail(CHART_BARS).reset_index()
    bars = [
        {
            "time": row["date"].strftime("%Y-%m-%d"),
            "open": round(row["open"], 4),
            "high": round(row["high"], 4),
            "low": round(row["low"], 4),
            "close": round(row["close"], 4),
            "volume": int(row["volume"]),
        }
        for _, row in chart.iterrows()
    ]

    return {
        "close": round(float(close_now), 4),
        "peak_price": round(float(peak_high), 4),
        "peak_date": df.index[peak_i].strftime("%Y-%m-%d"),
        "pull_low": round(pull_low, 4),
        "duration_days": int(duration),
        "depth_pct": None if math.isnan(depth_pct) else round(float(depth_pct) * 100, 2),
        "rebound_pct": None if math.isnan(rebound_pct) else round(float(rebound_pct) * 100, 2),
        "drawdown_from_peak_pct": None if math.isnan(drawdown_from_peak_pct) else round(float(drawdown_from_peak_pct) * 100, 2),
        "volume": int(v[j]),
        "volume_avg20": None if vol_avg20 is None or math.isnan(vol_avg20) else int(vol_avg20),
        "volume_ratio": None if isinstance(vol_ratio, float) and math.isnan(vol_ratio) else (None if vol_ratio is None else round(float(vol_ratio), 2)),
        "above_ma50": None if math.isnan(ma50_now) else bool(close_now > ma50_now),
        "above_ma200": None if math.isnan(ma200_now) else bool(close_now > ma200_now),
        "is_candidate": is_candidate,
        "bars": bars,
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
    }


def main() -> None:
    universe = []
    with UNIVERSE_CSV.open() as f:
        for row in csv.DictReader(f):
            universe.append(row)

    results = []
    errors = []
    for row in universe:
        symbol = row["symbol"]
        obj = fetch_yahoo(symbol)
        if obj is None:
            errors.append(symbol)
            continue
        df = to_frame(obj)
        if df is None:
            errors.append(symbol)
            continue
        stats = analyze(df)
        if stats is None:
            errors.append(symbol)
            continue
        results.append({
            "symbol": symbol,
            "market": row.get("market", ""),
            "name": row.get("name", symbol),
            "sector": row.get("sector", ""),
            **stats,
        })
        time.sleep(0.15)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_count": len(universe),
        "ok_count": len(results),
        "error_symbols": errors,
        "results": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False))
    print(f"wrote {OUT_JSON} ok={len(results)} errors={len(errors)}")
    if errors:
        print("errors:", errors)


if __name__ == "__main__":
    main()
