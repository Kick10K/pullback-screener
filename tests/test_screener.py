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
        n_screened = n_band = n_days = n_cand = 0
        for code, f in feats.items():
            if d not in f.index:
                continue
            r = krs.screen_on_date(f, d, metas[code])
            if not r:
                continue
            n_screened += 1
            n_band += int(r["passes_retrace_band"])
            n_days += int(r["passes_days_since_high"])
            n_cand += int(r["is_candidate"])
        counts.append(n_cand)
        funnels[d] = (n_screened, n_band, n_days, n_cand)

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
        print(f"    0종목인 날 퍼널 (정배열+되돌림 감지 -> 밴드통과 / 경과일통과 -> 후보):")
        for d in zero_days[:8]:
            s, b, dd, c = funnels[d]
            print(f"      {d.date()}  감지 {s} -> 밴드 {b} / 경과일 {dd} -> 후보 {c}")

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


def main() -> int:
    print("SCREENER_SPEC.md §7 검증 — V-3 / V-4 / V-7")
    print(f"CONFIG: DAYS_SINCE_HIGH {krs.CONFIG['DAYS_SINCE_HIGH_MIN']}~{krs.CONFIG['DAYS_SINCE_HIGH_MAX']}일 · "
          f"RETRACE {krs.CONFIG['RETRACE_MIN']}~{krs.CONFIG['RETRACE_MAX']} · "
          f"FRESHNESS_IDEAL {krs.CONFIG['FRESHNESS_IDEAL_MIN']}~{krs.CONFIG['FRESHNESS_IDEAL_MAX']}일")

    v3_days_since_high()
    v4_distribution()
    v7_exclusion_rules()

    print("\n" + "=" * 78)
    n_pass = sum(1 for _, p, _ in results if p)
    for name, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name:<45} {detail}")
    print(f"\n  합계: {n_pass}/{len(results)} 통과")
    print("=" * 78)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
