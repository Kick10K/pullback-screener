#!/usr/bin/env python3
"""Embed the latest dashboard payload into dashboard/index.html as `var SEED = {...};`.

dashboard_refresh_kr.py writes outputs/live/ as plain JSON; this bakes that JSON into
the page so the HTML stays self-contained (§4) and renders without any network call.
Idempotent — rerun after every refresh.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "outputs" / "live" / "kr_dashboard_summary.json"
BARS_DIR = ROOT / "outputs" / "live" / "kr_dashboard_bars"
PAGE = ROOT / "dashboard" / "index.html"


def main() -> None:
    payload = json.loads(SUMMARY.read_text())["payload"]
    bars = {}
    for p in sorted(BARS_DIR.glob("*.json")):
        bars[p.stem] = json.loads(p.read_text())["bars"]
    payload["bars_by_code"] = bars

    seed = json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))
    html = PAGE.read_text()
    new, n = re.subn(r"var SEED = \{.*?\};\n", "var SEED = " + seed.replace("\\", "\\\\") + ";\n",
                     html, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError("index.html 에서 'var SEED = {...};' 를 찾지 못했다 — 페이지 구조가 바뀌었는지 확인할 것")
    PAGE.write_text(new)
    print(f"SEED 주입 완료: {len(payload['results'])}종목 · 차트 {len(bars)}종목 · "
          f"기준일 {payload['run_date']} · {len(new)/1e6:.2f}MB")


if __name__ == "__main__":
    main()
