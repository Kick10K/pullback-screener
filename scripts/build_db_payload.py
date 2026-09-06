#!/usr/bin/env python3
"""Build the slimmed payload that goes into the Artifact DB.

dashboard/index.html 은 SEED(페이지에 박힌 데이터)로 먼저 그린 뒤, Artifact 런타임에서
`kr_screener_summary/latest` 문서를 읽어 **payload 전체를 덮어쓴다**. 따라서 이 문서가
낡으면 페이지를 아무리 다시 배포해도 화면은 옛 데이터를 보여준다. 갱신할 때마다
반드시 이 파일로 DB 문서도 같이 올릴 것.

DB 문서는 **256KiB** 한도가 있어 전체 payload(약 268KB)가 그대로는 안 들어간다.
페이지가 실제로 참조하는 필드만 남긴다 — 목록은 아래 ROW_FIELDS 가 단일 출처다.
필드를 화면에서 새로 쓰기 시작하면 여기에도 추가해야 한다(안 하면 그 칸만 '–'로 뜬다).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "outputs" / "live" / "kr_dashboard_summary.json"
OUT = ROOT / "outputs" / "live" / "kr_dashboard_db.json"

LIMIT_BYTES = 262_144

# dashboard/index.html 이 row.* 로 참조하는 필드 전부. 그 외(ma5~ma120, h20/l20,
# l_leg/l_pull, passes_*, value_growth_pct, vol_dryup_ratio)는 차트 bars 나 structure 에
# 이미 들어 있거나 화면에서 쓰지 않으므로 DB 문서에서 뺀다.
ROW_FIELDS = (
    "code", "name", "market", "sector", "industry", "date",
    "close", "mcap_eok", "is_uptrend", "is_candidate", "exclude_reason",
    "retrace_ratio", "retrace_ratio_legacy", "disparity_vs_ma20",
    "days_since_high20", "avg_trading_value20_eok", "score", "peak_date",
    "dd_from_high", "depth_pct", "range_pct", "bounce_from_low", "structure",
)


def main() -> None:
    payload = json.loads(SRC.read_text())["payload"]
    slim = {k: v for k, v in payload.items() if k != "results"}
    slim["results"] = [{k: r[k] for k in ROW_FIELDS if k in r} for r in payload["results"]]

    body = json.dumps({"payload": slim}, ensure_ascii=False)
    size = len(body.encode())
    OUT.write_text(body)

    dropped = sorted(set(payload["results"][0]) - set(ROW_FIELDS))
    print(f"DB payload: {len(slim['results'])}종목 · {size:,} bytes "
          f"({size / LIMIT_BYTES:.0%} of {LIMIT_BYTES:,}B 한도)")
    print(f"  제외한 행 필드: {', '.join(dropped)}")
    print(f"  -> {OUT}")
    if size > LIMIT_BYTES:
        raise SystemExit(f"한도 초과 {size - LIMIT_BYTES:,}B — ROW_FIELDS 를 더 줄이거나 문서를 나눠야 한다")


if __name__ == "__main__":
    main()
