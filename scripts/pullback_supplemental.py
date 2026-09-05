#!/usr/bin/env python3
"""Supplemental controls, benchmark, standalone filters and cost stress."""

from pathlib import Path
import math
import numpy as np
import pandas as pd

from pullback_backtest import (
    START, END, IS_END, OOS_START, Rule, costs, evaluate, generate_trades,
    indicators, load_yahoo, safe_symbol, select_nonoverlap, simulate_exit,
)


RAW = Path("data/raw_yahoo")
OUT = Path("analysis_output")
META = pd.read_csv("config/pullback_universe.csv")


def benchmark_metrics(df, market):
    rows = []
    for period, a, b in [("Full", START, END), ("IS", START, IS_END), ("OOS", OOS_START, END)]:
        s = df.loc[a:b, "close"].dropna()
        ret = s.pct_change().dropna()
        total = s.iloc[-1] / s.iloc[0] - 1
        years = (s.index[-1] - s.index[0]).days / 365.25
        cagr = (1 + total) ** (1 / years) - 1
        dd = s / s.cummax() - 1
        sharpe = math.sqrt(252) * ret.mean() / ret.std(ddof=1)
        rows.append({"market": market, "period": period, "benchmark": "S&P 500" if market == "US" else "KOSPI",
                     "total_return": total, "cagr": cagr, "max_drawdown": dd.min(), "sharpe": sharpe})
    return rows


def trend_only_trades(data, market):
    out = []
    mm = META.set_index("symbol")
    for symbol in META.loc[META.market == market, "symbol"]:
        if symbol not in data:
            continue
        x = data[symbol].copy()
        x["pull_low"] = x["low"].shift(1).rolling(10).min()
        x["peak_i"] = np.nan
        for j in range(253, len(x) - 1):
            r, p = x.iloc[j], x.iloc[j - 1]
            if not (r.close > r.ma20 and p.close <= p.ma20 and r.close > r.ma200 and r.ret20 >= .15 and r.ma50 > x.iloc[j-20].ma50):
                continue
            peak_i = j - 20 + int(np.nanargmax(x["high"].iloc[j-20:j].to_numpy()))
            x.iloc[j, x.columns.get_loc("peak_i")] = peak_i
            sim = simulate_exit(x, j + 1, j, Rule())
            if sim is None:
                continue
            bc, sc = costs(market)
            gross = sim["exit_price"] / sim["entry_price"] - 1
            net = sim["exit_price"] * (1-sc) / (sim["entry_price"] * (1+bc)) - 1
            out.append({"market": market, "symbol": symbol, "name": mm.loc[symbol, "name"], "sector": mm.loc[symbol, "sector"],
                        "rule": "trend_only_control", "setup_date": x.index[j], "signal_date": x.index[j],
                        "entry_date": x.index[sim["entry_i"]], "exit_date": x.index[sim["exit_i"]],
                        "entry_price": sim["entry_price"], "exit_price": sim["exit_price"], "initial_stop": sim["initial_stop"],
                        "risk_pct": sim["risk_pct"], "gross_return": gross, "net_return": net,
                        "r_multiple": net/sim["risk_pct"], "holding_days": sim["holding_days"], "exit_reason": sim["exit_reason"],
                        "depth": np.nan, "duration": np.nan, "prior_advance": r.ret20, "vol_ratio": np.nan,
                        "atr_pct": r.atr_pct, "rs60": r.rs60, "market_up": bool(r.market_up), "dist52": r.dist52,
                        "gap_fill": np.nan, "peak_high": x.iloc[peak_i].high})
    return pd.DataFrame(out)


def bootstrap_ci(trades, seed=7, reps=2000):
    if trades.empty:
        return (np.nan, np.nan)
    t = trades.copy(); t["month"] = pd.to_datetime(t.entry_date).dt.to_period("M").astype(str)
    groups = [g.net_return.to_numpy() for _, g in t.groupby("month")]
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(reps):
        sample = [groups[i] for i in rng.integers(0, len(groups), len(groups))]
        vals.append(np.concatenate(sample).mean())
    return tuple(np.quantile(vals, [.025, .975]))


def main():
    market_raw = {"US": load_yahoo(RAW / f"{safe_symbol('^GSPC')}.json", "^GSPC"),
                  "KR": load_yahoo(RAW / f"{safe_symbol('^KS11')}.json", "^KS11")}
    data = {}
    for row in META.itertuples(index=False):
        p = RAW / f"{safe_symbol(row.symbol)}.json"
        if p.exists():
            try:
                df = load_yahoo(p, row.symbol)
                if len(df) >= 300:
                    data[row.symbol] = indicators(df, market_raw[row.market])
            except Exception:
                pass
    bench = []
    for market in ["US", "KR"]:
        bench += benchmark_metrics(market_raw[market], market)
    pd.DataFrame(bench).to_csv(OUT / "benchmark_metrics.csv", index=False)

    control_metrics, control_logs, standalone = [], [], []
    for market in ["US", "KR"]:
        tr = trend_only_trades(data, market)
        rows, _, sel = evaluate(tr, data, "Trend-only control", market)
        control_metrics += rows; control_logs.append(sel)
        for f in ["volume", "market", "rs", "volatility", "near_high"]:
            rr = Rule(filters=(f,))
            raw = generate_trades(data, META, market, rr)
            rows, _, sel = evaluate(raw, data, f"Standalone:{f}", market)
            standalone += rows
    pd.DataFrame(control_metrics).to_csv(OUT / "control_metrics.csv", index=False)
    pd.concat(control_logs, ignore_index=True).to_csv(OUT / "control_trade_logs.csv", index=False)
    pd.DataFrame(standalone).to_csv(OUT / "standalone_filters.csv", index=False)

    logs = pd.read_csv(OUT / "all_trade_logs.csv", parse_dates=["entry_date", "exit_date"])
    stress_rows, ci_rows = [], []
    for (market, family, rule), g in logs.groupby(["market", "test_family", "rule"]):
        if family not in ["Baseline", "Filter"]:
            continue
        oos = g[g.entry_date >= OOS_START].copy()
        if len(oos) == 0:
            continue
        bc, sc = costs(market)
        oos["stress_return"] = oos.exit_price * (1-2*sc) / (oos.entry_price*(1+2*bc)) - 1
        pos, neg = oos.loc[oos.stress_return > 0, "stress_return"], oos.loc[oos.stress_return <= 0, "stress_return"]
        stress_rows.append({"market": market, "family": family, "rule": rule, "trades": len(oos),
                            "base_expectancy": oos.net_return.mean(), "double_cost_expectancy": oos.stress_return.mean(),
                            "double_cost_profit_factor": pos.sum()/abs(neg.sum()) if len(neg) else np.nan})
        lo, hi = bootstrap_ci(oos)
        ci_rows.append({"market": market, "family": family, "rule": rule, "trades": len(oos),
                        "expectancy": oos.net_return.mean(), "ci95_low": lo, "ci95_high": hi})
    pd.DataFrame(stress_rows).to_csv(OUT / "cost_stress.csv", index=False)
    pd.DataFrame(ci_rows).to_csv(OUT / "expectancy_ci.csv", index=False)


if __name__ == "__main__":
    main()
