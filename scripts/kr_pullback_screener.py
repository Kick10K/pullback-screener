#!/usr/bin/env python3
"""KR daily pullback ("눌림목") screener.

Run once after the KOSPI/KOSDAQ close. Builds a large/mid-cap universe,
requires an intact long-side uptrend (MA5>20>60>120, rising 60MA, not
overheated), and flags names that have pulled back a moderate amount from
their most recent 20-session closing high -- purely on CLOSING PRICES, so
an intraday wick through the 20MA that closes back above it is not treated
as a pullback.

DATA SOURCE NOTE: the brief asked for pykrx first, FinanceDataReader as a
fallback. Both were tried and both are unusable right now (see the run
report printed at the end and outputs/kr_screener/data_source_report.txt):
pykrx's underlying host (data.krx.co.kr) now returns "로그인 또는
회원가입이 필요합니다" for the anonymous stat endpoints pykrx calls --
KRX put its free data behind a login wall. FinanceDataReader could not
even be installed (this sandbox's pip index has no distribution for it).
This script instead uses Naver Finance's public, unauthenticated
endpoints (m.stock.naver.com, finance.naver.com, api.finance.naver.com),
which are what most Korean retail-quant tooling has fallen back to for
the same reason. Two consequences worth knowing:
  - Daily 거래대금 is not published directly by this source; it is
    approximated as close * volume (the standard proxy).
  - PRICE BASIS (verified 2026-09-06, INV-6): the series IS adjusted for
    splits and bonus issues, and is NOT adjusted for dividends. Evidence:
    (a) across 141 universe stocks x 3 years there is not one close-to-close
        move outside the KRX ±30% price limit -- an unadjusted split would
        necessarily produce one (ratio 0.5 / 0.2 / 0.1);
    (b) the series lines up with Yahoo's split-adjusted raw closes
        (median naver/yahoo_raw = 1.0000 on 005930 / 051910 / 035420) and
        sits 1-3% above Yahoo's adjclose, which is the dividend leg.
    An earlier version of this docstring claimed the opposite without
    checking. detect_price_discontinuity() now enforces this as a guard
    rather than a comment.
"""

from __future__ import annotations

import json
import re
import time
import io
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "kr_cache"
OUT_DIR = ROOT / "outputs" / "kr_screener"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# ============================== CONFIG ======================================
# Every threshold the screener applies lives here. Change these, not the
# logic below, to retune the screener.
CONFIG = dict(
    # --- universe ---
    MCAP_MIN_EOK = 15_000,          # 시가총액 하한, 억원 단위. 15,000억 = 1.5조원.
    MIN_LISTING_DAYS = 60,          # 상장일로부터 최소 경과 영업일. 이보다 짧으면 제외.
    MIN_AVG_TRADING_VALUE_EOK = 10, # 20일 평균 거래대금(억원) 하한. 대형주 위주라 느슨한 기본값.
                                     # 올리면 유동성 낮은 종목이 빠지고, 내리면 호가가 얕은 종목이 섞인다.
    EXCLUDE_PREFERRED_BY_CODE = True,  # 종목코드 끝자리가 0이 아니면 우선주로 본다(§3-1).
                                     # 이름 패턴('우','우B')만으로는 '한화3우B' 같은 변형을 놓친다.
                                     # 끄면 우선주가 유니버스에 섞여 같은 기업이 중복 신호를 낸다.
    EXCLUDE_TICKERS_FILE = "config/exclude_tickers.txt",  # 관리종목/거래정지를 직접 적어 두는 목록(§5 2단계).
                                     # 한 줄에 종목코드 하나. KRX가 안정적인 공개 API를 주지 않아 수동 보완이 필요하다.

    # --- 관리종목/거래정지/이상급등 휴리스틱 (§5 3단계) ---
    HALT_LOOKBACK_DAYS = 10,        # 최근 N 거래일 안에
    HALT_ZERO_VALUE_DAYS = 1,       # 거래대금 0원인 날이 이 일수 이상이면 거래정지로 간주해 제외.
                                     # 올리면 짧은 정지를 놓치고, 내리면 거래가 한산한 날 하루에도 탈락한다.
    EXTREME_MOVE_LOOKBACK = 20,     # 최근 N 거래일 안에
    EXTREME_MOVE_THRESHOLD = 0.25,  # 단일 종가 변동이 ±이 값 이상이면 이상급등/투자경고로 보고 제외.
                                     # 0으로 두면 이 필터가 꺼진다. 내리면 정상 급등주까지 잘려 나간다.
    EXCLUDE_NAME_RE = re.compile(   # 우선주/스팩/리츠 이름 패턴 (종목코드로 우선주를 구분하는 방법도 있으나
        r"(우[A-Z]?B?$|스팩\d*호?$|리츠$)"     # 이름 규칙이 더 안정적이라 이름 기준으로 제외.
    ),                                   # 주의: "리츠"는 끝 앵커($) 필수 -- 안 그러면 "메리츠금융지주"처럼
                                          # 리츠가 아닌데 이름에 "리츠"가 들어간 회사가 오탐된다.
    ETF_BRAND_PREFIXES = (          # ETF/ETN/ELW 브랜드 접두어 (§3-1). 새 브랜드가 생기면 여기에만 추가한다.
        "KODEX", "TIGER", "KBSTAR", "ACE", "RISE", "PLUS", "SOL", "KOSEF",
        "ARIRANG", "HANARO", "TIMEFOLIO", "KIWOOM", "VITA", "UNICORN", "BNK",
    ),                                   # 접두어로 시작하고 '뒤에 공백이나 숫자가 오는' 경우만 상장상품으로 본다.
                                          # 뒤 문자를 확인하지 않으면 ACE/SOL/PLUS 같은 짧은 토큰이 일반
                                          # 종목명에 부분 일치해 오탐한다. 확인을 없애면 정상 기업이 사라진다.

    # --- trend filter (정배열) ---
    MA_PERIODS = (5, 20, 60, 120),  # 정배열 판정에 쓰는 이동평균 기간.
    MA60_SLOPE_LOOKBACK = 20,       # 60일선 기울기를 며칠 전 대비로 잴지. 5일 전 대비는 노이즈에 가까워
                                     # 20일로 둔다(§3-2). 내리면 횡보 구간이 '상승'으로 잡힌다.
    MA5_ALIGN_TOLERANCE = 0.97,     # MA5 > MA20 * 이 값. 되돌림 중에는 MA5가 MA20 아래로 잠깐 내려가는 게
                                     # 정상이라 이 조건에만 완화를 허용한다(§3-2). 1.0으로 올리면 눌림이
                                     # 깊은 종목이 추세 단계에서 먼저 탈락해 되돌림 조건에 도달하지 못한다.
    DISPARITY_MA = 20,              # 이격도 = 종가 / 이 기간 이평선. 점수 계산의 기준선.
    # 이격도 밴드. 상한은 과열, 하한은 추세 훼손을 잘라낸다. 장기선일수록 폭이 넓어야 한다.
    DISPARITY_MAX_MA20 = 1.12,      # 종가가 20일선보다 12% 넘게 높으면 과열 -> 제외.
    DISPARITY_MAX_MA60 = 1.35,      # 60일선 대비 상한.
    DISPARITY_MAX_MA120 = 1.75,     # 120일선 대비 상한. 올리면 급등 후 눌림도 통과한다.
    DISPARITY_MIN_MA20 = 0.92,      # 종가가 20일선보다 8% 넘게 낮으면 되돌림이 아니라 추세 훼손 -> 제외.
    DISPARITY_MIN_MA60 = 0.98,      # 종가는 60일선 근처 이상은 유지해야 한다.
                                     # 하한을 내리면(=끄면) 20일선을 크게 이탈한 종목이 되돌림으로 잡힌다.

    # --- retracement (되돌림), 종가 기준 ---
    HIGH_LOOKBACK = 20,             # ② 고점 탐색 창. 당일을 제외한 최근 N일 종가에서 H를 잡는다.
                                     # 올리면 낡은 고점이 기준이 되고, 내리면 사소한 봉우리마다 신호가 난다.
    LEG_LOOKBACK = 40,              # ① 상승 다리 시작점(L_leg) 탐색 창. H_date 이전으로 이 일수까지 거슬러
                                     # 올라가 최저 종가를 찾는다. 고점 창(20일)과 같게 두면 상승 다리가
                                     # 창 경계에서 잘려 분모가 실제보다 작아지고 되돌림비율이 부풀려진다
                                     # (R-4 심텍: 20일이면 0.385 통과, 실제 다리로는 0.174 탈락).
                                     # 내리면 그 절단이 되살아나고, 올리면 다리 시작점이 몇 달 전 저점까지
                                     # 내려가 되돌림비율이 전부 밴드 아래로 깔린다.
    RETRACE_MIN = 0.20,             # 통과 하한. (H-종가)/(H-L_leg) 이 값 미만이면 눌림이 너무 얕음 -> 제외.
    RETRACE_MAX = 0.70,             # 통과 상한. 이 값 초과면 추세 훼손 수준의 눌림 -> 제외.
                                     # 비율 자체는 필터로 잘라도 결과 테이블 컬럼에는 항상 남긴다.
    DD_FROM_HIGH_MAX = 0.18,        # 고점 대비 낙폭 상한(§3-3). 되돌림비율은 상대값이라 같은 0.4대에서도
                                     # 실제 낙폭이 3~17%로 벌어진다. 절대 낙폭의 꼬리를 자르는 안전장치다.
                                     # 올리면 추세 전환급 하락이 되돌림으로 섞이고, 내리면 깊은 눌림이 잘린다.

    # --- 시계열 구조 조건 (INV-7): ① L_leg -> ② H -> ③ L_pull -> ④ 오늘 ---
    RANGE_PCT_MIN = 0.05,           # 상승 다리 (H-L_leg)/L_leg 하한. 이 아래면 '눌림'이 아니라 횡보의
                                     # 노이즈 고점/저점이다. 횡보에서도 ①②③④는 기계적으로 항상 존재하므로,
                                     # 상승 다리가 실재하는지 보는 이 조건이 INV-7의 전제다.
                                     # 내리면(=0) 방향성 없는 횡보 종목이 되돌림비율 밴드에 우연히 들어와 섞인다.
    PULLBACK_LOW_MIN_AGE = 1,       # 눌림 바닥(③) 이후 최소 경과 거래일. 0으로 내리면 '오늘이 바닥'인,
                                     # 즉 아직 하락 중이라 내일 더 빠질지 모르는 신호가 다시 44% 섞인다.
                                     # 0으로 두지 말 것. 올리면 반등 확인은 확실해지나 진입이 늦어진다.
    NOT_LOWEST_IN_DAYS = 3,         # 당일 종가가 최근 N일(당일 포함) 최저면 탈락시키는 보조 조건.
                                     # 1이면 항상 참이라 사실상 무효. 올리면 반등 확인이 엄격해지고 후보가 빠르게 준다.

    # --- 되돌림 구간 형성 조건 (INV-4) ---
    DAYS_SINCE_HIGH_MIN = 2,        # 고점 이후 최소 경과 거래일. 1이면 '고점 다음날 음봉 하나'일 뿐 되돌림 구간이
                                     # 아직 없다. 내리면(=1 허용) 상승 진행 중인 종목이 후보로 섞이고, 눌림 구간이
                                     # 1일뿐이라 vol_dryup_ratio가 통계적으로 무의미해진다. 2 미만으로 두지 말 것.
    DAYS_SINCE_HIGH_MAX = 15,       # 고점 이후 최대 경과 거래일. 넘으면 눌림이 아니라 추세 이탈/횡보로 본다.
                                     # 올리면 낡은 고점을 기준으로 한 종목이 늘고, 내리면 후보 수가 빠르게 준다.
    FRESHNESS_IDEAL_MIN = 3,        # freshness 점수 만점 구간 시작(거래일).
    FRESHNESS_IDEAL_MAX = 8,        # freshness 점수 만점 구간 끝. 넓히면 경과일 변별력이 사라지고,
                                     # 좁히면 특정 일수에만 점수가 쏠린다.

    # --- 데이터 무결성 가드 (INV-6) ---
    PRICE_GAP_MAX_RATIO = 1.35,     # 전일 종가 대비 당일 종가 배율 상한.
    PRICE_GAP_MIN_RATIO = 0.65,     # 하한. KRX 가격제한폭이 ±30%라 이 밖의 종가 점프는 실제 거래로 설명되지 않고
                                     # 미조정 액면분할/무상증자를 의심해야 한다. 해당 종목은 제외하고 로그로 남긴다.
                                     # 넓히면(1.5/0.5) 실제 분할을 놓치고, 좁히면 상·하한가(±30%)가 오탐된다.

    # --- history / cache ---
    HISTORY_YEARS = 3,              # 최초 캐시 적재 시 받아올 연수.
    SPARKLINE_DAYS = 60,            # HTML 리포트 스파크라인에 쓸 최근 거래일 수.

    # --- scoring: 6개 항목을 0~1로 정규화해 가중합한 뒤 100점 만점으로 환산 ---
    # 가중치를 0으로 두면 그 항목은 점수에서 빠진다.
    WEIGHT_VALUE_SURGE = 20.0,      # 고점 직전 상승구간 거래대금 / 60일 평균 (수급 유입)
    WEIGHT_VOLUME_DRYUP = 25.0,     # 눌림 구간 거래량 / 상승 구간 거래량 (낮을수록 건전한 눌림)
    WEIGHT_MA20_PROXIMITY = 20.0,   # 20일선에 얼마나 붙어 있는가
    WEIGHT_FRESHNESS = 10.0,        # 고점 갱신 후 경과일이 이상 구간(3~8일)인가
    WEIGHT_RETRACE_QUALITY = 15.0,  # 되돌림비율이 피보나치 0.45 부근인가
    WEIGHT_TREND_STRENGTH = 10.0,   # 60일선 기울기 + 120일 상대수익률

    ADVANCE_VOL_DAYS = 5,           # 상승 구간 거래량/거래대금을 고점 직전 며칠로 볼지(§4).
                                     # 20일로 넓게 잡으면 되돌림 이전 구간이 섞여 상승 구간이 희석된다.
    VALUE_SURGE_LOOKBACK = 60,      # value_surge 의 비교 기준이 되는 평균 거래대금 기간.
    VALUE_SURGE_FULL = 2.0,         # 평소의 이 배수 이상이면 만점.
    VOLUME_DRYUP_FULL = 0.55,       # 되돌림 거래량이 상승구간의 이 비율 이하면 만점(=45% 이상 감소).
    VOLUME_DRYUP_ZERO = 1.30,       # 오히려 30% 늘었으면 0점.
    MA20_PROXIMITY_FULL = 0.015,    # 20일선과 1.5% 이내면 만점.
    MA20_PROXIMITY_ZERO = 0.10,     # 10% 벗어나면 0점.
    RETRACE_IDEAL = 0.45,           # 되돌림비율이 이 값이면 만점.
    RETRACE_SIGMA = 0.18,           # 가우시안 폭. 좁히면 특정 비율에만 점수가 쏠린다.
    TREND_SLOPE_FULL = 0.08,        # 60일선이 20일 동안 8% 오르면 만점.
    TREND_RETURN_LOOKBACK = 120,    # 상대수익률 기간.
    TREND_RETURN_FULL = 0.40,       # 120일 수익률 40%면 만점.
)

TARGET_MCAP_MIN_WON = CONFIG["MCAP_MIN_EOK"] * 1e8
TARGET_MIN_AVG_VALUE_WON = CONFIG["MIN_AVG_TRADING_VALUE_EOK"] * 1e8


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_text(url: str, encoding: str, timeout: int = 20) -> str:
    return http_get(url, timeout).decode(encoding, errors="ignore")


# ============================ universe building =============================

def is_excluded_name(name: str) -> bool:
    """종목명만으로 우선주/스팩/리츠/ETF·ETN·ELW를 걸러낸다 (§3-1).

    판별 규칙은 CONFIG에만 둔다(INV-5). 유니버스는 KIND 상장법인 목록과도 대조하므로
    실제 파이프라인에서는 ETF가 이중으로 걸러지지만, 이름만으로도 판별 가능해야 한다.
    """
    s = str(name).strip()
    if CONFIG["EXCLUDE_NAME_RE"].search(s):
        return True

    upper = s.upper()
    for brand in CONFIG["ETF_BRAND_PREFIXES"]:
        if upper.startswith(brand):
            rest = upper[len(brand):]
            if rest == "" or rest[0].isspace() or rest[0].isdigit():
                return True

    return bool(re.search(r"\b(ETN|ELW)\b", upper))


def is_preferred_code(code: str) -> bool:
    """종목코드 끝자리로 우선주를 판별한다 (§3-1).

    보통주는 끝자리가 0이다. 이름 패턴('우', '우B')만 보면 '한화3우B' 같은 변형이나
    이름이 특이한 우선주를 놓친다. 이름 규칙과 함께 쓴다.
    """
    if not CONFIG["EXCLUDE_PREFERRED_BY_CODE"]:
        return False
    c = str(code).strip().zfill(6)
    return len(c) == 6 and c[-1] != "0"


def load_manual_excludes() -> set[str]:
    """§5 2단계 — 사용자가 직접 관리하는 제외 목록. 없으면 주석만 있는 빈 파일을 만든다."""
    path = ROOT / CONFIG["EXCLUDE_TICKERS_FILE"]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# 관리종목/거래정지/투자경고 등 직접 제외할 종목코드를 한 줄에 하나씩 적는다.\n"
            "# KRX가 이 목록을 안정적인 공개 API로 주지 않아 수동 보완이 필요하다 (SCREENER_SPEC.md §5).\n"
            "# 예)\n# 900110\n",
            encoding="utf-8",
        )
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(line.zfill(6))
    return out


def fetch_kind_listing(market_type: str) -> pd.DataFrame:
    """market_type: 'stockMkt' (KOSPI) or 'kosdaqMkt' (KOSDAQ)."""
    url = f"https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType={market_type}"
    html = http_get_text(url, "euc-kr")
    tables = pd.read_html(io.StringIO(html))
    df = tables[0]
    df.columns = ["name", "market_seg", "code", "industry", "product", "listing_date", "settle_month", "ceo", "homepage", "region"][:len(df.columns)]
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["listing_date"] = pd.to_datetime(df["listing_date"], errors="coerce")
    # 한국전력공사처럼 지역 컬럼만 다른 중복 행이 있다. 그대로 두면 merge에서 행이 불어난다.
    df = df.drop_duplicates("code")
    return df[["code", "name", "listing_date", "industry"]]


# KIND 업종(KSIC)은 유니버스 139종목에 52종이 나올 만큼 잘게 쪼개져 있어 한눈에 보기 어렵다.
# 화면 표시용으로만 굵게 접는다. 위에서부터 먼저 걸리는 규칙을 쓰므로 순서가 의미를 가진다.
# 주의: 이 값은 표시 전용이며 스크리닝 판정에 일절 쓰지 않는다.
SECTOR_RULES = (
    (r"반도체|전자부품|통신 및 방송 장비|측정, 시험|컴퓨터 및 주변장치", "반도체·전자"),
    (r"일차전지|이차전지", "2차전지"),
    (r"소프트웨어|컴퓨터 프로그래밍|자료처리", "IT·소프트웨어"),
    (r"오디오물 출판|영화|방송업|광고", "미디어·엔터"),
    (r"금융업|보험업|금융 지원|은행", "금융·지주"),   # 국내 지주회사는 KSIC상 '기타 금융업'으로 분류된다
    (r"의약|의료용|자연과학 및 공학 연구개발", "바이오·헬스케어"),
    (r"화학|고무제품|철강|비철금속|비금속 광물|절연선", "화학·소재"),
    (r"전동기, 발전기|기타 전기장비", "전력기기"),
    (r"선박|항공기,우주선|무기 및 총포탄|그외 기타 운송장비", "조선·방산·항공우주"),
    (r"기계 제조업|기계장비 및 관련 물품 도매", "기계"),
    (r"자동차", "자동차"),
    (r"석유 정제품|전기업|가스", "에너지·유틸리티"),
    (r"건설업|건축기술|공사업", "건설·인프라"),
    (r"운송업|운송관련", "운송"),
    (r"전기 통신업", "통신"),
    (r"식품|담배|도매업|중개업|개인 서비스업|소매", "소비·유통"),
)


def sector_of(industry: str | float) -> str:
    if not isinstance(industry, str):
        return "기타"
    for pattern, name in SECTOR_RULES:
        if re.search(pattern, industry):
            return name
    return "기타"


def fetch_mcap_ranked(market: str, mcap_floor_eok: float) -> pd.DataFrame:
    """시가총액 순위를 네이버 모바일 JSON API에서 받는다. market: 'KOSPI' | 'KOSDAQ'.

    예전에는 PC 웹의 시세 테이블을 HTML 파싱했는데, 코드는 정규식으로 이름·시총은 표에서
    따로 뽑아 **위치로** 짝지었다. 그 방식은 한 행만 어긋나도 뒤 전체가 밀렸다(§8 R-3).
    이 API는 한 레코드가 코드·이름·시총을 함께 주므로 어긋남이 구조적으로 불가능하다.
    (2026-10 네이버 금융 개편으로 기존 `class="tltle"` 테이블 자체가 사라졌다.)

    `stockEndType`/`tradeStopType`/`tradableStatusCode` 도 함께 와서 §5 1단계
    (데이터 소스가 주는 거래정지 정보)를 여기서 처리한다.
    """
    rows, page = [], 1
    while True:
        url = (f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
               f"?page={page}&pageSize=100")
        payload = json.loads(http_get(url).decode("utf-8"))
        stocks = payload.get("stocks") or []
        if not stocks:
            break
        page_min_cap = None
        for st in stocks:
            raw_cap = st.get("marketValueRaw")
            cap_eok = float(raw_cap) / 1e8 if raw_cap not in (None, "") else float("nan")
            if page_min_cap is None or (cap_eok == cap_eok and cap_eok < page_min_cap):
                page_min_cap = cap_eok
            # 일반 주식만. ETF/ETN/ELW 는 stockEndType 으로 소스가 직접 구분해 준다.
            if st.get("stockEndType") != "stock":
                continue
            stop = (st.get("tradeStopType") or {})
            raw_val = st.get("accumulatedTradingValueRaw")
            rows.append({
                "code": str(st.get("itemCode", "")).zfill(6),
                "name": (st.get("stockName") or "").strip(),
                "mcap_eok": cap_eok,
                "trading_value_won": float(raw_val) if raw_val not in (None, "") else float("nan"),
                "market": market,
                # §5 1단계: 소스가 주는 거래 상태. TRADING / ok 가 아니면 정상 거래가 아니다.
                "source_halted": (stop.get("name") not in (None, "TRADING"))
                                 or (st.get("tradableStatusCode") not in (None, "ok")),
            })
        if page_min_cap is not None and page_min_cap < mcap_floor_eok:
            break
        if len(stocks) < 100:
            break
        page += 1
        time.sleep(0.15)
    if not rows:
        raise RuntimeError(
            f"{market} 시총 순위를 한 건도 받지 못했다 — 네이버 API 응답 형식이 바뀌었는지 확인할 것"
        )
    out = pd.DataFrame(rows)
    out = out[out["mcap_eok"] >= mcap_floor_eok].reset_index(drop=True)
    return out


def build_universe() -> tuple[pd.DataFrame, dict]:
    report = {}
    kospi_cap = fetch_mcap_ranked("KOSPI", CONFIG["MCAP_MIN_EOK"])
    kosdaq_cap = fetch_mcap_ranked("KOSDAQ", CONFIG["MCAP_MIN_EOK"])
    cap_df = pd.concat([kospi_cap, kosdaq_cap], ignore_index=True)
    report["mcap_floor_pass"] = len(cap_df)

    # §5 1단계: 소스가 거래정지/관리 상태를 알려주면 그대로 쓴다 (휴리스틱보다 우선).
    src_halted = cap_df[cap_df["source_halted"]]
    cap_df = cap_df[~cap_df["source_halted"]].reset_index(drop=True)
    report["excluded_source_halted"] = src_halted["name"].tolist()

    name_excluded = cap_df[cap_df["name"].apply(is_excluded_name)]
    cap_df = cap_df[~cap_df["code"].isin(name_excluded["code"])].reset_index(drop=True)
    report["excluded_by_name_pattern"] = name_excluded["name"].tolist()

    # §3-1: 종목코드 끝자리로도 우선주를 건다. 이름 규칙이 놓치는 변형을 잡는다.
    code_pref = cap_df[cap_df["code"].apply(is_preferred_code)]
    cap_df = cap_df[~cap_df["code"].isin(code_pref["code"])].reset_index(drop=True)
    report["excluded_preferred_by_code"] = code_pref["name"].tolist()

    # §5 2단계: 수동 제외 목록
    manual = load_manual_excludes()
    if manual:
        hit = cap_df[cap_df["code"].isin(manual)]
        cap_df = cap_df[~cap_df["code"].isin(manual)].reset_index(drop=True)
        report["excluded_manual"] = hit["name"].tolist()
    else:
        report["excluded_manual"] = []

    kospi_list = fetch_kind_listing("stockMkt")
    kosdaq_list = fetch_kind_listing("kosdaqMkt")
    listing = pd.concat([kospi_list, kosdaq_list], ignore_index=True).drop_duplicates("code")
    merged = cap_df.merge(listing[["code", "listing_date", "industry"]], on="code", how="left")
    merged["sector"] = merged["industry"].map(sector_of)

    today = pd.Timestamp.now().normalize()
    merged["listing_days"] = (today - merged["listing_date"]).dt.days
    not_in_kind = merged[merged["listing_date"].isna()]  # ETF/ETN/펀드 등 KIND 상장법인 목록에 없는 상품 (일반 기업이 아님)
    report["excluded_not_a_company"] = not_in_kind["name"].tolist()
    too_new = merged[merged["listing_date"].notna() & (merged["listing_days"] < CONFIG["MIN_LISTING_DAYS"])]
    report["excluded_too_new"] = too_new["name"].tolist()
    merged = merged[merged["listing_date"].notna() & ~(merged["listing_days"] < CONFIG["MIN_LISTING_DAYS"])].reset_index(drop=True)
    merged["listing_date"] = merged["listing_date"].dt.strftime("%Y-%m-%d")

    report["final_universe_size"] = len(merged)
    return merged, report


# ============================== price history ================================

def naver_daily(code: str, start: str, end: str) -> pd.DataFrame:
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}&requestType=1"
           f"&startTime={start}&endTime={end}&timeframe=day")
    text = http_get_text(url, "utf-8")
    rows = re.findall(r'\["(\d{8})",\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)', text)
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]).set_index("date")
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    return df.set_index("date").sort_index()


def update_cache(code: str) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{code}.parquet"
    today = datetime.now().strftime("%Y%m%d")
    if path.exists():
        cached = pd.read_parquet(path)
        last = cached.index.max()
        start = (last - timedelta(days=5)).strftime("%Y%m%d")  # small overlap in case of late revisions
        fresh = naver_daily(code, start, today)
        combined = pd.concat([cached, fresh])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        start = (datetime.now() - timedelta(days=int(365.25 * CONFIG["HISTORY_YEARS"]))).strftime("%Y%m%d")
        combined = naver_daily(code, start, today)
    combined.to_parquet(path)
    return combined


# =============================== indicators ==================================

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    for p in CONFIG["MA_PERIODS"]:
        x[f"ma{p}"] = x["close"].rolling(p).mean()
    slope_lb = CONFIG["MA60_SLOPE_LOOKBACK"]
    x["ma60_slope"] = x["ma60"] / x["ma60"].shift(slope_lb) - 1.0
    x["ma60_slope_up"] = x["ma60_slope"] > 0
    dma = CONFIG["DISPARITY_MA"]
    x["disparity"] = x["close"] / x[f"ma{dma}"]
    p5, p20, p60, p120 = CONFIG["MA_PERIODS"]
    for pnum in CONFIG["MA_PERIODS"]:
        x[f"disp{pnum}"] = x["close"] / x[f"ma{pnum}"]

    # 정배열. MA5 vs MA20 에만 완화 계수를 둔다 — 되돌림 중 MA5가 MA20 아래로 잠깐
    # 내려가는 것은 정상이고, 여기서 자르면 눌림이 깊은 종목이 되돌림 조건에 닿지 못한다(§3-2).
    tol = CONFIG["MA5_ALIGN_TOLERANCE"]
    x["is_stacked"] = ((x[f"ma{p5}"] > x[f"ma{p20}"] * tol)
                       & (x[f"ma{p20}"] > x[f"ma{p60}"])
                       & (x[f"ma{p60}"] > x[f"ma{p120}"]))

    # 이격도 밴드: 상한(과열) + 하한(추세 훼손). 장기선일수록 폭이 넓다.
    disp_ok = (
        (x["disp20"] <= CONFIG["DISPARITY_MAX_MA20"]) & (x["disp20"] >= CONFIG["DISPARITY_MIN_MA20"])
        & (x["disp60"] <= CONFIG["DISPARITY_MAX_MA60"]) & (x["disp60"] >= CONFIG["DISPARITY_MIN_MA60"])
        & (x["disp120"] <= CONFIG["DISPARITY_MAX_MA120"])
    )
    x["disparity_ok"] = disp_ok
    x["is_uptrend"] = x["is_stacked"] & x["ma60_slope_up"] & disp_ok

    # 장기 상대수익률 (추세 강도 점수용)
    x["long_return"] = x["close"] / x["close"].shift(CONFIG["TREND_RETURN_LOOKBACK"]) - 1.0

    w = CONFIG["HIGH_LOOKBACK"]
    prior_close = x["close"].shift(1)
    x["h20"] = prior_close.rolling(w).max()
    x["l20"] = prior_close.rolling(w).min()
    # 구 정의: 분모가 20일 창 '전체'의 최저값. 고점보다 뒤에 있는 저점이 분모에 섞일 수 있어
    # 되돌림률과 반등률이 한 컬럼에 뒤엉킨다. INV-3 하위호환용으로 값만 병기 보존한다(§2).
    rng_legacy = x["h20"] - x["l20"]
    x["retrace_ratio_legacy"] = np.where(rng_legacy > 0, (x["h20"] - x["close"]) / rng_legacy, np.nan)

    # INV-7: 창을 고점 기준으로 둘로 쪼갠다 (§2 의사코드).
    #   ① L_leg  = min(close[t-LEG_LOOKBACK : H_date+1])  상승 다리의 시작 저점 -> retrace_ratio 분모
    #   ③ L_pull = min(close[H_date : t+1])               눌림 바닥 (당일 포함)
    # 다리 탐색 창은 고점 탐색 창보다 넓다(R-4). 같으면 다리가 창 경계에서 잘린다.
    # days_since_high20을 구하던 루프에서 고점 인덱스를 그대로 재사용한다.
    lw = CONFIG["LEG_LOOKBACK"]
    close_arr = x["close"].to_numpy(dtype=float)
    n = len(x)
    days_since_high = np.full(n, np.nan)
    l_leg = np.full(n, np.nan)
    l_leg_age = np.full(n, np.nan)      # 기준일로부터 L_leg까지의 거래일 수 (차트 ① 마커용)
    l_pull = np.full(n, np.nan)
    days_since_pullback_low = np.full(n, np.nan)
    for j in range(w + 1, n):
        window = close_arr[j - w:j]  # matches prior_close.shift/rolling alignment
        hi = j - w + int(np.argmax(window))            # ② H_date (절대 인덱스)
        days_since_high[j] = j - hi
        leg_start = max(0, j - lw)                     # ① 탐색 시작 (고점 창보다 넓다)
        li = leg_start + int(np.argmin(close_arr[leg_start:hi + 1]))
        l_leg[j] = close_arr[li]
        l_leg_age[j] = j - li
        seg = close_arr[hi:j + 1]                      # ③ 고점 '이후' 구간 (당일 포함)
        lpi = hi + int(np.argmin(seg))
        l_pull[j] = close_arr[lpi]
        days_since_pullback_low[j] = j - lpi           # 0이면 오늘이 바닥 -> 탈락
    x["days_since_high20"] = days_since_high
    x["l_leg"] = l_leg
    x["days_since_leg_low"] = l_leg_age
    x["l_pull"] = l_pull
    x["days_since_pullback_low"] = days_since_pullback_low

    leg = x["h20"] - x["l_leg"]                        # 상승 다리 폭
    x["retrace_ratio"] = np.where(leg > 0, (x["h20"] - x["close"]) / leg, np.nan)
    x["range_pct"] = np.where(x["l_leg"] > 0, leg / x["l_leg"], np.nan)
    x["bounce_from_low"] = np.where(x["l_pull"] > 0, (x["close"] - x["l_pull"]) / x["l_pull"], np.nan)
    x["dd_from_high"] = np.where(x["h20"] > 0, (x["h20"] - x["close"]) / x["h20"], np.nan)

    # 최근 N일(당일 포함) 최저 종가인가 -- ③→④ 반등 보조 조건
    nl = CONFIG["NOT_LOWEST_IN_DAYS"]
    x["is_lowest_recent"] = x["close"] <= x["close"].rolling(nl).min()

    # 거래대금은 종가 x 거래량 근사다(§6: 근사를 쓰면 명시할 것). 컬럼명에 근사임을 남긴다.
    x["trading_value_eok"] = x["close"] * x["volume"] / 1e8

    # --- 관리종목/거래정지/이상급등 휴리스틱 (§5 3단계) ---
    # 거래대금 0원인 날이 최근 N일 안에 있으면 거래정지로 간주한다.
    zero_days = (x["trading_value_eok"].fillna(0) <= 0).rolling(
        CONFIG["HALT_LOOKBACK_DAYS"], min_periods=1).sum()
    x["halted"] = zero_days >= CONFIG["HALT_ZERO_VALUE_DAYS"]
    thr = CONFIG["EXTREME_MOVE_THRESHOLD"]
    if thr and thr > 0:
        x["extreme_move"] = (x["close"].pct_change().abs() >= thr).rolling(
            CONFIG["EXTREME_MOVE_LOOKBACK"], min_periods=1).max().astype(bool)
    else:
        x["extreme_move"] = False

    # --- value_surge: 고점 직전 상승구간 거래대금 / 60일 평균 (수급 유입) ---
    # 상승 구간을 고점 직전 ADVANCE_VOL_DAYS 로 좁게 잡는다. 20일로 넓히면 되돌림 이전
    # 구간까지 섞여 상승 구간이 희석된다(§4).
    avg_value_long = x["trading_value_eok"].rolling(
        CONFIG["VALUE_SURGE_LOOKBACK"], min_periods=20).mean()
    val_arr = x["trading_value_eok"].to_numpy(dtype=float)
    vol_arr = x["volume"].to_numpy(dtype=float)
    adv_days = CONFIG["ADVANCE_VOL_DAYS"]
    rally_value = np.full(len(x), np.nan)
    rally_volume = np.full(len(x), np.nan)
    pull_volume = np.full(len(x), np.nan)
    for j in range(w + 1, n):
        if np.isnan(days_since_high[j]):
            continue
        hj = j - int(days_since_high[j])              # ② 고점 인덱스
        rs = max(0, hj - (adv_days - 1))
        rally_value[j] = np.nanmean(val_arr[rs:hj + 1])      # 상승 구간 거래대금
        rally_volume[j] = np.nanmean(vol_arr[rs:hj + 1])     # 상승 구간 거래량
        if hj + 1 <= j:
            pull_volume[j] = np.nanmean(vol_arr[hj + 1:j + 1])  # 눌림 구간 거래량
    x["rally_value_eok"] = rally_value
    with np.errstate(invalid="ignore", divide="ignore"):
        x["value_surge"] = rally_value / avg_value_long.to_numpy(dtype=float)
        x["vol_dryup_ratio"] = pull_volume / rally_volume

    ema12 = x["close"].ewm(span=12, adjust=False).mean()
    ema26 = x["close"].ewm(span=26, adjust=False).mean()
    x["macd"] = ema12 - ema26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    # INV-1: 신호 계산은 종가 전용이다. 장중 고가/저가를 '쓰지 않는다'를 주석이 아니라
    # 구조로 강제한다 — 여기서 떨어뜨리면 하류 코드가 접근할 방법 자체가 없어진다.
    # 캔들 차트처럼 OHLC 가 필요한 표시 계층은 compute_features() 이전의 원본을 쓴다.
    return x.drop(columns=[c for c in ("high", "low") if c in x.columns])


def detect_price_discontinuity(df: pd.DataFrame) -> list[tuple[str, float]]:
    """미조정 액면분할/무상증자 의심 지점 탐지 (INV-6 가드).

    KRX 일간 가격제한폭이 ±30%이므로, 종가 대비 종가 비율이 그 밖으로 벗어나면
    실제 거래로는 설명되지 않는다. 남는 설명은 주식 수 변경이 가격에 반영되지
    않은 경우이고, 그 상태로 두면 H·L·이동평균이 오염되어 가짜 되돌림이 만들어진다.
    """
    if len(df) < 2:
        return []
    ratio = df["close"] / df["close"].shift(1)
    bad = ratio[(ratio > CONFIG["PRICE_GAP_MAX_RATIO"]) | (ratio < CONFIG["PRICE_GAP_MIN_RATIO"])]
    return [(d.strftime("%Y-%m-%d"), round(float(v), 4)) for d, v in bad.items() if not pd.isna(v)]


def freshness_score(days: float) -> float:
    """고점 이후 경과일 점수. 이상 구간에서 만점, 벗어날수록 감점.

    '짧을수록 좋다'로 두면 고점 바로 다음날이 만점을 받아, 되돌림이 시작도 안 된
    종목이 점수 상위를 차지한다(§4). 그래서 고원(plateau)형으로 만든다.
    """
    lo, hi = CONFIG["FRESHNESS_IDEAL_MIN"], CONFIG["FRESHNESS_IDEAL_MAX"]
    if lo <= days <= hi:
        return 1.0
    if days < lo:
        return float(np.clip(1.0 - (lo - days) / lo, 0.0, 1.0))
    span = max(CONFIG["DAYS_SINCE_HIGH_MAX"] - hi, 1)
    return float(np.clip(1.0 - (days - hi) / span, 0.0, 1.0))


def peak_date_of(x: pd.DataFrame, j: int) -> str | None:
    """기준일 j의 20일 고점이 형성된 날짜. 점수 산출과 무관하게 항상 필요하다."""
    d = x["days_since_high20"].iloc[j]
    if pd.isna(d):
        return None
    peak_i = j - int(d)
    return x.index[peak_i].strftime("%Y-%m-%d") if 0 <= peak_i < len(x) else None


def structure_points(x: pd.DataFrame, j: int) -> dict:
    """INV-7의 ①②③④ 네 지점을 (날짜, 값, 경과 거래일)로 돌려준다.

    화면 표시 전용이지만 좌표는 반드시 판정과 같은 컬럼에서 뽑는다 — 차트가 그리는 점과
    스크리너가 계산한 값이 갈라지면 그림이 결함을 감춘다(R-4).
    ②는 **종가** 고점이다. 장중 고가가 더 높은 봉과 다를 수 있고 그게 정상이다(INV-1).
    """
    def at(age) -> tuple[str | None, float | None]:
        if pd.isna(age):
            return None, None
        i = j - int(age)
        if not (0 <= i < len(x)):
            return None, None
        return x.index[i].strftime("%Y-%m-%d"), float(x["close"].iloc[i])

    leg_age = x["days_since_leg_low"].iloc[j]
    high_age = x["days_since_high20"].iloc[j]
    pull_age = x["days_since_pullback_low"].iloc[j]
    leg_date, leg_close = at(leg_age)
    high_date, high_close = at(high_age)
    pull_date, pull_close = at(pull_age)

    def age_int(v):
        return None if pd.isna(v) else int(v)

    # 탐색 창의 시작 날짜 — 차트 음영용. 어느 구간에서 뽑은 값인지 보이게 한다.
    def win_start(n: int) -> str:
        return x.index[max(0, j - n)].strftime("%Y-%m-%d")

    la, ha, pa = age_int(leg_age), age_int(high_age), age_int(pull_age)
    # ① < ② < ③ ≤ ④ (④는 기준일이므로 경과일 0). 경과일은 클수록 과거다.
    order_ok = None if None in (la, ha, pa) else bool(la > ha > pa >= 0)

    return dict(
        leg_date=leg_date, leg_close=leg_close, leg_age=la,
        high_date=high_date, high_close=high_close, high_age=ha,
        pull_date=pull_date, pull_close=pull_close, pull_age=pa,
        base_date=x.index[j].strftime("%Y-%m-%d"), base_close=float(x["close"].iloc[j]),
        high_win_start=win_start(CONFIG["HIGH_LOOKBACK"]),
        leg_win_start=win_start(CONFIG["LEG_LOOKBACK"]),
        order_ok=order_ok,
    )


def _lin(value: float, full: float, zero: float) -> float:
    """value 가 full 이면 1점, zero 면 0점인 선형 점수. full>zero / full<zero 둘 다 지원."""
    if pd.isna(value):
        return float("nan")
    if full == zero:
        return 1.0
    return float(np.clip((zero - value) / (zero - full), 0.0, 1.0))


def score_row(x: pd.DataFrame, j: int) -> dict:
    """6개 항목을 0~1로 정규화해 가중합한 뒤 100점 만점으로 환산한다(§4).

    각 항목의 기준값은 전부 CONFIG 에 있다(INV-5). 결측은 0점으로 두되, 원시 지표는
    별도 컬럼으로 함께 돌려줘 왜 그 점수가 나왔는지 역추적할 수 있게 한다.
    """
    row = x.iloc[j]
    comp: dict[str, float] = {}

    # 1) 거래대금 증가율 — 고점 직전 상승구간 / 60일 평균
    value_surge = row.get("value_surge", np.nan)
    comp["value_surge"] = 0.0 if pd.isna(value_surge) else float(
        np.clip(value_surge / CONFIG["VALUE_SURGE_FULL"], 0, 1))

    # 2) 눌림 구간 거래량 감소 — 낮을수록 건전
    dry = row.get("vol_dryup_ratio", np.nan)
    comp["volume_dryup"] = _lin(dry, CONFIG["VOLUME_DRYUP_FULL"], CONFIG["VOLUME_DRYUP_ZERO"])

    # 3) 20일선 근접도
    disp = row.get("disparity", np.nan)
    dist = abs(disp - 1.0) if not pd.isna(disp) else np.nan
    comp["ma20_proximity"] = _lin(dist, CONFIG["MA20_PROXIMITY_FULL"], CONFIG["MA20_PROXIMITY_ZERO"])

    # 4) 고점 후 경과일 — 고원형(§4에서 확정된 형태, freshness_score 가 단일 출처)
    fresh = row.get("days_since_high20", np.nan)
    comp["freshness"] = 0.5 if pd.isna(fresh) else freshness_score(float(fresh))

    # 5) 되돌림비율 품질 — 피보나치 0.45 부근이면 만점, 가우시안 감점
    rr = row.get("retrace_ratio", np.nan)
    comp["retrace_quality"] = 0.0 if pd.isna(rr) else float(
        np.exp(-0.5 * ((rr - CONFIG["RETRACE_IDEAL"]) / CONFIG["RETRACE_SIGMA"]) ** 2))

    # 6) 추세 강도 — 60일선 기울기 + 120일 상대수익률
    slope = row.get("ma60_slope", np.nan)
    lret = row.get("long_return", np.nan)
    a = 0.0 if pd.isna(slope) else float(np.clip(slope / CONFIG["TREND_SLOPE_FULL"], 0, 1))
    b = 0.0 if pd.isna(lret) else float(np.clip(lret / CONFIG["TREND_RETURN_FULL"], 0, 1))
    comp["trend_strength"] = 0.5 * a + 0.5 * b

    weights = {
        "value_surge": CONFIG["WEIGHT_VALUE_SURGE"],
        "volume_dryup": CONFIG["WEIGHT_VOLUME_DRYUP"],
        "ma20_proximity": CONFIG["WEIGHT_MA20_PROXIMITY"],
        "freshness": CONFIG["WEIGHT_FRESHNESS"],
        "retrace_quality": CONFIG["WEIGHT_RETRACE_QUALITY"],
        "trend_strength": CONFIG["WEIGHT_TREND_STRENGTH"],
    }
    total_w = sum(weights.values())
    acc = 0.0
    parts = {}
    for key, wgt in weights.items():
        v = comp[key]
        v = 0.0 if (v is None or pd.isna(v)) else float(v)
        parts[f"s_{key}"] = round(v * 100, 1)
        acc += wgt * v

    peak_i = j - int(row["days_since_high20"]) if not pd.isna(row["days_since_high20"]) else None
    return dict(
        score=round(acc / total_w * 100, 2),
        value_surge=None if pd.isna(value_surge) else round(float(value_surge), 2),
        vol_dryup_ratio=None if pd.isna(dry) else round(float(dry), 2),
        long_return_pct=None if pd.isna(lret) else round(float(lret) * 100, 1),
        ma60_slope_pct=None if pd.isna(slope) else round(float(slope) * 100, 2),
        peak_date=x.index[peak_i].strftime("%Y-%m-%d") if peak_i is not None else None,
        **parts,
    )


def structure_verdict(row: pd.Series) -> dict:
    """INV-7: 가격이 ① L_leg -> ② H -> ③ L_pull -> ④ 오늘 순서를 지나왔는지 판정한다.

    screen_on_date()와 대시보드가 같은 함수를 쓰게 해서 CSV와 대시보드 판정이 갈라지지
    않게 한다(INV-4와 같은 이유). 탈락시키더라도 지표 컬럼 자체는 호출측에서 남긴다(INV-3).
    """
    reasons: list[str] = []

    rp = row["range_pct"]
    if pd.isna(rp) or rp < CONFIG["RANGE_PCT_MIN"]:
        shown = "N/A" if pd.isna(rp) else f"{rp * 100:.1f}%"
        reasons.append(f"상승 다리 {shown} (최소 {CONFIG['RANGE_PCT_MIN'] * 100:.0f}%)")

    dspl = row["days_since_pullback_low"]
    if pd.isna(dspl) or dspl < CONFIG["PULLBACK_LOW_MIN_AGE"]:
        reasons.append("오늘이 눌림 바닥 (반등 미확인)")

    if bool(row["is_lowest_recent"]):
        reasons.append(f"최근 {CONFIG['NOT_LOWEST_IN_DAYS']}일 최저 종가")

    return dict(ok=not reasons, reasons=reasons)


def screen_funnel(rows: list[dict]) -> dict[str, int]:
    """되돌림 단계 필터를 순서대로 얹으며 각 단계 잔존 수를 센다 (§3).

    어느 조건이 몇 종목을 잘랐는지 보이지 않으면 튜닝이 불가능하다. 콘솔·CSV·HTML 이
    전부 이 함수를 써서 같은 숫자를 쓴다.
    """
    C = CONFIG
    stages = [
        (f"고점경과일 {C['DAYS_SINCE_HIGH_MIN']}~{C['DAYS_SINCE_HIGH_MAX']}일",
         lambda r: r["passes_days_since_high"]),
        (f"되돌림비율 {C['RETRACE_MIN']}~{C['RETRACE_MAX']}",
         lambda r: r["passes_retrace_band"]),
        (f"고점 대비 낙폭 {C['DD_FROM_HIGH_MAX'] * 100:.0f}% 이내",
         lambda r: r["passes_dd_from_high"]),
        (f"상승 다리 {C['RANGE_PCT_MIN'] * 100:.0f}% 이상",
         lambda r: (r["range_pct"] is not None) and r["range_pct"] >= C["RANGE_PCT_MIN"]),
        ("오늘이 눌림 바닥 아님",
         lambda r: (r["days_since_pullback_low"] is not None)
         and r["days_since_pullback_low"] >= C["PULLBACK_LOW_MIN_AGE"]),
        (f"최근 {C['NOT_LOWEST_IN_DAYS']}일 최저 종가 아님", lambda r: r["passes_structure"]),
    ]
    out, cur = {}, list(rows)
    for label, keep in stages:
        cur = [r for r in cur if keep(r)]
        out[label] = len(cur)
    return out


def screen_on_date(feat: pd.DataFrame, date: pd.Timestamp, meta: dict) -> dict | None:
    if date not in feat.index:
        return None
    j = feat.index.get_loc(date)
    row = feat.iloc[j]
    if pd.isna(row["retrace_ratio"]) or not row["is_uptrend"]:
        return None
    avg_val20 = feat["trading_value_eok"].iloc[max(0, j - 19):j + 1].mean()
    if avg_val20 < CONFIG["MIN_AVG_TRADING_VALUE_EOK"]:
        return None
    # §5 3단계 휴리스틱. 유니버스 단계의 조건이므로 행 자체를 떨어뜨린다(정배열·유동성과 동일).
    if bool(row.get("halted", False)) or bool(row.get("extreme_move", False)):
        return None
    passes_band = CONFIG["RETRACE_MIN"] <= row["retrace_ratio"] <= CONFIG["RETRACE_MAX"]

    # INV-4: 고점 경과일이 범위 밖이면 후보에서 탈락시킨다. 행 자체는 남겨서
    # retrace_ratio(INV-3)와 탈락 사유를 계속 볼 수 있게 한다.
    dsh = row["days_since_high20"]
    dsh_int = None if pd.isna(dsh) else int(dsh)
    passes_days = dsh_int is not None and (
        CONFIG["DAYS_SINCE_HIGH_MIN"] <= dsh_int <= CONFIG["DAYS_SINCE_HIGH_MAX"]
    )
    struct = structure_verdict(row)  # INV-7: ①→②→③→④ 순서 확인

    # §3-3: 고점 대비 낙폭 상한. 되돌림비율은 상대값이라 같은 0.4대에서도 실제 낙폭이
    # 크게 벌어진다. 절대 낙폭이 이 선을 넘으면 되돌림이 아니라 추세 전환으로 본다.
    dd = row["dd_from_high"]
    passes_dd = (not pd.isna(dd)) and dd <= CONFIG["DD_FROM_HIGH_MAX"]

    reasons = []
    if not passes_dd:
        shown = "N/A" if pd.isna(dd) else f"{dd * 100:.1f}%"
        reasons.append(f"고점 대비 낙폭 {shown} (상한 {CONFIG['DD_FROM_HIGH_MAX'] * 100:.0f}%)")
    if not passes_days:
        reasons.append(
            f"고점경과일 {dsh_int}일 (허용 {CONFIG['DAYS_SINCE_HIGH_MIN']}~{CONFIG['DAYS_SINCE_HIGH_MAX']}일)"
        )
    if not passes_band:
        reasons.append(
            f"되돌림비율 {row['retrace_ratio']:.3f} (허용 {CONFIG['RETRACE_MIN']}~{CONFIG['RETRACE_MAX']})"
        )
    reasons.extend(struct["reasons"])

    # 경과일 필터에 걸린 종목은 점수 계산에 도달하지 않는다(§4).
    sc = score_row(feat, j) if passes_days else dict(
        score=None, value_surge=None, vol_dryup_ratio=None, long_return_pct=None,
        ma60_slope_pct=None, peak_date=peak_date_of(feat, j),
    )

    return dict(
        code=meta["code"], name=meta["name"], market=meta["market"],
        sector=meta.get("sector") or "기타",
        date=date.strftime("%Y-%m-%d"),
        close=float(row["close"]), mcap_eok=meta.get("mcap_eok"),
        retrace_ratio=round(float(row["retrace_ratio"]), 3),
        retrace_ratio_legacy=None if pd.isna(row["retrace_ratio_legacy"]) else round(float(row["retrace_ratio_legacy"]), 3),
        passes_retrace_band=bool(passes_band),
        passes_days_since_high=bool(passes_days),
        passes_structure=bool(struct["ok"]),
        passes_dd_from_high=bool(passes_dd),
        is_candidate=bool(passes_band and passes_days and passes_dd and struct["ok"]),
        exclude_reason="; ".join(reasons) if reasons else "",
        ma5=float(row["ma5"]), ma20=float(row["ma20"]), ma60=float(row["ma60"]), ma120=float(row["ma120"]),
        disparity_vs_ma20=round(float(row["disparity"]), 3),
        days_since_high20=dsh_int,
        days_since_pullback_low=None if pd.isna(row["days_since_pullback_low"]) else int(row["days_since_pullback_low"]),
        bounce_from_low=None if pd.isna(row["bounce_from_low"]) else round(float(row["bounce_from_low"]), 4),
        dd_from_high=None if pd.isna(row["dd_from_high"]) else round(float(row["dd_from_high"]), 4),
        range_pct=None if pd.isna(row["range_pct"]) else round(float(row["range_pct"]), 4),
        avg_trading_value20_eok=round(float(avg_val20), 1),
        **sc,
    )


# ================================= report =====================================

def sparkline_svg(closes: pd.Series, up: bool) -> str:
    vals = closes.to_numpy(float)
    if len(vals) < 2:
        return ""
    lo, hi = vals.min(), vals.max()
    span = hi - lo if hi > lo else 1.0
    w, h, pad = 120, 32, 2
    xs = np.linspace(pad, w - pad, len(vals))
    ys = h - pad - (vals - lo) / span * (h - 2 * pad)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    color = "#cf2a3a" if up else "#2461e0"
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"/></svg>')


def build_html_report(results: list[dict], histories: dict, run_date: str, funnel: dict,
                      n_uptrend_detected: int, stage_funnel: dict | None = None) -> str:
    # §9 8번: 되돌림 단계 필터의 잔존 수를 퍼널에 그대로 펼친다. 어느 조건이 병목인지
    # 리포트만 보고도 알 수 있어야 한다.
    stage_html = "".join(
        f"<div>{label}<b>{n}</b></div>" for label, n in (stage_funnel or {}).items()
    )
    rows_html = []
    for r in sorted(results, key=lambda d: -d["score"]):
        hist = histories[r["code"]]
        spark_series = hist["close"].tail(CONFIG["SPARKLINE_DAYS"])
        up = spark_series.iloc[-1] >= spark_series.iloc[0]
        band = "band-ok" if r["is_candidate"] else "band-out"
        rows_html.append(f"""
        <tr>
          <td class="l">{r['name']}<br><span class="dim">{r['code']} · {r['market']}</span></td>
          <td class="r">{r['close']:,.0f}</td>
          <td class="r">{r['mcap_eok']:,.0f}억</td>
          <td class="r {band}">{r['retrace_ratio']:.2f}</td>
          <td class="r">{r['disparity_vs_ma20']:.2f}</td>
          <td class="r">MA5 {r['ma5']:,.0f} / MA20 {r['ma20']:,.0f} / MA60 {r['ma60']:,.0f} / MA120 {r['ma120']:,.0f}</td>
          <td class="r">{r['days_since_high20']}일</td>
          <td class="r">{r['avg_trading_value20_eok']:,.0f}억</td>
          <td class="r score">{r['score']:.1f}</td>
          <td>{sparkline_svg(spark_series, up)}</td>
        </tr>""")
    pass_band = sum(1 for r in results if r["is_candidate"])
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>KR 되돌림 스크리너 {run_date}</title>
<style>
body {{ font-family: -apple-system, "Apple SD Gothic Neo", sans-serif; background:#f4f5f8; color:#14161c; margin:0; padding:28px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} .sub {{ color:#5c6272; font-size:13px; margin-bottom:18px; }}
.funnel {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.funnel div {{ background:#fff; border:1px solid #dde0e8; border-radius:8px; padding:8px 12px; font-size:12.5px; }}
.funnel b {{ display:block; font-size:16px; }}
table {{ border-collapse:collapse; width:100%; background:#fff; border:1px solid #dde0e8; border-radius:10px; overflow:hidden; font-size:13px; }}
th {{ background:#ecedf3; text-align:right; padding:8px 10px; font-size:11px; color:#5c6272; text-transform:uppercase; }}
th.l {{ text-align:left; }}
td {{ padding:8px 10px; border-top:1px solid #eceef3; text-align:right; }}
td.l {{ text-align:left; }} .dim {{ color:#9096a6; font-size:11px; }}
.band-ok {{ color:#178a3c; font-weight:700; }} .band-out {{ color:#9096a6; }}
.score {{ font-weight:800; }}
.note {{ margin-top:18px; font-size:12px; color:#5c6272; line-height:1.7; background:#fff; border:1px solid #dde0e8; border-radius:8px; padding:12px 14px; }}
</style></head><body>
<h1>KR 되돌림(눌림목) 스크리너 — {run_date}</h1>
<div class="sub">정배열(5&gt;20&gt;60&gt;120) + 60일선 상승 + 이격도 {CONFIG['DISPARITY_MIN_MA20']}~{CONFIG['DISPARITY_MAX_MA20']} 종목 중, 종가 기준 20일 되돌림비율을 계산한 결과. 통과 구간({CONFIG['RETRACE_MIN']}~{CONFIG['RETRACE_MAX']}) 충족 {pass_band}건 / 정배열+눌림 감지 전체 {n_uptrend_detected}건.</div>
<div class="funnel">
  <div>시총 {CONFIG['MCAP_MIN_EOK']:,}억 이상<b>{funnel.get('mcap_floor_pass','-')}</b></div>
  <div>우선주/스팩/리츠 제외 후<b>{funnel.get('mcap_floor_pass',0) - len(funnel.get('excluded_by_name_pattern',[]))}</b></div>
  <div>ETF 등 비상장법인 제외 후<b>{funnel.get('mcap_floor_pass',0) - len(funnel.get('excluded_by_name_pattern',[])) - len(funnel.get('excluded_not_a_company',[]))}</b></div>
  <div>상장 60일 미만 제외 후<b>{funnel.get('final_universe_size','-')}</b></div>
  <div>정배열+눌림 감지<b>{n_uptrend_detected}</b></div>
  {stage_html}
</div>
<table><thead><tr>
<th class="l">종목</th><th>종가</th><th>시가총액</th><th>되돌림비율</th><th>이격도(20D)</th><th>이동평균</th><th>고점 후 경과</th><th>20일평균거래대금</th><th>점수</th><th>최근 {CONFIG['SPARKLINE_DAYS']}일</th>
</tr></thead><tbody>
{''.join(rows_html) if rows_html else '<tr><td colspan="10" style="text-align:center;padding:30px;color:#9096a6">조건을 만족하는 종목이 없습니다.</td></tr>'}
</tbody></table>
<div class="note">
데이터 출처: KRX 정보데이터시스템(data.krx.co.kr)이 무인증 통계 API를 로그인 필수로 전환해 pykrx가 더 이상 동작하지 않고, 이 샌드박스에서는 FinanceDataReader 설치도 되지 않아(PyPI 인덱스에 해당 배포본 없음) 두 방법 모두 실패했습니다. 대신 네이버 금융의 공개 엔드포인트로 대체했습니다.
20일 평균 거래대금은 (종가×거래량) 근사치이며 실제 체결 거래대금과는 소폭 차이가 있을 수 있습니다. 가격은 액면분할·무상증자가 반영된 수정주가이며(배당은 미반영), 매 실행마다 ±30% 가격제한폭을 벗어나는 종가 불연속이 있는지 검사해 해당 종목은 제외합니다. 이 화면은 투자 판단을 보조하는 참고 자료입니다.
</div>
</body></html>"""


def spot_check(results: list[dict], histories: dict, n: int = 3) -> str:
    lines = []
    for r in sorted(results, key=lambda d: -d["score"])[:n]:
        h = histories[r["code"]]
        peak_date = pd.Timestamp(r["peak_date"]) if r.get("peak_date") else None
        if peak_date is None or peak_date not in h.index:
            continue
        j_peak = h.index.get_loc(peak_date)
        j_now = h.index.get_loc(pd.Timestamp(r["date"]))
        peak_close = h["close"].iloc[j_peak]
        low_since = h["close"].iloc[j_peak:j_now + 1].min()
        low_date = h["close"].iloc[j_peak:j_now + 1].idxmin().strftime("%Y-%m-%d")
        now_close = h["close"].iloc[j_now]
        drop_pct = (peak_close - low_since) / peak_close * 100
        bounce_pct = (now_close - low_since) / low_since * 100
        lines.append(
            f"- {r['name']}({r['code']}): 고점 {peak_date.strftime('%Y-%m-%d')} 종가 {peak_close:,.0f}원 -> "
            f"저점 {low_date} 종가 {low_since:,.0f}원 (고점 대비 -{drop_pct:.1f}%) -> "
            f"현재({r['date']}) {now_close:,.0f}원 (저점 대비 +{bounce_pct:.1f}%), "
            f"되돌림비율 {r['retrace_ratio']:.2f}, 정배열 유지, 점수 {r['score']:.1f}"
        )
    return "\n".join(lines) if lines else "(상위 결과 없음)"


# ================================== main ======================================

def fetch_status_flags(code: str) -> list[str]:
    try:
        raw = http_get(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=10)
        d = json.loads(raw)
    except Exception:
        return []
    flags = []
    for key in ("iconInfos", "description"):
        val = d.get(key)
        if not val:
            continue
        text = json.dumps(val, ensure_ascii=False)
        for kw in ("관리", "거래정지", "정지", "투자경고", "투자주의", "투자위험", "환기"):
            if kw in text:
                flags.append(kw)
    return sorted(set(flags))


def main():
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("[1/5] 유니버스 구성 중 (시가총액 랭킹 페이지네이션)...")
    universe, funnel = build_universe()
    print(f"      시총 {CONFIG['MCAP_MIN_EOK']:,}억+ : {funnel['mcap_floor_pass']}종목")
    print(f"      이름패턴 제외(우선주/스팩/리츠): {len(funnel['excluded_by_name_pattern'])}종목 -> {funnel['excluded_by_name_pattern'][:10]}{'...' if len(funnel['excluded_by_name_pattern'])>10 else ''}")
    print(f"      ETF/ETN 등 비상장법인 제외: {len(funnel['excluded_not_a_company'])}종목")
    print(f"      상장 60일 미만 제외: {len(funnel['excluded_too_new'])}종목")
    print(f"      최종 유니버스: {funnel['final_universe_size']}종목")

    print("[2/5] 관리종목/거래정지 의심 플래그 확인 + 상태 필터링...")
    status_excluded = []
    keep_rows = []
    for _, row in universe.iterrows():
        flags = fetch_status_flags(row["code"])
        if flags:
            status_excluded.append((row["name"], flags))
        else:
            keep_rows.append(row)
        time.sleep(0.08)
    universe = pd.DataFrame(keep_rows).reset_index(drop=True)
    print(f"      상태 플래그로 제외: {len(status_excluded)}종목 -> {status_excluded[:10]}")
    print(f"      스크리닝 대상: {len(universe)}종목")

    print("[3/5] 종목별 3년치 일봉 캐시 갱신 중 (최초 실행이면 시간이 걸립니다)...")
    histories: dict[str, pd.DataFrame] = {}
    metas: dict[str, dict] = {}
    failed_fetch = []
    discontinuity_excluded = []
    for i, (_, row) in enumerate(universe.iterrows(), 1):
        code = row["code"]
        try:
            hist = update_cache(code)
            if len(hist) < 130:
                failed_fetch.append((row["name"], "history too short"))
                continue
            gaps = detect_price_discontinuity(hist)          # INV-6 가드
            if gaps:
                discontinuity_excluded.append((row["name"], code, gaps[:3]))
                continue
            histories[code] = compute_features(hist)
            metas[code] = row.to_dict()
        except Exception as e:
            failed_fetch.append((row["name"], str(e)[:120]))
        if i % 20 == 0:
            print(f"      {i}/{len(universe)} 완료...")
        time.sleep(0.05)
    print(f"      데이터 확보 실패: {len(failed_fetch)}종목 -> {failed_fetch[:10]}")
    print(f"      가격 불연속(미조정 분할 의심) 제외: {len(discontinuity_excluded)}종목 -> {discontinuity_excluded[:5]}")
    print(f"      최종 분석 대상: {len(histories)}종목")

    # pick the run date = most common last index among all histories
    last_dates = pd.Series([h.index.max() for h in histories.values()])
    run_date = last_dates.mode().iloc[0]
    stale = [c for c, h in histories.items() if h.index.max() < run_date]
    print(f"[4/5] 기준일 {run_date.date()} 로 스크리닝 (최신 데이터 없는 {len(stale)}종목은 제외)")

    all_candidates = []
    for code, feat in histories.items():
        if code in stale:
            continue
        r = screen_on_date(feat, run_date, metas[code])
        if r:
            all_candidates.append(r)
    final = [r for r in all_candidates if r["is_candidate"]]
    n_band = sum(1 for r in all_candidates if r["passes_retrace_band"])
    n_days_out = sum(1 for r in all_candidates if not r["passes_days_since_high"])
    stage_funnel = screen_funnel(all_candidates)
    print(f"      정배열+눌림 감지: {len(all_candidates)}종목")
    for label, n in stage_funnel.items():
        print(f"        {label}: {n}종목")
    print(f"      최종 후보: {len(final)}종목")

    date_str = run_date.strftime("%Y%m%d")
    csv_path = OUT_DIR / f"kr_pullback_{date_str}.csv"
    # CSV에는 탈락 종목도 남긴다 (INV-3: 밴드 밖이어도 retrace_ratio는 항상 보존).
    csv_rows = sorted(all_candidates, key=lambda d: (not d["is_candidate"], -(d["score"] or 0)))
    pd.DataFrame(csv_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    # §9 8번: 단계별 퍼널을 산출물로도 남긴다. 콘솔 로그만으로는 어제와 비교할 수 없다.
    funnel_rows = [{"단계": "시총 하한 통과", "잔존": funnel["mcap_floor_pass"]}]
    funnel_rows += [
        {"단계": "우선주/스팩/리츠 이름패턴 제외", "잔존": None, "제외": len(funnel["excluded_by_name_pattern"])},
        {"단계": "우선주 종목코드 제외", "잔존": None, "제외": len(funnel.get("excluded_preferred_by_code", []))},
        {"단계": "수동 제외목록", "잔존": None, "제외": len(funnel.get("excluded_manual", []))},
        {"단계": "ETF/ETN 등 비상장법인 제외", "잔존": None, "제외": len(funnel["excluded_not_a_company"])},
        {"단계": "상장 60일 미만 제외", "잔존": None, "제외": len(funnel["excluded_too_new"])},
        {"단계": "최종 유니버스", "잔존": funnel["final_universe_size"]},
        {"단계": "정배열+눌림 감지", "잔존": len(all_candidates)},
    ]
    funnel_rows += [{"단계": k, "잔존": v} for k, v in stage_funnel.items()]
    funnel_rows.append({"단계": "최종 후보", "잔존": len(final)})
    funnel_csv = OUT_DIR / f"kr_pullback_funnel_{date_str}.csv"
    pd.DataFrame(funnel_rows).to_csv(funnel_csv, index=False, encoding="utf-8-sig")

    html_path = OUT_DIR / f"kr_pullback_{date_str}.html"
    html_path.write_text(build_html_report(final, histories, run_date.strftime("%Y-%m-%d"),
                                           funnel, len(all_candidates), stage_funnel), encoding="utf-8")

    print("[5/5] 최근 거래일 분포 검증 중 (캐시된 데이터로 추가 네트워크 호출 없이 재계산)...")
    VALIDATION_DAYS = 120
    calendar_ref = max(histories.values(), key=len)
    val_dates = calendar_ref.index[-VALIDATION_DAYS:]
    dist = []
    for d in val_dates:
        n_pass = 0
        for code, feat in histories.items():
            if d not in feat.index:
                continue
            r = screen_on_date(feat, d, metas[code])
            if r and r["is_candidate"]:
                n_pass += 1
        dist.append((d.strftime("%Y-%m-%d"), n_pass))
    counts = [c for _, c in dist]
    dist_df = pd.DataFrame(dist, columns=["date", "n_pass"])
    dist_path = OUT_DIR / "validation_distribution.csv"
    dist_df.to_csv(dist_path, index=False)
    zero_days = [d for d, c in dist if c == 0]
    p95 = int(np.percentile(counts, 95)) if counts else 0
    max_day = max(dist, key=lambda x: x[1]) if dist else (None, 0)

    report_lines = [
        f"실행 시각: {datetime.now().isoformat(timespec='seconds')}",
        f"소요 시간: {time.time()-t0:.0f}초",
        "",
        "== 데이터 소스 ==",
        "pykrx: 실패 (data.krx.co.kr가 통계 API에 로그인을 요구 -- 'RequestUnauthorized' 아님, 실제 서버 응답이 '로그인 또는 회원가입이 필요합니다' 페이지로 리다이렉트됨. KRX 정책 변경으로 보이며 이 샌드박스만의 문제가 아님)",
        "FinanceDataReader: 실패 (pip install 시 'No matching distribution found' -- 이 샌드박스의 PyPI 인덱스에 해당 패키지가 없음)",
        "대체 사용: 네이버 금융 공개 엔드포인트 (finance.naver.com, api.finance.naver.com, m.stock.naver.com, kind.krx.co.kr)",
        "",
        "== 유니버스 필터 퍼널 ==",
        f"시총 {CONFIG['MCAP_MIN_EOK']:,}억 이상: {funnel['mcap_floor_pass']}",
        f"우선주/스팩/리츠 이름패턴 제외: -{len(funnel['excluded_by_name_pattern'])}",
        f"ETF/ETN 등 비상장법인 제외: -{len(funnel['excluded_not_a_company'])}",
        f"상장 60일 미만 제외: -{len(funnel['excluded_too_new'])}",
        f"관리종목/거래정지 의심(네이버 아이콘 기준, best-effort) 제외: -{len(status_excluded)}",
        f"데이터 확보 실패 제외: -{len(failed_fetch)}",
        f"최종 스크리닝 유니버스: {len(histories)}",
        "",
        f"== {run_date.date()} 스크리닝 결과 ==",
        f"정배열+눌림 감지: {len(all_candidates)}",
        f"되돌림비율 {CONFIG['RETRACE_MIN']}~{CONFIG['RETRACE_MAX']} 통과(최종 후보): {len(final)}",
        "",
        f"== 최근 {VALIDATION_DAYS}거래일 분포 검증 (동일 유니버스를 과거로 되돌려 재적용, survivorship bias 있음 유의) ==",
        f"일평균 통과 종목수: {np.mean(counts):.1f}, 중앙값: {np.median(counts):.0f}, 최대: {max_day[1]}건({max_day[0]}), 95백분위: {p95}",
        f"0종목 통과일: {len(zero_days)}/{VALIDATION_DAYS}일 -> {zero_days[:8]}{'...' if len(zero_days)>8 else ''}",
        "",
        "== 상위 후보 실측 검증 (고점->저점->현재 수치로 눌림목 형태 확인) ==",
        spot_check(final, histories, n=5),
    ]
    report_path = OUT_DIR / "run_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print("\n".join(report_lines))
    print(f"\nCSV: {csv_path}\nHTML: {html_path}\n검증분포: {dist_path}\n리포트: {report_path}")


if __name__ == "__main__":
    main()
