#!/usr/bin/env python3
"""SCREENER_SPEC.md §7 검증 자동화 — V-3 / V-4 / V-7 + §8 회귀 케이스.

실행:  python3 tests/test_screener.py
네트워크를 쓰지 않는다. data/kr_cache 의 parquet 과
outputs/live/kr_dashboard_summary.json 의 유니버스 메타만 사용한다.

각 테스트는 판정(PASS/FAIL)과 함께 근거 숫자를 출력한다.
"수치 없이 통과했습니다"로 끝내지 않기 위한 파일이다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("krs", ROOT / "scripts" / "kr_pullback_screener.py")
krs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(krs)

CACHE_DIR = ROOT / "data" / "kr_cache"
SUMMARY = ROOT / "outputs" / "live" / "kr_dashboard_summary.json"

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"\n  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def load_universe() -> list[dict]:
    """유니버스 메타(코드/이름/시장/시총). 네트워크 대신 마지막 대시보드 산출물에서 읽는다."""
    payload = json.loads(SUMMARY.read_text())["payload"]
    return payload["results"]


def load_features(code: str) -> pd.DataFrame | None:
    p = CACHE_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if len(df) < 130:
        return None
    return krs.compute_features(df)


# ============================== V-3 ==========================================

def v3_days_since_high() -> None:
    """§7 V-3 — days_since_high20 = 1 인 종목이 통과하면 실패 (INV-4 회귀 방지)."""
    print("\n" + "=" * 78)
    print("V-3. 고점 경과일 테스트 (INV-4)")
    print("=" * 78)

    # (a) 합성 데이터: 어제가 20일 종가 고점이고 오늘 소폭 눌린 상승 종목
    n = 200
    # ndarray 로 만든다. Series 를 다른 인덱스의 DataFrame 에 넣으면 정렬되어 전부 NaN 이 된다.
    close = np.linspace(10_000, 20_000, n)                # 꾸준한 상승 -> 정배열 성립
    close[-1] = close[-2] * 0.97                          # 오늘만 -3% (고점 경과일 = 1)
    synth = pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "volume": np.full(n, 1_000_000.0),
    }, index=pd.bdate_range("2025-01-01", periods=n))
    feat = krs.compute_features(synth)
    j = len(feat) - 1
    dsh = int(feat["days_since_high20"].iloc[j])
    row = krs.screen_on_date(feat, feat.index[j], {"code": "TEST", "name": "합성", "market": "TEST"})

    ok_a = dsh == 1 and row is not None and row["is_candidate"] is False
    print(f"    합성종목: days_since_high20={dsh}, retrace_ratio={row['retrace_ratio'] if row else None}, "
          f"is_candidate={row['is_candidate'] if row else None}, score={row['score'] if row else None}")
    print(f"    탈락사유: {row['exclude_reason'] if row else '(행 없음)'}")
    record("V-3a 합성 days_since_high=1 종목 탈락", ok_a,
           f"경과일 {dsh}일, 후보여부 {row['is_candidate'] if row else 'N/A'}")

    # (b) §8 R-1 실제 회귀 케이스: KB금융 105560, 2026-09-04
    feat_kb = load_features("105560")
    target = pd.Timestamp("2026-09-04")
    if feat_kb is None or target not in feat_kb.index:
        record("V-3b R-1 KB금융(105560) 2026-09-04 탈락", False, "캐시에 해당 일자 없음")
    else:
        r = krs.screen_on_date(feat_kb, target, {"code": "105560", "name": "KB금융", "market": "KOSPI"})
        print(f"    KB금융 2026-09-04: 고점 {r['h20'] if 'h20' in r else feat_kb['h20'].loc[target]:,.0f} / "
              f"저점 {feat_kb['l20'].loc[target]:,.0f} / 종가 {r['close']:,.0f}")
        print(f"      retrace_ratio={r['retrace_ratio']}, days_since_high20={r['days_since_high20']}, "
              f"passes_retrace_band={r['passes_retrace_band']}, is_candidate={r['is_candidate']}, score={r['score']}")
        print(f"      탈락사유: {r['exclude_reason']}")
        ok_b = (r["is_candidate"] is False
                and r["days_since_high20"] == 1
                and r["score"] is None
                and "고점경과일" in r["exclude_reason"])
        record("V-3b R-1 KB금융(105560) 2026-09-04 탈락", ok_b,
               f"is_candidate={r['is_candidate']}, score={r['score']}, 경과일={r['days_since_high20']}")

    # (c) 전 종목 불변식: 후보 중 경과일이 허용 범위 밖인 건이 하나라도 있으면 실패
    lo, hi = krs.CONFIG["DAYS_SINCE_HIGH_MIN"], krs.CONFIG["DAYS_SINCE_HIGH_MAX"]
    violations, checked = [], 0
    for meta in load_universe():
        f = load_features(meta["code"])
        if f is None or target not in f.index:
            continue
        checked += 1
        r = krs.screen_on_date(f, target, meta)
        if r and r["is_candidate"] and not (lo <= (r["days_since_high20"] or -1) <= hi):
            violations.append((meta["name"], r["days_since_high20"]))
    print(f"    기준일 {target.date()} 검사 {checked}종목 · 허용범위 {lo}~{hi}일 · 위반 {len(violations)}건")
    record("V-3c 후보 전체가 경과일 범위 내", not violations, f"검사 {checked}종목, 위반 {len(violations)}건")

    # (d) §4 freshness 방향성: 고점 다음날이 이상구간보다 높은 점수를 받으면 안 된다
    f1, f4, f15 = krs.freshness_score(1), krs.freshness_score(4), krs.freshness_score(15)
    print(f"    freshness_score: 1일={f1:.2f}  4일={f4:.2f}  15일={f15:.2f}")
    record("V-3d freshness 방향 (1일 < 이상구간)", f1 < f4 and f15 < f4,
           f"1일 {f1:.2f} / 4일 {f4:.2f} / 15일 {f15:.2f}")


# ============================== V-4 ==========================================

def v4_distribution() -> None:
    """§7 V-4 — 최근 120거래일 통과 종목수 분포. 0종목 날 >5% 또는 20종목 초과 날 >25% 면 실패."""
    print("\n" + "=" * 78)
    print("V-4. 백테스트성 분포 확인 (최근 120거래일)")
    print("=" * 78)

    universe = load_universe()
    feats: dict[str, pd.DataFrame] = {}
    metas: dict[str, dict] = {}
    for meta in universe:
        f = load_features(meta["code"])
        if f is not None:
            feats[meta["code"]] = f
            metas[meta["code"]] = meta
    if not feats:
        record("V-4 분포", False, "캐시 없음")
        return

    calendar = max(feats.values(), key=len).index[-120:]
    counts, funnels = [], {}
    for d in calendar:
        n_screened = n_band = n_days = n_struct = n_cand = 0
        for code, f in feats.items():
            if d not in f.index:
                continue
            r = krs.screen_on_date(f, d, metas[code])
            if not r:
                continue
            n_screened += 1
            n_band += int(r["passes_retrace_band"])
            n_days += int(r["passes_days_since_high"])
            n_struct += int(r["passes_structure"])
            n_cand += int(r["is_candidate"])
        counts.append(n_cand)
        funnels[d] = (n_screened, n_band, n_days, n_struct, n_cand)

    a = np.array(counts)
    zero_ratio = float((a == 0).mean())
    over20_ratio = float((a > 20).mean())
    pass_rate = float(a.mean() / len(feats)) if feats else 0.0

    print(f"    유니버스 {len(feats)}종목 · 거래일 {len(a)}일")
    print(f"    평균 {a.mean():.2f} · 중앙값 {np.median(a):.0f} · 최소 {a.min()} · 최대 {a.max()}")
    print(f"    Q1 {np.percentile(a, 25):.0f} · Q3 {np.percentile(a, 75):.0f}")
    print(f"    0종목인 날 {int((a == 0).sum())}/{len(a)}일 ({zero_ratio:.1%})")
    print(f"    20종목 초과인 날 {int((a > 20).sum())}/{len(a)}일 ({over20_ratio:.1%})")
    print(f"    유니버스 대비 일평균 통과율 {pass_rate:.2%}")

    zero_days = [d for d, c in zip(calendar, counts) if c == 0]
    if zero_days:
        print(f"    0종목인 날 퍼널 (감지 -> 밴드 / 경과일 / 구조(INV-7) -> 후보):")
        for d in zero_days[:8]:
            s, b, dd, st, c = funnels[d]
            print(f"      {d.date()}  감지 {s} -> 밴드 {b} / 경과일 {dd} / 구조 {st} -> 후보 {c}")

    ok = zero_ratio <= 0.05 and over20_ratio <= 0.25
    record("V-4 분포 판정", ok,
           f"0종목 {zero_ratio:.1%} (기준 ≤5%), 20종목초과 {over20_ratio:.1%} (기준 ≤25%)")


# ============================== V-7 ==========================================

def v7_exclusion_rules() -> None:
    """§7 V-7 — 실제 KRX 종목명으로 제외 규칙 단위 테스트."""
    print("\n" + "=" * 78)
    print("V-7. 제외 규칙 단위 테스트")
    print("=" * 78)

    cases = [
        ("삼성전자", False, "일반주"),
        ("삼성전자우", True, "우선주"),
        ("현대차2우B", True, "우선주"),
        ("메리츠금융지주", False, "리츠 아님 (오탐 방지)"),
        ("ESR켄달스퀘어리츠", True, "리츠"),
        ("디비금융제11호스팩", True, "스팩"),
        ("KODEX 200", True, "ETF"),
    ]
    failed = []
    for name, expect_excluded, label in cases:
        got = krs.is_excluded_name(name)
        mark = "OK " if got == expect_excluded else "XX "
        if got != expect_excluded:
            failed.append((name, label, expect_excluded, got))
        print(f"    {mark} {name:<18} 기대={'제외' if expect_excluded else '통과'}  실제={'제외' if got else '통과'}   ({label})")

    detail = f"{len(cases) - len(failed)}/{len(cases)} 통과"
    if failed:
        detail += " · 실패: " + ", ".join(f"{n}({l})" for n, l, _, _ in failed)
    record("V-7 제외 규칙", not failed, detail)

    # ETF 브랜드 접두어(ACE·SOL·PLUS 등 짧은 토큰)가 일반 기업을 잡아채지 않는지 확인.
    # 이 규칙이 새로 만드는 위험이 바로 오탐이므로, 제외되는 이름은 전부 브랜드 접두어로
    # 시작해야 한다. 우선주/리츠 규칙이 엉뚱하게 발동하는 경우도 여기서 걸린다.
    universe = load_universe()
    excluded = [m["name"] for m in universe if krs.is_excluded_name(m["name"])]
    brands = tuple(b.upper() for b in krs.CONFIG["ETF_BRAND_PREFIXES"])
    not_a_product = [n for n in excluded if not n.strip().upper().startswith(brands)]
    print(f"    실제 유니버스 {len(universe)}종목 중 제외 대상 {len(excluded)}건: {excluded}")
    print(f"      -> 상장상품이 아닌데 제외된 건: {len(not_a_product)}건 {not_a_product if not_a_product else ''}")
    record("V-7b 일반 기업 오탐 없음", not not_a_product,
           f"제외 {len(excluded)}건 전부 ETF 브랜드 (오탐 {len(not_a_product)}건)")


# ============================== V-8 ==========================================

def _synthetic(win: np.ndarray, base_n: int = 200, base_start: float = 10_000.0,
               daily: float = 1.004) -> pd.DataFrame:
    """완만한 상승 기반(정배열·거래대금 성립) + **마지막 LEG_LOOKBACK 봉**을 지정한 경로로 붙인다.

    `win` 은 배율 배열이고 길이가 `LEG_LOOKBACK` 이어야 한다. 다리 탐색 창이 정확히 이 구간과
    겹치므로, ①(L_leg)은 base 가 아니라 `win` 안에서 잡힌다. 20봉짜리 꼬리만 붙이면
    계속 오르는 base 에서 L_leg 를 집어와 케이스 의도가 무너진다(R-4 반영 시 실제로 깨졌다).

    Series 가 아니라 ndarray 로 넘겨야 한다 — 인덱스가 다른 DataFrame 에 Series 를 넣으면
    pandas 가 정렬해 전부 NaN 이 된다.
    """
    win = np.asarray(win, dtype=float)
    lw = krs.CONFIG["LEG_LOOKBACK"]
    assert len(win) == lw, f"win 길이는 LEG_LOOKBACK({lw})이어야 한다 (받은 값 {len(win)})"
    # base 는 win[0] 바로 아래에서 끝나도록 맞춰 이어붙인다(연속성 + 정배열 성립).
    base = base_start * win[0] * daily ** np.arange(-base_n, 0)
    close = np.concatenate([base, base_start * win])
    n = len(close)
    return pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "volume": np.full(n, 1_000_000.0),
    }, index=pd.bdate_range("2024-01-01", periods=n))


def _judge(tail: np.ndarray, label: str) -> dict | None:
    feat = krs.compute_features(_synthetic(tail))
    d = feat.index[-1]
    r = krs.screen_on_date(feat, d, {"code": "TEST", "name": label, "market": "TEST"})
    row = feat.iloc[-1]
    print(f"    [{label}] 정배열={bool(row['is_uptrend'])} "
          f"H={row['h20']:,.0f} L_leg={row['l_leg']:,.0f} L_pull={row['l_pull']:,.0f} 종가={row['close']:,.0f}")
    print(f"          경과일={row['days_since_high20']:.0f} 바닥경과일={row['days_since_pullback_low']:.0f} "
          f"되돌림={row['retrace_ratio']:.3f} 상승다리={row['range_pct']*100:.1f}% "
          f"반등={row['bounce_from_low']*100:.2f}% 최근3일최저={bool(row['is_lowest_recent'])}")
    print(f"          -> 후보={r['is_candidate'] if r else '행 없음(지표 계산 불가)'}"
          + (f" / 사유: {r['exclude_reason']}" if r and r["exclude_reason"] else ""))
    return r


def v8_time_structure() -> None:
    """§7 V-8 — 합성 4케이스 + 전 종목 불변식 (INV-7 회귀 방지)."""
    print("\n" + "=" * 78)
    print("V-8. 시계열 구조 테스트 (INV-7)")
    print("=" * 78)

    # 40봉 창 구성: 0..35 상승(① -> ②, +15%), 36..39 눌림(③ -> ④) -> 고점 경과일 4일
    # ①은 창의 첫 봉(1.00). 눌림을 4봉으로 얕게 두는 이유: 더 깊게 파면 MA5가 MA20 아래로
    # 내려가 정배열 자체가 깨지고, 케이스가 의도한 조건이 아니라 추세 필터에서 탈락한다.
    up = np.linspace(1.000, 1.150, 36)
    def with_pull(pull):                        # 고점 이후 경로 (배율)
        return np.concatenate([up, np.array(pull, dtype=float)])

    # A: 눌림이 오늘까지 이어져 오늘이 바닥 -> 탈락
    ra = _judge(with_pull([1.138, 1.124, 1.113, 1.105]), "A 오늘이 바닥")
    ok_a = ra is not None and not ra["is_candidate"] and "오늘이 눌림 바닥" in ra["exclude_reason"]
    record("V-8a 오늘이 눌림 바닥이면 탈락", ok_a,
           f"바닥경과일 {ra['days_since_pullback_low'] if ra else 'N/A'}")

    # B: 바닥(37번봉) 이후 2일 반등 -> 통과
    rb = _judge(with_pull([1.138, 1.100, 1.108, 1.114]), "B 바닥 후 반등")
    ok_b = rb is not None and rb["is_candidate"]
    record("V-8b 바닥 후 반등이면 통과", ok_b,
           f"후보={rb['is_candidate'] if rb else 'N/A'}, 되돌림={rb['retrace_ratio'] if rb else 'N/A'}")

    # C: ① 없음 — 창 내내 하락하다 끝에서 반등. 고점 직전이 곧 최저라 상승 다리가 없다 -> 탈락
    rc = _judge(np.concatenate([np.linspace(1.300, 1.005, 22), np.linspace(1.000, 0.960, 12),
                                np.linspace(0.966, 0.999, 6)]), "C 상승 다리 없음")
    ok_c = rc is None or not rc["is_candidate"]
    record("V-8c 상승 다리 없이 하락→반등이면 탈락", ok_c,
           "행 없음(분모 0)" if rc is None else f"사유: {rc['exclude_reason']}")

    # D: 창 내내 횡보 (상승 다리 < 5%) -> 탈락
    wob = 1.0 + 0.012 * np.sin(np.linspace(0, 3 * np.pi, krs.CONFIG["LEG_LOOKBACK"]))
    rd = _judge(wob, "D 횡보")
    ok_d = rd is not None and not rd["is_candidate"] and "상승 다리" in rd["exclude_reason"]
    record("V-8d 횡보(상승 다리 5% 미만) 탈락", ok_d,
           f"상승다리 {rd['range_pct']*100:.1f}%" if rd else "행 없음")

    # (e) 전 종목 불변식 — 컬럼 값을 원본 종가에서 독립적으로 재계산해 대조하고,
    #     통과 종목이 전부 ①<② 이고 바닥경과일 ≥ 1 인지 확인한다.
    w = krs.CONFIG["HIGH_LOOKBACK"]
    lw = krs.CONFIG["LEG_LOOKBACK"]
    target = pd.Timestamp("2026-09-04")
    mismatches, order_viol, bounce_viol, checked, n_cand = [], [], [], 0, 0
    for meta in load_universe():
        f = load_features(meta["code"])
        if f is None or target not in f.index:
            continue
        j = f.index.get_loc(target)
        c = f["close"].to_numpy(dtype=float)
        win = c[j - w:j]
        hi = j - w + int(np.argmax(win))
        leg_start = max(0, j - lw)                    # 다리 탐색 창은 고점 창보다 넓다 (R-4)
        li = leg_start + int(np.argmin(c[leg_start:hi + 1]))
        pull_seg = c[hi:j + 1]
        lpi = hi + int(np.argmin(pull_seg))
        row = f.iloc[j]
        if not (row["l_leg"] == c[li] and row["l_pull"] == c[lpi]
                and row["days_since_pullback_low"] == j - lpi
                and row["days_since_high20"] == j - hi):
            mismatches.append(meta["name"])
        checked += 1
        r = krs.screen_on_date(f, target, meta)
        if r and r["is_candidate"]:
            n_cand += 1
            if not li < hi:                       # ① 은 ② 보다 앞이어야 한다
                order_viol.append((meta["name"], j - li, j - hi))
            if r["days_since_pullback_low"] < krs.CONFIG["PULLBACK_LOW_MIN_AGE"]:
                bounce_viol.append((meta["name"], r["days_since_pullback_low"]))
    print(f"    기준일 {target.date()} · 검사 {checked}종목 · 통과 {n_cand}종목")
    print(f"      독립 재계산 불일치 {len(mismatches)}건 {mismatches[:5] if mismatches else ''}")
    print(f"      ①→② 순서 위반 {len(order_viol)}건 {order_viol[:5] if order_viol else ''}")
    print(f"      바닥경과일 < {krs.CONFIG['PULLBACK_LOW_MIN_AGE']} 위반 {len(bounce_viol)}건")
    record("V-8e 전 종목 불변식 (①<② · 바닥경과일 ≥ 1 · 컬럼 재계산 일치)",
           not (mismatches or order_viol or bounce_viol),
           f"검사 {checked}종목/통과 {n_cand}종목, 위반 {len(mismatches)+len(order_viol)+len(bounce_viol)}건")


# ============================== V-9 ==========================================

def v9_old_vs_new() -> None:
    """§7 V-9 — retrace_ratio(신, 분모 H-L_leg) vs retrace_ratio_legacy(구, 분모 H-L20) 비교."""
    print("\n" + "=" * 78)
    print("V-9. 신구 정의 비교 (최근 130거래일 × 전 종목)")
    print("=" * 78)

    lo, hi_b = krs.CONFIG["RETRACE_MIN"], krs.CONFIG["RETRACE_MAX"]
    d_lo, d_hi = krs.CONFIG["DAYS_SINCE_HIGH_MIN"], krs.CONFIG["DAYS_SINCE_HIGH_MAX"]
    n_obs = n_diff = 0
    old_pass = s_bounce = s_lowest = s_leg = 0
    max_diff = 0.0
    old_in_new_out = 0

    for meta in load_universe():
        f = load_features(meta["code"])
        if f is None:
            continue
        tail = f.tail(130)
        for _, row in tail.iterrows():
            new, old = row["retrace_ratio"], row["retrace_ratio_legacy"]
            if pd.isna(new) or pd.isna(old) or not row["is_uptrend"]:
                continue
            n_obs += 1
            diff = abs(new - old)
            max_diff = max(max_diff, diff)
            if diff >= 0.01:
                n_diff += 1
            if lo <= old <= hi_b and not (lo <= new <= hi_b):
                old_in_new_out += 1
            # 구 기준 통과 신호(= 밴드 + 경과일)를 기준선으로 두고 조건을 하나씩 얹는다
            dsh = row["days_since_high20"]
            if not (lo <= old <= hi_b and d_lo <= dsh <= d_hi):
                continue
            old_pass += 1
            if row["days_since_pullback_low"] < krs.CONFIG["PULLBACK_LOW_MIN_AGE"]:
                continue
            s_bounce += 1
            if bool(row["is_lowest_recent"]):
                continue
            s_lowest += 1
            if pd.isna(row["range_pct"]) or row["range_pct"] < krs.CONFIG["RANGE_PCT_MIN"]:
                continue
            if not (lo <= new <= hi_b):
                continue
            s_leg += 1

    print(f"    비교 관측치 {n_obs:,}건 (정배열 & 두 값 모두 계산 가능한 날)")
    print(f"    |신-구| ≥ 0.01 인 건수: {n_diff:,}건 ({n_diff/max(n_obs,1):.1%}) · 최대 차이 {max_diff:.3f}")
    print(f"    구 정의로는 밴드 안 / 신 정의로는 밴드 밖: {old_in_new_out:,}건 (= 잘못 통과했던 신호)")
    print()
    print(f"    통과 신호 퍼널")
    print(f"      구 기준 (되돌림비율 밴드 + 고점경과일)            : {old_pass:,}건")
    print(f"      + 오늘이 눌림 바닥 아님 (PULLBACK_LOW_MIN_AGE)   : {s_bounce:,}건 ({s_bounce/max(old_pass,1):.0%})")
    print(f"      + 최근 {krs.CONFIG['NOT_LOWEST_IN_DAYS']}일 최저 아님 (NOT_LOWEST_IN_DAYS)     : {s_lowest:,}건 ({s_lowest/max(old_pass,1):.0%})")
    print(f"      + 상승 다리 {krs.CONFIG['RANGE_PCT_MIN']*100:.0f}%↑ & 신 정의도 밴드 안        : {s_leg:,}건 ({s_leg/max(old_pass,1):.0%})")

    # 판정: 감소가 실제로 일어났고(회귀 방지), 전부 사라지지는 않았는지만 본다.
    ok = old_pass > 0 and s_leg < old_pass and s_leg > 0
    record("V-9 신구 비교 (감소 확인)", ok, f"{old_pass:,} -> {s_leg:,}건 ({s_leg/max(old_pass,1):.0%} 잔존)")


# ============================== R-2 ==========================================

def r2_regression_cases() -> None:
    """§8 R-2 — 실제로 잘못 잡혔던 4건이 전부 탈락하는지."""
    print("\n" + "=" * 78)
    print("R-2. 회귀 케이스 (오늘이 눌림 바닥인데 통과했던 건)")
    print("=" * 78)

    cases = [("011200", "2025-06-27", "HMM"), ("011170", "2025-10-10", "롯데케미칼"),
             ("247540", "2025-12-18", "에코프로비엠"), ("034020", "2025-12-17", "두산에너빌리티")]
    survived = []
    for code, ds, name in cases:
        f = load_features(code)
        d = pd.Timestamp(ds)
        if f is None or d not in f.index:
            survived.append((code, ds, "캐시 없음"))
            print(f"    {name}({code}) {ds}: 캐시에 없음 -> 검증 불가")
            continue
        r = krs.screen_on_date(f, d, {"code": code, "name": name, "market": "KOSPI"})
        if r is None:
            print(f"    {name}({code}) {ds}: 행 없음(정배열/유동성 단계에서 제외)")
            continue
        print(f"    {name}({code}) {ds}: 되돌림 신={r['retrace_ratio']} 구={r['retrace_ratio_legacy']} "
              f"경과일={r['days_since_high20']} 바닥경과일={r['days_since_pullback_low']} "
              f"반등={r['bounce_from_low']*100:.2f}%")
        print(f"      -> 후보={r['is_candidate']} / 사유: {r['exclude_reason'] or '(없음)'}")
        if r["is_candidate"]:
            survived.append((code, ds, "여전히 통과"))
    record("R-2 회귀 4건 전부 탈락", not survived,
           "전부 탈락" if not survived else f"미탈락/검증불가 {survived}")


# ============================== R-4 ==========================================

def r4_leg_truncation() -> None:
    """§8 R-4 — 상승 다리가 고점 탐색 창 경계에서 잘려 되돌림이 부풀려지던 케이스."""
    print("\n" + "=" * 78)
    print("R-4. 상승 다리 절단 (LEG_LOOKBACK)")
    print("=" * 78)

    f = load_features("222800")
    d = pd.Timestamp("2026-09-04")
    if f is None or d not in f.index:
        record("R-4 심텍(222800) 2026-09-04 탈락", False, "캐시에 해당 일자 없음")
        return
    r = krs.screen_on_date(f, d, {"code": "222800", "name": "심텍", "market": "KOSDAQ"})
    row = f.loc[d]
    print(f"    H={row['h20']:,.0f} L_leg={row['l_leg']:,.0f}({row['days_since_leg_low']:.0f}일전) "
          f"L_pull={row['l_pull']:,.0f} 종가={row['close']:,.0f}")
    print(f"    되돌림 신={r['retrace_ratio']} 구(legacy)={r['retrace_ratio_legacy']} "
          f"고점대비낙폭={r['dd_from_high']*100:.2f}% 20일변동폭={(row['h20']-row['l20'])/row['h20']*100:.1f}%")
    print(f"    -> 후보={r['is_candidate']} / 사유: {r['exclude_reason'] or '(없음)'}")

    # 다리 시작점이 고점 탐색 창(20일) 밖에 있어야 이 케이스가 재현된다
    outside = row["days_since_leg_low"] > krs.CONFIG["HIGH_LOOKBACK"]
    ok = (not r["is_candidate"]) and outside and r["retrace_ratio"] < krs.CONFIG["RETRACE_MIN"]
    record("R-4 심텍(222800) 2026-09-04 탈락", ok,
           f"되돌림 {r['retrace_ratio']} (하한 {krs.CONFIG['RETRACE_MIN']}), "
           f"다리시작 {row['days_since_leg_low']:.0f}일전(고점창 {krs.CONFIG['HIGH_LOOKBACK']}일 밖={outside})")

    # 전 종목: L_leg 는 고점 창을 넘어선 곳에서 잡힐 수 있어야 한다(= 창이 실제로 넓어졌는가)
    ages = []
    for meta in load_universe():
        g = load_features(meta["code"])
        if g is None or d not in g.index:
            continue
        a = g.loc[d, "days_since_leg_low"]
        if not pd.isna(a):
            ages.append(a)
    ages = np.array(ages)
    beyond = int((ages > krs.CONFIG["HIGH_LOOKBACK"]).sum())
    print(f"    전 종목 다리시작 경과일: 중앙값 {np.median(ages):.0f}일 · 최대 {ages.max():.0f}일 · "
          f"고점창 밖에서 잡힌 종목 {beyond}/{len(ages)} ({beyond/len(ages):.0%})")
    record("R-4b 다리 탐색 창이 실제로 넓게 작동", beyond > 0,
           f"{beyond}/{len(ages)}종목이 고점창({krs.CONFIG['HIGH_LOOKBACK']}일) 밖에서 L_leg 확보")


def main() -> int:
    print("SCREENER_SPEC.md §7 검증 — V-3 / V-4 / V-7 / V-8 / V-9 / R-2 / R-4")
    print(f"CONFIG: DAYS_SINCE_HIGH {krs.CONFIG['DAYS_SINCE_HIGH_MIN']}~{krs.CONFIG['DAYS_SINCE_HIGH_MAX']}일 · "
          f"RETRACE {krs.CONFIG['RETRACE_MIN']}~{krs.CONFIG['RETRACE_MAX']} · "
          f"FRESHNESS_IDEAL {krs.CONFIG['FRESHNESS_IDEAL_MIN']}~{krs.CONFIG['FRESHNESS_IDEAL_MAX']}일")
    print(f"        INV-7: RANGE_PCT_MIN {krs.CONFIG['RANGE_PCT_MIN']:.0%} · "
          f"PULLBACK_LOW_MIN_AGE {krs.CONFIG['PULLBACK_LOW_MIN_AGE']}일 · "
          f"NOT_LOWEST_IN_DAYS {krs.CONFIG['NOT_LOWEST_IN_DAYS']}일")
    print(f"        탐색 창: HIGH_LOOKBACK {krs.CONFIG['HIGH_LOOKBACK']}일 · LEG_LOOKBACK {krs.CONFIG['LEG_LOOKBACK']}일")

    v3_days_since_high()
    v4_distribution()
    v7_exclusion_rules()
    v8_time_structure()
    v9_old_vs_new()
    r2_regression_cases()
    r4_leg_truncation()

    print("\n" + "=" * 78)
    n_pass = sum(1 for _, p, _ in results if p)
    for name, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name:<45} {detail}")
    print(f"\n  합계: {n_pass}/{len(results)} 통과")
    print("=" * 78)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
