#!/usr/bin/env python3
"""Reproducible OHLCV-only pullback study for a liquid US/KR survivor sample.

Signals use information known at the close and entries occur at the next open.
The script deliberately favors transparent rules over exhaustive optimization.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


START = pd.Timestamp("2015-01-01")
END = pd.Timestamp("2025-12-31")
IS_END = pd.Timestamp("2021-12-31")
OOS_START = pd.Timestamp("2022-01-01")


@dataclass(frozen=True)
class Rule:
    entry: str = "prior_high"
    stop: str = "structural"
    exit: str = "time10"
    depth_min: float = 0.05
    depth_max: float = 0.15
    max_duration: int = 10
    support_ma: int = 50
    filters: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        suffix = "+".join(self.filters) if self.filters else "none"
        return f"{self.entry}|{self.stop}|{self.exit}|d{self.depth_min:.2f}-{self.depth_max:.2f}|ma{self.support_ma}|{suffix}"


def safe_symbol(symbol: str) -> str:
    return symbol.replace("^", "_").replace("=", "_").replace("/", "_")


def load_yahoo(path: Path, symbol: str) -> pd.DataFrame:
    obj = json.loads(path.read_text())
    result = obj["chart"]["result"][0]
    ts = result.get("timestamp", [])
    quote = result["indicators"]["quote"][0]
    adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose", quote["close"])
    df = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize(),
        "open_raw": quote["open"], "high_raw": quote["high"], "low_raw": quote["low"],
        "close_raw": quote["close"], "volume": quote["volume"], "adj_close": adj,
    }).dropna(subset=["open_raw", "high_raw", "low_raw", "close_raw", "adj_close"])
    ratio = df["adj_close"] / df["close_raw"]
    for col in ["open", "high", "low", "close"]:
        df[col] = df[f"{col}_raw"] * ratio
    df = df.set_index("date").sort_index()
    df = df.loc[(df.index >= START) & (df.index <= END), ["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")]
    return df


def indicators(df: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    for n in [5, 10, 20, 50, 60, 120, 200]:
        x[f"ma{n}"] = x["close"].rolling(n).mean()
    prev = x["close"].shift(1)
    tr = pd.concat([(x["high"] - x["low"]), (x["high"] - prev).abs(), (x["low"] - prev).abs()], axis=1).max(axis=1)
    x["atr14"] = tr.rolling(14).mean()
    x["atr_pct"] = x["atr14"] / x["close"]
    x["ret20"] = x["close"].pct_change(20)
    x["ret60"] = x["close"].pct_change(60)
    x["rv20"] = np.log(x["close"]).diff().rolling(20).std() * math.sqrt(252)
    x["vol20"] = x["volume"].shift(1).rolling(20).median()
    x["high252"] = x["high"].shift(1).rolling(252).max()
    x["dist52"] = x["close"] / x["high252"] - 1
    m = market[["close"]].rename(columns={"close": "market_close"}).reindex(x.index).ffill()
    m["market_ma200"] = m["market_close"].rolling(200).mean()
    m["market_ret60"] = m["market_close"].pct_change(60)
    x = x.join(m)
    x["market_up"] = (x["market_close"] > x["market_ma200"]) & (x["market_ma200"] > x["market_ma200"].shift(20))
    x["rs60"] = x["ret60"] - x["market_ret60"]
    return x


def add_setups(x: pd.DataFrame, rule: Rule) -> pd.DataFrame:
    x = x.copy()
    n = len(x)
    fields = {k: np.full(n, np.nan) for k in ["peak_i", "duration", "depth", "prior_advance", "vol_ratio", "pull_low", "gap_fill"]}
    setup = np.zeros(n, dtype=bool)
    h, l, c, v = (x[k].to_numpy(float) for k in ["high", "low", "close", "volume"])
    ma = x[f"ma{rule.support_ma}"].to_numpy(float)
    ma200 = x["ma200"].to_numpy(float)
    ma50 = x["ma50"].to_numpy(float)
    for j in range(252, n):
        w0 = max(0, j - 20)
        peak_i = w0 + int(np.nanargmax(h[w0:j]))
        duration = j - peak_i
        if duration < 2 or duration > rule.max_duration:
            continue
        pre0 = max(0, peak_i - 20)
        prior_low = np.nanmin(l[pre0:peak_i + 1])
        advance = h[peak_i] / prior_low - 1 if prior_low > 0 else np.nan
        pull_low = np.nanmin(l[peak_i + 1:j + 1])
        depth = (h[peak_i] - pull_low) / h[peak_i]
        pb_vol = np.nanmean(v[peak_i + 1:j + 1])
        adv_vol = np.nanmean(v[max(0, peak_i - 5):peak_i + 1])
        vol_ratio = pb_vol / adv_vol if adv_vol > 0 else np.nan
        gap_fill = np.nan
        gap_idx = None
        for g in range(max(1, peak_i - 5), peak_i + 1):
            if l[g] > h[g - 1] * 1.003:
                gap_idx = g
        if gap_idx is not None:
            gap_floor, gap_top = h[gap_idx - 1], l[gap_idx]
            gap_fill = 0.0 if pull_low >= gap_top else (1.0 if pull_low <= gap_floor else (gap_top - pull_low) / (gap_top - gap_floor))
        base = (
            advance >= 0.15
            and rule.depth_min <= depth <= rule.depth_max
            and np.isfinite(ma[j]) and c[j] > ma[j]
            and np.isfinite(ma200[j]) and c[j] > ma200[j]
            and np.isfinite(ma50[j - 20]) and ma50[j] > ma50[j - 20]
        )
        if not base:
            continue
        setup[j] = True
        vals = {"peak_i": peak_i, "duration": duration, "depth": depth, "prior_advance": advance,
                "vol_ratio": vol_ratio, "pull_low": pull_low, "gap_fill": gap_fill}
        for k, val in vals.items():
            fields[k][j] = val
    x["setup"] = setup
    for k, vals in fields.items():
        x[k] = vals
    return x


def filter_ok(row: pd.Series, filters: tuple[str, ...]) -> bool:
    for f in filters:
        if f == "volume" and not (pd.notna(row.vol_ratio) and row.vol_ratio <= 0.75):
            return False
        if f == "market" and not bool(row.market_up):
            return False
        if f == "rs" and not (pd.notna(row.rs60) and row.rs60 >= 0.05):
            return False
        if f == "volatility" and not (pd.notna(row.atr_pct) and 0.015 <= row.atr_pct <= 0.06):
            return False
        if f == "near_high" and not (pd.notna(row.dist52) and row.dist52 >= -0.15):
            return False
    return True


def latest_setup(x: pd.DataFrame, j: int, lookback: int = 3) -> int | None:
    a = max(0, j - lookback)
    idx = np.flatnonzero(x["setup"].to_numpy()[a:j + 1])
    return None if len(idx) == 0 else a + int(idx[-1])


def trigger_ok(x: pd.DataFrame, j: int, entry: str) -> tuple[bool, int | None]:
    if entry == "anticipatory":
        return bool(x.iloc[j].setup), j
    ref = latest_setup(x, j - 1, 3) if j >= 1 else None
    if ref is None:
        return False, None
    r, p = x.iloc[j], x.iloc[j - 1]
    if entry == "prior_high":
        ok = r.close > p.high
    elif entry == "short_resistance":
        ok = j >= 5 and r.close > x["high"].iloc[j - 5:j].max()
    elif entry == "ma20_recapture":
        ok = p.close <= p.ma20 and r.close > r.ma20
    elif entry == "bull_volume":
        ok = r.close > r.open and r.close > p.close and r.volume >= 1.2 * r.vol20
    elif entry == "higher_low":
        ok = j >= 4 and x.iloc[j - 1].low > x.iloc[j - 3].low and r.close > x.iloc[j - 1].high
    else:
        ok = False
    return bool(ok), ref


def costs(market: str) -> tuple[float, float]:
    # Representative all-in implementation assumptions, not historical broker quotes.
    return (0.0005, 0.0005) if market == "US" else (0.00115, 0.00265)


def simulate_exit(x: pd.DataFrame, entry_i: int, setup_i: int, rule: Rule) -> dict | None:
    if entry_i >= len(x):
        return None
    entry = float(x.iloc[entry_i].open)
    atr = float(x.iloc[setup_i].atr14)
    pull_low = float(x.iloc[setup_i].pull_low)
    peak_i = int(x.iloc[setup_i].peak_i)
    peak_high = float(x.iloc[peak_i].high)
    if not np.isfinite(entry) or not np.isfinite(atr) or not np.isfinite(pull_low):
        return None
    if rule.stop == "structural":
        stop = pull_low - 0.10 * atr
    elif rule.stop == "fixed5":
        stop = entry * 0.95
    elif rule.stop == "atr2":
        stop = entry - 2.0 * atr
    else:  # ma20: risk proxy only; actual exit is next-open after close below MA20
        stop = entry - 2.0 * atr
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.20:
        return None
    max_hold = 20 if rule.exit == "time20" else (5 if rule.exit == "time5" else 10)
    end_i = min(len(x) - 1, entry_i + max_hold)
    exit_i, exit_price, reason = end_i, float(x.iloc[end_i].close), "time"
    trail = stop
    for k in range(entry_i, end_i + 1):
        day = x.iloc[k]
        if rule.stop != "ma20" and day.low <= stop:
            exit_i, exit_price, reason = k, min(float(day.open), stop), "stop"
            break
        if rule.stop == "ma20" and k < end_i and day.close < day.ma20:
            exit_i, exit_price, reason = k + 1, float(x.iloc[k + 1].open), "ma20_stop"
            break
        target = None
        if rule.exit == "fixed10": target = entry * 1.10
        elif rule.exit == "2R": target = entry + 2.0 * risk
        elif rule.exit == "previous_high": target = peak_high
        if target is not None and day.high >= target:
            exit_i, exit_price, reason = k, max(float(day.open), target), "target"
            break
        if rule.exit == "trailing2atr":
            if day.low <= trail:
                exit_i, exit_price, reason = k, min(float(day.open), trail), "trailing"
                break
            trail = max(trail, float(day.close - 2.0 * day.atr14))
        if rule.exit == "ma20" and k < end_i and day.close < day.ma20:
            exit_i, exit_price, reason = k + 1, float(x.iloc[k + 1].open), "ma20_exit"
            break
    return {"entry_i": entry_i, "exit_i": exit_i, "entry_price": entry, "exit_price": exit_price,
            "initial_stop": stop, "risk_pct": risk / entry, "exit_reason": reason,
            "holding_days": exit_i - entry_i + 1, "peak_high": peak_high}


def generate_trades(data: dict[str, pd.DataFrame], meta: pd.DataFrame, market_name: str, rule: Rule) -> pd.DataFrame:
    out = []
    symbols = meta.loc[meta.market == market_name, "symbol"].tolist()
    for symbol in symbols:
        if symbol not in data:
            continue
        x = add_setups(data[symbol], rule)
        for j in range(253, len(x) - 1):
            ok, setup_i = trigger_ok(x, j, rule.entry)
            if not ok or setup_i is None:
                continue
            s = x.iloc[setup_i]
            if not filter_ok(s, rule.filters):
                continue
            sim = simulate_exit(x, j + 1, setup_i, rule)
            if sim is None:
                continue
            buy_cost, sell_cost = costs(market_name)
            gross = sim["exit_price"] / sim["entry_price"] - 1
            net = sim["exit_price"] * (1 - sell_cost) / (sim["entry_price"] * (1 + buy_cost)) - 1
            out.append({
                "market": market_name, "symbol": symbol, "name": meta.set_index("symbol").loc[symbol, "name"],
                "sector": meta.set_index("symbol").loc[symbol, "sector"], "rule": rule.name,
                "setup_date": x.index[setup_i], "signal_date": x.index[j], "entry_date": x.index[sim["entry_i"]],
                "exit_date": x.index[sim["exit_i"]], "entry_price": sim["entry_price"], "exit_price": sim["exit_price"],
                "initial_stop": sim["initial_stop"], "risk_pct": sim["risk_pct"], "gross_return": gross,
                "net_return": net, "r_multiple": net / sim["risk_pct"], "holding_days": sim["holding_days"],
                "exit_reason": sim["exit_reason"], "depth": s.depth, "duration": int(s.duration),
                "prior_advance": s.prior_advance, "vol_ratio": s.vol_ratio, "atr_pct": s.atr_pct,
                "rs60": s.rs60, "market_up": bool(s.market_up), "dist52": s.dist52,
                "gap_fill": s.gap_fill, "peak_high": sim["peak_high"],
            })
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).sort_values(["entry_date", "symbol"]).reset_index(drop=True)


def select_nonoverlap(trades: pd.DataFrame, max_positions: int = 10) -> pd.DataFrame:
    if trades.empty:
        return trades
    active: list[tuple[pd.Timestamp, str]] = []
    chosen = []
    for date, group in trades.groupby("entry_date", sort=True):
        active = [(d, s) for d, s in active if d >= date]
        held = {s for _, s in active}
        slots = max_positions - len(active)
        if slots <= 0:
            continue
        g = group.loc[~group.symbol.isin(held)].sort_values(["rs60", "vol_ratio"], ascending=[False, True], na_position="last")
        for idx, row in g.head(slots).iterrows():
            chosen.append(idx)
            active.append((row.exit_date, row.symbol))
    return trades.loc[chosen].sort_values(["entry_date", "symbol"]).reset_index(drop=True)


def portfolio_nav(trades: pd.DataFrame, data: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    dates = pd.date_range(start, end, freq="B")
    if trades.empty:
        return pd.DataFrame({"date": dates, "nav": 1.0, "exposure": 0.0})
    entries = {d: g.to_dict("records") for d, g in trades.groupby("entry_date")}
    positions: dict[str, dict] = {}
    cash = 1.0
    rows = []
    for date in dates:
        for sym, pos in list(positions.items()):
            if pos["exit_date"] <= date:
                cash += pos["shares"] * pos["exit_price"] * (1 - pos["sell_cost"])
                del positions[sym]
        marks = []
        for sym, pos in positions.items():
            hist = data[sym].loc[:date, "close"]
            marks.append(pos["shares"] * float(hist.iloc[-1]) if not hist.empty else 0.0)
        equity_before = cash + sum(marks)
        for tr in entries.get(date, []):
            if tr["symbol"] in positions or len(positions) >= 10:
                continue
            buy_cost, sell_cost = costs(tr["market"])
            budget = min(cash, equity_before * 0.10)
            if budget <= 0:
                break
            shares = budget / (tr["entry_price"] * (1 + buy_cost))
            cash -= shares * tr["entry_price"] * (1 + buy_cost)
            positions[tr["symbol"]] = {"shares": shares, "exit_date": tr["exit_date"],
                "exit_price": tr["exit_price"], "sell_cost": sell_cost}
        marks = []
        for sym, pos in positions.items():
            hist = data[sym].loc[:date, "close"]
            marks.append(pos["shares"] * float(hist.iloc[-1]) if not hist.empty else 0.0)
        nav = cash + sum(marks)
        exposure = sum(marks) / nav if nav else 0.0
        rows.append((date, nav, exposure))
    return pd.DataFrame(rows, columns=["date", "nav", "exposure"])


def metrics(trades: pd.DataFrame, nav: pd.DataFrame, label: str, market: str, period: str) -> dict:
    if trades.empty:
        return {"label": label, "market": market, "period": period, "trades": 0}
    r = trades.net_return
    wins, losses = r[r > 0], r[r <= 0]
    wr = len(wins) / len(r)
    avgw = wins.mean() if len(wins) else 0.0
    avgl = abs(losses.mean()) if len(losses) else 0.0
    expectancy = wr * avgw - (1 - wr) * avgl
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.nan
    total = nav.nav.iloc[-1] / nav.nav.iloc[0] - 1
    years = max((nav.date.iloc[-1] - nav.date.iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1
    dd = nav.nav / nav.nav.cummax() - 1
    daily = nav.nav.pct_change().fillna(0)
    sharpe = math.sqrt(252) * daily.mean() / daily.std(ddof=1) if daily.std(ddof=1) > 0 else 0.0
    return {
        "label": label, "market": market, "period": period, "trades": len(trades), "total_return": total,
        "cagr": cagr, "win_rate": wr, "avg_win": avgw, "avg_loss": avgl, "profit_factor": pf,
        "expectancy": expectancy, "avg_r": trades.r_multiple.mean(), "max_drawdown": dd.min(),
        "sharpe": sharpe, "exposure": nav.exposure.mean(), "avg_holding_days": trades.holding_days.mean(),
        "gross_expectancy": trades.gross_return.mean(), "net_expectancy": trades.net_return.mean(),
    }


def evaluate(trades: pd.DataFrame, data: dict[str, pd.DataFrame], label: str, market: str) -> tuple[list[dict], dict[str, pd.DataFrame], pd.DataFrame]:
    selected = select_nonoverlap(trades)
    rows, navs = [], {}
    periods = {"Full": (START, END), "IS": (START, IS_END), "OOS": (OOS_START, END)}
    for pname, (a, b) in periods.items():
        t = selected[(selected.entry_date >= a) & (selected.entry_date <= b)].copy()
        nav = portfolio_nav(t, data, a, b)
        navs[pname] = nav
        rows.append(metrics(t, nav, label, market, pname))
    return rows, navs, selected


def bucket_table(trades: pd.DataFrame, col: str, bins: list, labels: list[str], market: str, name: str) -> pd.DataFrame:
    t = trades.copy()
    t["bucket"] = pd.cut(t[col], bins=bins, labels=labels, include_lowest=True)
    out = t.groupby("bucket", observed=False).agg(trades=("net_return", "size"), win_rate=("net_return", lambda s: (s > 0).mean()),
        expectancy=("net_return", "mean"), avg_r=("r_multiple", "mean"), avg_holding=("holding_days", "mean")).reset_index()
    out.insert(0, "market", market); out.insert(1, "analysis", name)
    return out


def regime_table(trades: pd.DataFrame, market: str) -> pd.DataFrame:
    t = trades.copy(); t["bucket"] = np.where(t.market_up, "시장 상승", "시장 비상승")
    out = t.groupby("bucket").agg(trades=("net_return", "size"), win_rate=("net_return", lambda s: (s > 0).mean()),
        expectancy=("net_return", "mean"), avg_r=("r_multiple", "mean")).reset_index()
    out.insert(0, "market", market); out.insert(1, "analysis", "시장환경")
    return out


def walk_forward(data: dict[str, pd.DataFrame], meta: pd.DataFrame, market: str) -> pd.DataFrame:
    candidates = [
        Rule(depth_min=a, depth_max=b, support_ma=ma)
        for a, b in [(0.03, 0.10), (0.05, 0.12), (0.05, 0.15), (0.08, 0.15)] for ma in [20, 50]
    ]
    cache = {r.name: select_nonoverlap(generate_trades(data, meta, market, r)) for r in candidates}
    rows = []
    for test_year in range(2018, 2026):
        train_start, train_end = pd.Timestamp(f"{test_year-3}-01-01"), pd.Timestamp(f"{test_year-1}-12-31")
        scored = []
        for r in candidates:
            t = cache[r.name]
            z = t[(t.entry_date >= train_start) & (t.entry_date <= train_end)]
            score = z.net_return.mean() if len(z) >= 20 else -999
            scored.append((score, r))
        best_score, best = max(scored, key=lambda z: z[0])
        test = cache[best.name]
        test = test[(test.entry_date >= pd.Timestamp(f"{test_year}-01-01")) & (test.entry_date <= pd.Timestamp(f"{test_year}-12-31"))]
        rows.append({"market": market, "test_year": test_year, "train_start": train_start, "train_end": train_end,
            "selected_rule": best.name, "train_expectancy": best_score, "test_trades": len(test),
            "test_expectancy": test.net_return.mean() if len(test) else np.nan,
            "test_win_rate": (test.net_return > 0).mean() if len(test) else np.nan,
            "test_profit_factor": test.loc[test.net_return > 0, "net_return"].sum() / abs(test.loc[test.net_return <= 0, "net_return"].sum()) if (test.net_return <= 0).any() else np.nan})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="data/raw_yahoo")
    ap.add_argument("--universe", default="config/pullback_universe.csv")
    ap.add_argument("--out-dir", default="analysis_output")
    args = ap.parse_args()
    raw_dir, out_dir = Path(args.raw_dir), Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = pd.read_csv(args.universe)
    index_map = {"US": "^GSPC", "KR": "^KS11"}
    market_data = {}
    for m, sym in index_map.items():
        p = raw_dir / f"{safe_symbol(sym)}.json"
        market_data[m] = load_yahoo(p, sym)
    data = {}
    load_status = []
    for row in meta.itertuples(index=False):
        p = raw_dir / f"{safe_symbol(row.symbol)}.json"
        if not p.exists():
            load_status.append({"symbol": row.symbol, "status": "missing", "rows": 0}); continue
        try:
            df = load_yahoo(p, row.symbol)
            if len(df) < 300:
                load_status.append({"symbol": row.symbol, "status": "insufficient", "rows": len(df)}); continue
            data[row.symbol] = indicators(df, market_data[row.market])
            load_status.append({"symbol": row.symbol, "status": "ok", "rows": len(df)})
        except Exception as exc:
            load_status.append({"symbol": row.symbol, "status": f"error:{type(exc).__name__}", "rows": 0})
    pd.DataFrame(load_status).to_csv(out_dir / "data_status.csv", index=False)

    result_rows, comparison_rows, all_selected, nav_outputs = [], [], [], []
    buckets, robustness, walk = [], [], []
    baseline_rule = Rule()
    for market in ["US", "KR"]:
        base_all = generate_trades(data, meta, market, baseline_rule)
        rows, navs, selected = evaluate(base_all, data, "Baseline", market)
        result_rows += rows; all_selected.append(selected.assign(test_family="Baseline"))
        for pname, nav in navs.items():
            nav_outputs.append(nav.assign(market=market, strategy="Baseline", period=pname))
        buckets.append(bucket_table(selected, "depth", [0, .05, .08, .12, .15, 1], ["<5%", "5-8%", "8-12%", "12-15%", ">15%"], market, "조정깊이"))
        buckets.append(bucket_table(selected, "duration", [0, 3, 6, 10, 99], ["1-3일", "4-6일", "7-10일", "10일+"], market, "조정기간"))
        buckets.append(bucket_table(selected, "vol_ratio", [0, .5, .75, 1, 99], ["≤0.50", "0.50-0.75", "0.75-1.00", ">1.00"], market, "거래량비율"))
        buckets.append(regime_table(selected, market))

        entry_rules = ["anticipatory", "prior_high", "short_resistance", "ma20_recapture", "bull_volume", "higher_low"]
        for ent in entry_rules:
            r = Rule(entry=ent)
            tr = generate_trades(data, meta, market, r)
            rr, _, sel = evaluate(tr, data, f"Entry:{ent}", market)
            comparison_rows += rr; all_selected.append(sel.assign(test_family="Entry"))
        for stop in ["structural", "fixed5", "atr2", "ma20"]:
            r = Rule(stop=stop)
            tr = generate_trades(data, meta, market, r)
            rr, _, sel = evaluate(tr, data, f"Stop:{stop}", market)
            comparison_rows += rr; all_selected.append(sel.assign(test_family="Stop"))
        for ex in ["time5", "time10", "time20", "fixed10", "2R", "trailing2atr", "ma20", "previous_high"]:
            r = Rule(exit=ex)
            tr = generate_trades(data, meta, market, r)
            rr, _, sel = evaluate(tr, data, f"Exit:{ex}", market)
            comparison_rows += rr; all_selected.append(sel.assign(test_family="Exit"))

        filter_steps = [(), ("volume",), ("volume", "market"), ("volume", "market", "rs"),
                        ("volume", "market", "rs", "volatility"), ("volume", "market", "rs", "volatility", "near_high")]
        for i, fs in enumerate(filter_steps):
            r = Rule(filters=fs)
            tr = generate_trades(data, meta, market, r)
            rr, _, sel = evaluate(tr, data, f"Filter:{i}:{'+'.join(fs) if fs else 'baseline'}", market)
            comparison_rows += rr; all_selected.append(sel.assign(test_family="Filter"))
        for a, b in [(0.03, .10), (.05, .12), (.05, .15), (.08, .15), (.10, .20)]:
            for ma in [20, 50]:
                r = Rule(depth_min=a, depth_max=b, support_ma=ma)
                tr = select_nonoverlap(generate_trades(data, meta, market, r))
                robustness.append({"market": market, "depth_min": a, "depth_max": b, "support_ma": ma,
                    "trades": len(tr), "expectancy": tr.net_return.mean() if len(tr) else np.nan,
                    "win_rate": (tr.net_return > 0).mean() if len(tr) else np.nan,
                    "profit_factor": tr.loc[tr.net_return > 0, "net_return"].sum() / abs(tr.loc[tr.net_return <= 0, "net_return"].sum()) if (tr.net_return <= 0).any() else np.nan})
        walk.append(walk_forward(data, meta, market))

    pd.DataFrame(result_rows).to_csv(out_dir / "baseline_metrics.csv", index=False)
    pd.DataFrame(comparison_rows).to_csv(out_dir / "strategy_comparison.csv", index=False)
    pd.concat(all_selected, ignore_index=True).to_csv(out_dir / "all_trade_logs.csv", index=False)
    pd.concat(nav_outputs, ignore_index=True).to_csv(out_dir / "nav_series.csv", index=False)
    pd.concat(buckets, ignore_index=True).to_csv(out_dir / "bucket_analysis.csv", index=False)
    pd.DataFrame(robustness).to_csv(out_dir / "robustness.csv", index=False)
    pd.concat(walk, ignore_index=True).to_csv(out_dir / "walk_forward.csv", index=False)
    print(json.dumps({"loaded": len(data), "baseline_rows": len(result_rows), "comparison_rows": len(comparison_rows),
                      "trade_rows": sum(len(x) for x in all_selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
