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
    MIN_AVG_TRADING_VALUE_EOK = 5,  # 20일 평균 거래대금(억원) 하한. 대형주 위주라 느슨한 기본값.
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
    MA60_SLOPE_LOOKBACK = 5,        # 60일선 기울기를 며칠 전 대비로 잴지. 5일 전보다 높으면 상승.
    DISPARITY_MA = 20,              # 이격도 = 종가 / 이 기간 이평선. 스펙에 기준선이 명시되지 않아 20일선으로 가정.
    DISPARITY_MAX = 1.15,           # 이격도가 이 값을 넘으면(20일선 대비 +15% 초과) 과열로 보고 제외.

    # --- retracement (되돌림), 종가 기준 ---
    RETRACE_WINDOW = 20,            # 당일을 제외한 최근 N일 종가로 고점/저점을 잡음.
    RETRACE_MIN = 0.20,             # 통과 하한. (H-종가)/(H-L_leg) 이 값 미만이면 눌림이 너무 얕음 -> 제외.
    RETRACE_MAX = 0.70,             # 통과 상한. 이 값 초과면 추세 훼손 수준의 눌림 -> 제외.
                                     # 비율 자체는 필터로 잘라도 결과 테이블 컬럼에는 항상 남긴다.

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

    # --- scoring weights (각 0~1로 정규화한 값에 곱함, 합이 1일 필요는 없음) ---
    WEIGHT_VALUE_GROWTH = 0.30,     # 거래대금 증가율 (최근 5일 평균 / 그 이전 20일 평균)
    WEIGHT_VOLUME_DRYUP = 0.25,     # 눌림 구간 거래량이 직전 상승 구간보다 줄었는지 (건강한 눌림)
    WEIGHT_MA_DISTANCE = 0.25,      # 20일선까지의 거리가 가까울수록 높은 점수
    WEIGHT_FRESHNESS = 0.20,        # 고점 갱신 후 경과일이 짧을수록 높은 점수
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


def fetch_kind_listing(market_type: str) -> pd.DataFrame:
    """market_type: 'stockMkt' (KOSPI) or 'kosdaqMkt' (KOSDAQ)."""
    url = f"https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType={market_type}"
    html = http_get_text(url, "euc-kr")
    tables = pd.read_html(io.StringIO(html))
    df = tables[0]
    df.columns = ["name", "market_seg", "code", "industry", "product", "listing_date", "settle_month", "ceo", "homepage", "region"][:len(df.columns)]
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["listing_date"] = pd.to_datetime(df["listing_date"], errors="coerce")
    return df[["code", "name", "listing_date"]]


def fetch_mcap_ranked(sosok: int, mcap_floor_eok: float) -> pd.DataFrame:
    """sosok: 0=KOSPI, 1=KOSDAQ. Pages are pre-sorted by market cap desc;
    stop once a page's max cap drops below the floor."""
    rows = []
    page = 1
    while True:
        url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
        html = http_get_text(url, "euc-kr")
        codes = re.findall(r'code=(\d{6})"[^>]*class="tltle"', html)
        if not codes:
            break
        tables = pd.read_html(io.StringIO(html))
        t = tables[1].dropna(subset=["종목명"]).reset_index(drop=True)
        if len(t) != len(codes):
            n = min(len(t), len(codes))
            t = t.iloc[:n]
            codes = codes[:n]
        t = t.copy()
        t["code"] = codes
        rows.append(t[["code", "종목명", "시가총액", "상장주식수"]])
        page_min_cap = t["시가총액"].min()
        if page_min_cap < mcap_floor_eok or len(t) < 50:
            break
        page += 1
        time.sleep(0.15)
    out = pd.concat(rows, ignore_index=True)
    out.columns = ["code", "name", "mcap_eok", "shares_out"]
    out = out[out["mcap_eok"] >= mcap_floor_eok].reset_index(drop=True)
    out["market"] = "KOSPI" if sosok == 0 else "KOSDAQ"
    return out


def build_universe() -> tuple[pd.DataFrame, dict]:
    report = {}
    kospi_cap = fetch_mcap_ranked(0, CONFIG["MCAP_MIN_EOK"])
    kosdaq_cap = fetch_mcap_ranked(1, CONFIG["MCAP_MIN_EOK"])
    cap_df = pd.concat([kospi_cap, kosdaq_cap], ignore_index=True)
    report["mcap_floor_pass"] = len(cap_df)

    name_excluded = cap_df[cap_df["name"].apply(is_excluded_name)]
    cap_df = cap_df[~cap_df["code"].isin(name_excluded["code"])].reset_index(drop=True)
    report["excluded_by_name_pattern"] = name_excluded["name"].tolist()

    kospi_list = fetch_kind_listing("stockMkt")
    kosdaq_list = fetch_kind_listing("kosdaqMkt")
    listing = pd.concat([kospi_list, kosdaq_list], ignore_index=True).drop_duplicates("code")
    merged = cap_df.merge(listing[["code", "listing_date"]], on="code", how="left")

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
    x["ma60_slope_up"] = x["ma60"] > x["ma60"].shift(slope_lb)
    dma = CONFIG["DISPARITY_MA"]
    x["disparity"] = x["close"] / x[f"ma{dma}"]
    p5, p20, p60, p120 = CONFIG["MA_PERIODS"]
    x["is_stacked"] = (x[f"ma{p5}"] > x[f"ma{p20}"]) & (x[f"ma{p20}"] > x[f"ma{p60}"]) & (x[f"ma{p60}"] > x[f"ma{p120}"])
    x["is_uptrend"] = x["is_stacked"] & x["ma60_slope_up"] & (x["disparity"] <= CONFIG["DISPARITY_MAX"])

    w = CONFIG["RETRACE_WINDOW"]
    prior_close = x["close"].shift(1)
    x["h20"] = prior_close.rolling(w).max()
    x["l20"] = prior_close.rolling(w).min()
    # 구 정의: 분모가 20일 창 '전체'의 최저값. 고점보다 뒤에 있는 저점이 분모에 섞일 수 있어
    # 되돌림률과 반등률이 한 컬럼에 뒤엉킨다. INV-3 하위호환용으로 값만 병기 보존한다(§2).
    rng_legacy = x["h20"] - x["l20"]
    x["retrace_ratio_legacy"] = np.where(rng_legacy > 0, (x["h20"] - x["close"]) / rng_legacy, np.nan)

    # INV-7: 20일 창을 고점 기준으로 둘로 쪼갠다 (§2 의사코드).
    #   ① L_leg  = min(close[t-N : H_date+1])  상승 다리의 시작 저점 -> retrace_ratio 분모
    #   ③ L_pull = min(close[H_date : t+1])    눌림 바닥 (당일 포함)
    # days_since_high20을 구하던 루프에서 고점 인덱스를 그대로 재사용한다.
    close_arr = x["close"].to_numpy(dtype=float)
    n = len(x)
    days_since_high = np.full(n, np.nan)
    l_leg = np.full(n, np.nan)
    l_pull = np.full(n, np.nan)
    days_since_pullback_low = np.full(n, np.nan)
    for j in range(w + 1, n):
        window = close_arr[j - w:j]  # matches prior_close.shift/rolling alignment
        hi = j - w + int(np.argmax(window))            # ② H_date (절대 인덱스)
        days_since_high[j] = j - hi
        l_leg[j] = close_arr[j - w:hi + 1].min()       # ① 고점 '이전' 구간 (고점일 포함)
        seg = close_arr[hi:j + 1]                      # ③ 고점 '이후' 구간 (당일 포함)
        lpi = hi + int(np.argmin(seg))
        l_pull[j] = close_arr[lpi]
        days_since_pullback_low[j] = j - lpi           # 0이면 오늘이 바닥 -> 탈락
    x["days_since_high20"] = days_since_high
    x["l_leg"] = l_leg
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

    x["trading_value_eok"] = x["close"] * x["volume"] / 1e8

    ema12 = x["close"].ewm(span=12, adjust=False).mean()
    ema26 = x["close"].ewm(span=26, adjust=False).mean()
    x["macd"] = ema12 - ema26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    return x


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


def score_row(x: pd.DataFrame, j: int) -> dict:
    """Score components for row j; each raw metric plus a 0..1 normalized
    version relative to the trailing 120 sessions of this same stock (so the
    score is comparable across very different price/volume scales)."""
    def pct_rank(series: pd.Series, value: float) -> float:
        s = series.dropna()
        if len(s) < 10 or pd.isna(value):
            return 0.5
        return float((s < value).mean())

    hist = x.iloc[max(0, j - 120):j]

    val = x["trading_value_eok"]
    recent5 = val.iloc[j - 4:j + 1].mean()
    prior20 = val.iloc[j - 24:j - 4].mean()
    value_growth = (recent5 / prior20 - 1) if prior20 and prior20 > 0 else 0.0
    value_growth_n = pct_rank(hist["trading_value_eok"].pct_change(5), value_growth)

    peak_i = j - int(x["days_since_high20"].iloc[j]) if not pd.isna(x["days_since_high20"].iloc[j]) else None
    if peak_i is not None and peak_i < j:
        pullback_vol = x["volume"].iloc[peak_i + 1:j + 1].mean()
        pre_window = x["volume"].iloc[max(0, peak_i - 20):peak_i + 1]
        advance_vol = pre_window.mean() if len(pre_window) else np.nan
        vol_dryup_ratio = (pullback_vol / advance_vol) if advance_vol and advance_vol > 0 else np.nan
    else:
        vol_dryup_ratio = np.nan
    vol_dryup_n = 0.5 if pd.isna(vol_dryup_ratio) else float(np.clip(1 - vol_dryup_ratio, 0, 1))

    disparity = x["disparity"].iloc[j]
    ma_distance = abs(disparity - 1.0) if not pd.isna(disparity) else np.nan
    ma_distance_n = 0.5 if pd.isna(ma_distance) else float(np.clip(1 - ma_distance / 0.15, 0, 1))

    freshness = x["days_since_high20"].iloc[j]
    freshness_n = 0.5 if pd.isna(freshness) else freshness_score(float(freshness))

    score = (
        CONFIG["WEIGHT_VALUE_GROWTH"] * value_growth_n
        + CONFIG["WEIGHT_VOLUME_DRYUP"] * vol_dryup_n
        + CONFIG["WEIGHT_MA_DISTANCE"] * ma_distance_n
        + CONFIG["WEIGHT_FRESHNESS"] * freshness_n
    )
    return dict(
        score=round(float(score) * 100, 1),
        value_growth_pct=round(float(value_growth) * 100, 1),
        vol_dryup_ratio=None if pd.isna(vol_dryup_ratio) else round(float(vol_dryup_ratio), 2),
        peak_date=x.index[peak_i].strftime("%Y-%m-%d") if peak_i is not None else None,
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
    passes_band = CONFIG["RETRACE_MIN"] <= row["retrace_ratio"] <= CONFIG["RETRACE_MAX"]

    # INV-4: 고점 경과일이 범위 밖이면 후보에서 탈락시킨다. 행 자체는 남겨서
    # retrace_ratio(INV-3)와 탈락 사유를 계속 볼 수 있게 한다.
    dsh = row["days_since_high20"]
    dsh_int = None if pd.isna(dsh) else int(dsh)
    passes_days = dsh_int is not None and (
        CONFIG["DAYS_SINCE_HIGH_MIN"] <= dsh_int <= CONFIG["DAYS_SINCE_HIGH_MAX"]
    )
    struct = structure_verdict(row)  # INV-7: ①→②→③→④ 순서 확인

    reasons = []
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
        score=None, value_growth_pct=None, vol_dryup_ratio=None, peak_date=peak_date_of(feat, j)
    )

    return dict(
        code=meta["code"], name=meta["name"], market=meta["market"],
        date=date.strftime("%Y-%m-%d"),
        close=float(row["close"]), mcap_eok=meta.get("mcap_eok"),
        retrace_ratio=round(float(row["retrace_ratio"]), 3),
        retrace_ratio_legacy=None if pd.isna(row["retrace_ratio_legacy"]) else round(float(row["retrace_ratio_legacy"]), 3),
        passes_retrace_band=bool(passes_band),
        passes_days_since_high=bool(passes_days),
        passes_structure=bool(struct["ok"]),
        is_candidate=bool(passes_band and passes_days and struct["ok"]),
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


def build_html_report(results: list[dict], histories: dict, run_date: str, funnel: dict, n_uptrend_detected: int) -> str:
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
<div class="sub">정배열(5&gt;20&gt;60&gt;120) + 60일선 상승 + 이격도 {CONFIG['DISPARITY_MAX']} 이하 종목 중, 종가 기준 20일 되돌림비율을 계산한 결과. 통과 구간({CONFIG['RETRACE_MIN']}~{CONFIG['RETRACE_MAX']}) 충족 {pass_band}건 / 정배열+눌림 감지 전체 {n_uptrend_detected}건.</div>
<div class="funnel">
  <div>시총 {CONFIG['MCAP_MIN_EOK']:,}억 이상<b>{funnel.get('mcap_floor_pass','-')}</b></div>
  <div>우선주/스팩/리츠 제외 후<b>{funnel.get('mcap_floor_pass',0) - len(funnel.get('excluded_by_name_pattern',[]))}</b></div>
  <div>ETF 등 비상장법인 제외 후<b>{funnel.get('mcap_floor_pass',0) - len(funnel.get('excluded_by_name_pattern',[])) - len(funnel.get('excluded_not_a_company',[]))}</b></div>
  <div>상장 60일 미만 제외 후<b>{funnel.get('final_universe_size','-')}</b></div>
  <div>정배열+눌림 감지<b>{n_uptrend_detected}</b></div>
  <div>되돌림비율 0.2~0.7 통과<b>{pass_band}</b></div>
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
    n_struct_out = sum(1 for r in all_candidates if not r["passes_structure"])
    n_at_low = sum(1 for r in all_candidates if (r["days_since_pullback_low"] or 0) < CONFIG["PULLBACK_LOW_MIN_AGE"])
    n_flat = sum(1 for r in all_candidates if (r["range_pct"] is None or r["range_pct"] < CONFIG["RANGE_PCT_MIN"]))
    print(f"      정배열+눌림 감지: {len(all_candidates)}종목")
    print(f"      되돌림비율 {CONFIG['RETRACE_MIN']}~{CONFIG['RETRACE_MAX']} 통과: {n_band}종목")
    print(f"      고점경과일 {CONFIG['DAYS_SINCE_HIGH_MIN']}~{CONFIG['DAYS_SINCE_HIGH_MAX']}일 밖으로 탈락: {n_days_out}종목")
    print(f"      시계열 구조(INV-7) 탈락: {n_struct_out}종목 "
          f"(오늘이 바닥 {n_at_low} / 상승다리 {CONFIG['RANGE_PCT_MIN']*100:.0f}% 미만 {n_flat})")
    print(f"      최종 후보: {len(final)}종목")

    date_str = run_date.strftime("%Y%m%d")
    csv_path = OUT_DIR / f"kr_pullback_{date_str}.csv"
    # CSV에는 탈락 종목도 남긴다 (INV-3: 밴드 밖이어도 retrace_ratio는 항상 보존).
    csv_rows = sorted(all_candidates, key=lambda d: (not d["is_candidate"], -(d["score"] or 0)))
    pd.DataFrame(csv_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    html_path = OUT_DIR / f"kr_pullback_{date_str}.html"
    html_path.write_text(build_html_report(final, histories, run_date.strftime("%Y-%m-%d"), funnel, len(all_candidates)), encoding="utf-8")

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
