# pullback-screener

한국 주식(KRX) 일봉 기준 되돌림(눌림목) 스크리너. 장 마감 후 하루 1회 실행해
관찰 후보 종목 리스트를 뽑는다. **롱 관점만** 다루며 역배열 되돌림은 만들지 않는다.

---

## ★ 작업 규칙 (반드시 지킬 것)

### 1. SCREENER_SPEC.md 가 최상위 계약이다

코드를 수정하기 전에 `SCREENER_SPEC.md` 를 읽어라. 그 문서는 요구사항 명세이자
회귀 방지 계약이다. 문서와 코드가 충돌하면 **문서가 우선**한다.
스펙 자체를 바꿔야 한다고 판단되면 코드를 고치기 전에 먼저 물어라.

특히 §1 의 INV-1 ~ INV-6 는 절대 불변 규칙이다. 위반하는 코드는 병합하지 않는다.

- INV-1 종가 전용 (장중 고가·저가를 신호 계산에 쓰지 않는다)
- INV-2 고점 H·저점 L 은 당일을 제외한 직전 20 거래일에서 구한다
- INV-3 `retrace_ratio` 는 밴드 탈락 종목이라도 컬럼으로 남긴다
- INV-4 `days_since_high` 2 미만이면 무조건 탈락
- INV-5 모든 임계값은 `CONFIG` 한 곳에만 둔다
- INV-6 수정주가를 쓴다

### 2. "고쳤습니다"로 끝내지 마라

수정했으면 `SCREENER_SPEC.md` §7 의 검증(V-1 ~ V-7)을 실행하고
**결과를 숫자로 보고**하라. 통과 종목수 분포, 퍼널 단계별 잔존 수, 회귀 테스트 결과를
직접 보여줄 것. 코드 diff만 제시하고 끝내면 작업이 완료된 것이 아니다.

### 3. 잘못 잡힌 종목은 즉시 스펙에 고정하라

오탐이 발견되면 `SCREENER_SPEC.md` §8 에 종목코드·날짜·수치·기대결과를
R-1 과 같은 형식으로 추가한 뒤 고쳐라. 테스트에 박아두지 않으면 다음 수정에서 다시 깨진다.

### 4. 단편적으로 고치지 마라

증상 한 줄을 막는 패치 대신, 스펙의 어느 항목이 위반됐는지 먼저 특정하라.
필터 조건을 추가할 때는 점수 계산도 같은 방향인지 함께 확인한다.
(과거에 `days_since_high` 필터가 없는 상태에서 `freshness_n` 이 고점 다음날에
만점을 주고 있었다. 필터와 스코어가 반대 방향이면 조건을 넣어도 상위에 계속 올라온다)

### 5. 파라미터를 바꿨으면 binding 여부를 보고하라

임계값을 조정했으면 그 조건이 실제로 몇 종목을 잘랐는지 숫자로 확인하라.
아무것도 자르지 않는 조건은 있으나 마나다.

---

## 구조

```
scripts/kr_pullback_screener.py   주 구현 (유니버스·추세·되돌림·스코어링·HTML)
scripts/daily_screener.py         일별 실행 래퍼
scripts/pullback_backtest.py      백테스트 (--raw-dir/--universe/--out-dir)
scripts/build_pullback_report.py  리포트 생성
scripts/dashboard_refresh_kr.py   대시보드 갱신
dashboard/index.html              대시보드
config/pullback_universe.csv      유니버스 정의
data/raw_yahoo/                   원천 일봉 캐시 (gitignore)
outputs/, analysis_output/        산출물 (gitignore)
```

`sources/` 는 읽기 전용 참고 자료다 (아래 ChatGPT 미러 규칙 참조).

---

## 데이터 주의사항

- **현재 `data/raw_yahoo/` 는 수정주가가 아니다.** `kr_pullback_screener.py` 상단 주석에
  `Prices are NOT split-adjusted` 로 명시돼 있다. 룩백 구간에 액면분할이 있었던 종목은
  H·L·이동평균이 전부 오염되어 가짜 되돌림이 만들어진다. INV-6 위반이며 미해결 상태다
- `.env` 에 `KRX_AUTH_KEY` 가 있다. 수정주가 문제를 해결할 때 KRX 정식 소스로
  교체하는 방안을 우선 검토할 것
- 거래대금을 `종가 × 거래량` 으로 근사하는 경우 컬럼명이나 로그에 그 사실을 명시할 것
- 데이터 수집이 실패하면 조용히 넘어가지 말고 명확히 보고한다

---

## 미해결 항목

`SCREENER_SPEC.md` §9 "현재 위반 목록" 에 우선순위 표로 정리돼 있다.
항목을 해결할 때마다 그 표를 갱신할 것.

---

## ChatGPT project context

This directory is a local mirror of the ChatGPT project "투자 잡담".

- Treat every file under `sources/` as read-only reference material.
- Do not edit, rename, move, or delete synced project files.
- These files may be replaced the next time a task is created from this ChatGPT project.
