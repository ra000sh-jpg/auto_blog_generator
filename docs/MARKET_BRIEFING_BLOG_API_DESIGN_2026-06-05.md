# 시장 브리핑형 자동 블로그 API 설계안

작성일: 2026-06-05
목표: 하루 3개 네이버 블로그 초안을 생성하되, 그중 2개를 국장 개장 전/미장 개장 전 시장 브리핑으로 고정하고 텔레그램 승인 후 네이버 블로그 임시저장까지 진행한다.

## 1. 결론

DeepSeek API는 이 자동 블로그 생성기의 기본 글쓰기 엔진으로 충분하다. 다만 DeepSeek는 시장 데이터를 직접 보장하지 않는다. 따라서 설계는 다음처럼 분리해야 한다.

- DeepSeek: 데이터 요약, 시장 해석, 윤서재 페르소나 기반 글 생성, 품질 점검.
- 데이터 API: 지수, 환율, 금리, 코인, 뉴스 헤드라인 수집.
- 브라우저 자동화: 네이버 블로그 임시저장.
- 텔레그램: 초안 검토, 승인/수정 요청, 작업 상태 알림.

현재 비용 목표가 월 3달러 이하이므로, 첫 버전은 DeepSeek + 무료 데이터 소스 + Pexels + 텔레그램 + 로컬/무료 서버 조합이 가장 현실적이다. 유료 시장 데이터 API는 품질이 필요해질 때 붙이고, 처음부터 붙이지 않는다.

## 2. API 권장 구성

### 필수

| 용도 | 권장 API/방식 | 비용 판단 | 비고 |
| --- | --- | --- | --- |
| 글 생성 LLM | DeepSeek `deepseek-v4-flash` | 매우 낮음 | 공식 문서상 JSON 출력, tool calls, 긴 컨텍스트를 지원한다. |
| 텔레그램 승인 | Telegram Bot API | 무료 | 초안 전송, 승인/수정 callback 처리. |
| 네이버 업로드 | Playwright 브라우저 자동화 | 별도 API 없음 | 네이버 블로그 글쓰기 API는 종료되어 브라우저 자동화가 현실적이다. |
| 이미지 검색 | Pexels API | 무료 한도 충분 | 기본 200 req/hour, 20,000 req/month. 출처 표기 필요. |
| 블로그 경쟁글 확인 | Naver Search Blog API | 무료 한도 충분 | 블로그 발행 API가 아니라 검색 API다. |

### 시장 데이터 1차 구성

| 데이터 | 1차 소스 | 이유 |
| --- | --- | --- |
| KRX 장 시간/휴장 기준 | KRX 공식 페이지 + 휴장일 캐시 | 정규장 09:00~15:30 기준 확인. |
| 미국 장 시간 | Nasdaq/NYSE 기준 + timezone 변환 | 미국 정규장 09:30~16:00 ET. 한국시간은 서머타임에 따라 변한다. |
| 미국 지수/ETF/섹터 | yfinance 또는 Stooq 캐시 | 키 없이 시작 가능하지만 안정성은 낮음. |
| 국내 지수/외국인 수급 | KIS Open API 또는 증권사/거래소 데이터 | 공식 API 후보. 공개 블로그 재사용 약관은 별도 확인 필요. |
| 환율/금리/매크로 | FRED, 한국은행 ECOS, Alpha Vantage 일부 | FRED는 거시 시계열에 안정적. |
| 뉴스 | GDELT DOC API, RSS, Naver Search News/Blog | 무료 우선. 숫자보다 핵심 이벤트 추출용. |
| 코인 | CoinGecko/Binance 공개 API 후보 | risk-on/risk-off proxy로만 사용. |

Alpha Vantage는 공식 무료 한도가 25 request/day라 첫 버전의 중심 데이터 소스로는 빡빡하다. 다만 환율/일봉/market status 보조 소스로는 쓸 수 있다.

### 무료 데이터 부족 대응망

데이터가 부족할 때는 DeepSeek가 숫자를 추정하게 하지 않는다. 대신 여러 무료 소스를 계층적으로 조회하고, 출처가 부족하면 글의 성격을 "정확한 수치 브리핑"에서 "시나리오/체크리스트 브리핑"으로 낮춘다.

| 계층 | 목적 | 후보 소스 | 처리 방식 |
| --- | --- | --- | --- |
| 1차 가격 데이터 | 지수, ETF, 선물 proxy | yfinance, Stooq, 거래소 공개 페이지 캐시 | 같은 값이 2개 소스에서 크게 다르면 수치 대신 방향성만 사용 |
| 2차 매크로 | 금리, 달러, 주요 경제지표 | FRED, 한국은행 ECOS, ECB/BOJ 공식 자료 | 느린 지표이므로 하루 1회 캐시 |
| 3차 공시/실적 | 미국 개별주 이슈, 실적 전후 맥락 | SEC EDGAR, Nasdaq/NYSE 캘린더, 기업 IR RSS | 원문 재배포 금지, 핵심 이벤트만 요약 |
| 4차 글로벌 뉴스 | 해외 이슈, 지정학, 섹터 이슈 | GDELT DOC API, Google News RSS, Reuters/AP/CNBC RSS 가능 범위 | 제목/출처/시간만 수집하고 본문은 요약 금지 |
| 5차 코인 | 위험 선호 proxy | CoinGecko Demo/Public API, Binance market data | BTC/ETH 중심, 무료 한도 내 캐시 |
| 6차 커뮤니티/소셜 | 시장 반응 보조 | xAI X Search, Groq/Gemini Search, Reddit RSS | 기본 비활성화. 예산 cap 안에서만 제한 사용 |

미장 브리핑은 해외 데이터 접근을 더 적극적으로 한다. 최소 수집 universe는 다음처럼 둔다.

- 지수/선물 proxy: SPY, QQQ, DIA, IWM, VIX proxy.
- 섹터 ETF: XLK, XLY, XLF, XLE, XLU, XLV, SMH 또는 SOXX.
- 매크로: DXY, US10Y, US2Y, WTI, Gold.
- 글로벌 연결: EWY, FXI, KWEB, HSI/Nikkei/KOSPI proxy.
- 공시/실적: SEC EDGAR 8-K/10-Q/10-K, 주요 빅테크 IR RSS.
- 뉴스: GDELT에서 `Nasdaq`, `Federal Reserve`, `semiconductor`, `AI chip`, `Treasury yield`, `inflation` 키워드 그룹.

데이터 신뢰도 점수:

```text
source_confidence =
  official_source_count * 0.35
  + cross_source_match * 0.30
  + freshness_score * 0.20
  + historical_stability * 0.15
```

`source_confidence < 0.55`이면 구체적 수치 예측을 금지한다. 이 경우 본문에는 "오늘은 데이터가 엇갈리므로 방향보다 조건을 보겠다"는 식으로 쓴다.

### 보조 LLM/API 후보

DeepSeek를 기본 엔진으로 두되, 보조 API는 "최신성", "속도", "품질 검수", "무료 프로토타입"처럼 역할을 분리한다.

| 후보 | 적합한 역할 | 비용/제약 판단 | 적용 우선순위 |
| --- | --- | --- | --- |
| Gemini Flash-Lite | 저렴한 보조 생성, 품질 재검수, Google Search grounding 실험 | 무료 tier가 있고 유료도 낮은 편. 다만 무료 tier 입력은 제품 개선에 사용될 수 있다. | 높음 |
| Groq | 빠른 초안 변형, JSON 정리, 간단한 분류 | 매우 빠르고 저렴하지만 X 최신 데이터 접근 기능은 없다. | 중간 |
| xAI Grok | X 최신 흐름, 특정 계정/스레드 기반 트렌드 해석 | X Search가 강점. 모델 비용 + tool invocation 비용이 붙는다. | 선택 |
| NVIDIA NIM | 무료 프로토타입, 여러 오픈 모델 비교, 안전성/검수 모델 후보 | Developer Program 기준 프로토타입 무료. production 용도로는 별도 조건 필요. | 중간 |
| Qwen/DashScope | 저렴한 대체 생성, 긴 문서/CSV 처리, 다국어 요약 | OpenAI-compatible. 신규 무료 quota와 pay-as-you-go. 리전별 모델/가격 차이 확인 필요. | 높음 |
| GitHub 무료 API 목록 | 무료 데이터 소스 탐색 | GitHub에 올라온 "공유 API 키"는 사용 금지. 목록은 공급자를 찾는 용도로만 사용하고 키는 직접 발급한다. | 낮음 |

xAI Grok은 X Search가 필요한 글에만 제한적으로 사용한다. 예를 들어 "오늘 X에서 AI 반도체 관련 반응이 달라진 이유" 같은 1주 1~2회성 글에는 가치가 있지만, 하루 3개 모든 글에 붙이면 예산 통제가 어려워진다.

Groq는 "Grok"과 다르다. Groq는 빠른 LLM inference 서비스이고, X의 최신 게시물을 직접 가져오는 기능은 없다. 따라서 시장/블로그 파이프라인에서는 DeepSeek 장애 시 대체 생성, 초안 다듬기, 빠른 분류에 맞다.

NVIDIA NIM은 무료 프로토타입에 좋지만 운영 핵심 엔진으로 고정하지 않는다. 무료/preview 성격의 endpoint는 모델 availability와 한도가 바뀔 수 있으므로, "실험용 provider"로 등록하고 실패하면 DeepSeek/Gemini로 fallback한다.

Qwen은 보조 provider로 꽤 좋다. Alibaba Cloud Model Studio가 OpenAI-compatible API를 제공하고 신규 무료 quota도 있다. 특히 `qwen-long`은 긴 CSV, 과거 글 백업, topic memory를 한 번에 읽는 용도에 강하다. 다만 region마다 API key와 지원 모델/가격이 달라 운영 설정을 분리해야 한다.

### 모델 자동 업그레이드 정책

DeepSeek가 새 모델을 내더라도 운영 모델을 즉시 바꾸지는 않는다. "자동 발견 -> 검증 -> 텔레그램 승인 -> champion 전환"으로 처리한다.

```text
daily_model_discovery
  -> provider /models 또는 공식 가격 페이지 확인
  -> candidate_model_registry 저장
  -> golden prompt 5개로 비용/품질/한국어 안정성 테스트
  -> 기존 champion과 비교
  -> 통과 시 텔레그램으로 전환 제안
  -> 승인 시 provider 기본 모델 변경
```

자동 전환 조건:

- 기존 모델보다 비용이 같거나 낮다.
- JSON 출력과 tool calls가 유지된다.
- 시장 브리핑 샘플에서 숫자 조작이 없다.
- 윤서재 페르소나 문체 점수가 기존보다 낮지 않다.
- 3회 연속 API 호출 성공.

상위 모델이 비싸면 자동 전환하지 않고 `premium_candidate`로만 등록한다. 즉 "최신 모델이면 무조건 사용"이 아니라 "월 3달러 예산과 글 품질을 동시에 만족하면 사용"하는 방식이다.

## 3. DeepSeek만으로 충분한가

충분한 영역:

- CSV 한 줄 제목을 블로그 초안으로 확장.
- 국장/미장 브리핑 데이터를 사람이 읽을 수 있는 글로 바꿈.
- 윤서재 페르소나의 문장 톤 유지.
- 과장 표현, 투자 권유 표현, 제목-본문 불일치 검사.
- 텔레그램 수정 피드백을 반영한 재작성.

부족한 영역:

- 실시간 시장 데이터의 정확성.
- 뉴스 저작권/원문 재배포 판단.
- 네이버 로그인/캡차/에디터 UI 변화 대응.
- 투자 조언에 가까운 표현의 법적 리스크 통제.

권장 모델 운영:

- 기본 생성: `deepseek-v4-flash`, temperature 0.35~0.55.
- 시장 브리핑 JSON 요약: `deepseek-v4-flash`, temperature 0.2~0.35.
- 품질 재판정/복잡한 해석: 필요할 때만 `deepseek-v4-pro`.
- 기존 `deepseek-chat`, `deepseek-reasoner` 별칭은 2026-07-24 15:59 UTC 이후 deprecated 예정이므로 신규 설정에서는 쓰지 않는다.

## 4. 하루 3편 편성

| 슬롯 | 시간 | 글 성격 | 목적 |
| --- | --- | --- | --- |
| `KR_PREOPEN` | 평일 08:10~08:20 KST | 국장 개장 전 브리핑 | 지난밤 미장/코인/환율/금리 흐름이 오늘 국장 심리에 주는 영향 정리 |
| `US_PREOPEN` | 미국 정규장 개장 60분 전 | 미장 개장 전 브리핑 | 아시아/유럽/코인/선물 흐름이 미장 초반 변동성에 주는 영향 정리 |
| `EVERGREEN_INSIGHT` | 12:00~18:00 중 랜덤 | 오래 읽히는 통찰 글 | 시장에서 배운 판단 기준, 초심자 투자 습관, 자기개발/AI 자동화와 연결 |

국장 휴장일, 미국장 휴장일, 주말에는 시장 브리핑 슬롯을 억지로 만들지 않는다. 대신 사용자가 CSV에 제공한 제목 또는 topic backlog를 바탕으로 `EVERGREEN_INSIGHT`를 늘린다.

```text
if KRX is closed:
  KR_PREOPEN -> EVERGREEN_INSIGHT

if US market is closed:
  US_PREOPEN -> EVERGREEN_INSIGHT

if weekend:
  KR_PREOPEN -> EVERGREEN_INSIGHT
  US_PREOPEN -> EVERGREEN_INSIGHT
  optional third slot -> WEEKLY_REFLECTION
```

휴장일 대체 글의 성격:

- 시장 데이터 숫자 대신 "판단 기준", "기록법", "투자 습관", "AI 활용", "자기 운영 시스템"을 다룬다.
- 당일 브리핑과 다음 개장일 브리핑을 연결하는 질문을 남긴다.
- 제목은 CSV 제목을 우선하고, 없으면 최근 발행글/대기 주제를 topic memory에서 가져온다.

주의: 사용자가 붙여준 "서머타임 기준 20:30~20:50"은 미국 정규장 09:30 ET 기준으로는 빠르다. 미국 정규장 개장은 한국시간으로 서머타임에는 22:30, 표준시에는 23:30이다. 따라서 미장 전 글은 `America/New_York`의 09:30을 기준으로 한국시간을 자동 계산하고, 개장 60분 전 또는 90분 전으로 예약해야 한다.

## 5. 국장 전 브리핑 설계

### 데이터 수집

- 수집 시간: 05:30~07:50 KST.
- 필수 데이터:
  - S&P 500, Nasdaq, Dow 전일 정규장 마감.
  - SOXX/SMH, XLK, XLF 등 핵심 섹터 ETF.
  - EWY 또는 MSCI Korea proxy.
  - USD/KRW, DXY, US10Y.
  - BTC/ETH 24h 변화율, 변동성.
- 선택 데이터:
  - 미국 주요 실적/경제지표.
  - 반도체, 빅테크, 바이오 등 한국시장 커플링 섹터 뉴스.

### 점수 구조

기존 가중치는 방향이 좋지만 환율/금리가 표에서 빠져 있다. 국장에는 환율과 미국 금리가 외국인 수급의 핵심 변수이므로 별도 반영하는 편이 낫다.

```text
kr_preopen_score =
  us_index_close_score * 0.30
  + us_sector_coupling_score * 0.25
  + fx_rate_score * 0.15
  + us10y_rate_score * 0.10
  + crypto_risk_score * 0.10
  + ewy_msci_korea_score * 0.10
```

### 출력

- 오늘의 한 줄 판단: 상승/중립/경계가 아니라 "어떤 조건에서 어떤 시장 심리가 우세한가".
- 주목 섹터 2~3개: 반도체, 2차전지, 바이오, 금융, 조선 등.
- 초심자 체크포인트: "시초가 추격보다 10시 이후 수급 확인" 같은 행동 기준.
- 윤서재 코멘트: "시장은 예측보다 대응 기준을 가진 사람에게 덜 가혹하다" 식의 현실적 통찰.

## 6. 미장 전 브리핑 설계

### 데이터 수집

- 수집 시간: 미국 정규장 개장 120분 전부터 70분 전까지.
- 필수 데이터:
  - S&P 500/Nasdaq/Dow futures.
  - KOSPI/KOSDAQ 마감, 외국인/기관 수급.
  - 상해종합, 항셍, 니케이 등 아시아 마감.
  - 유럽 주요 지수 초반 흐름.
  - BTC/ETH 및 Nasdaq futures 동행 여부.
  - DXY, US10Y, WTI.
- 선택 데이터:
  - CPI, PCE, 고용, FOMC, 연준 발언.
  - 대형 기술주 프리마켓 이슈.

### 점수 구조

```text
us_preopen_score =
  us_futures_score * 0.40
  + asia_close_score * 0.20
  + rates_dollar_score * 0.15
  + crypto_risk_score * 0.15
  + macro_news_alignment_score * 0.10
```

기존의 "뉴스와 지수선물 상관관계 0.7 이상"은 좋은 방향이지만, 실시간 글 생성에서는 통계적 상관을 매번 신뢰하기 어렵다. 대신 다음 조건을 조합한 `macro_news_alignment_score`를 둔다.

- 뉴스 발생 후 지수선물 방향이 같은가.
- 같은 키워드가 2개 이상 신뢰 출처에서 반복되는가.
- 발표 시각이 시장 움직임과 맞물리는가.
- 금리/DXY/섹터 ETF가 같은 방향을 확인해주는가.

### 출력

- 본장 초반 변동성 시나리오: base/bull/bear 3개.
- 기술주/가치주 중 어느 쪽이 더 민감한지.
- 포지션 "권고" 대신 관찰 기준: 유지/축소/추격 금지 같은 표현은 "내 기준"으로 한정.
- 초심자 경고: 프리마켓 강세와 본장 강세는 다르다는 점.

## 7. 코인 데이터 사용 원칙

코인은 주식 글에서 독립 주제가 아니라 위험 선호 proxy로만 쓴다.

본문에 포함하는 조건:

- BTC 24h 변화율이 ±1.5% 이상.
- BTC와 Nasdaq futures 방향이 같음.
- BTC 변동성이 최근 평균 대비 확대.
- ETH/BTC 괴리가 위험자산 선호를 설명할 때.

본문에서 제외하는 조건:

- 변화율이 작고 주식 선물과 방향성이 다를 때.
- 코인 개별 이슈가 주식시장 판단을 흐릴 때.
- 데이터가 2개 이상 소스에서 확인되지 않을 때.

## 8. 글 템플릿

### `KR_PREOPEN`

```text
제목: 오늘 국장 전에 볼 3가지: {주요 변수}가 만든 {시장 심리}

1. 왜 오늘 이 흐름을 봐야 하는가
2. 지난밤 미장 핵심 데이터
3. 한국시장으로 넘어올 가능성이 큰 변수
4. 오늘 시초가에서 조심할 점
5. 초심자를 위한 판단 기준
6. 윤서재의 결론
7. 다음 글 예고
```

### `US_PREOPEN`

```text
제목: 미장 개장 전 체크: {아시아/유럽/코인 변수}가 말하는 오늘의 리스크

1. 오늘 미장을 보기 전 전제
2. 아시아와 유럽 흐름
3. 선물/금리/달러/코인의 같은 방향과 다른 방향
4. 본장 초반 3가지 시나리오
5. 초심자가 피해야 할 판단
6. 윤서재의 결론
7. 내일 국장과 연결되는 질문
```

### `EVERGREEN_INSIGHT`

```text
제목: 시장이 흔들릴 때 초심자가 먼저 정해야 할 기준

1. 오늘 시장 브리핑에서 시작된 질문
2. 초심자가 자주 놓치는 구조
3. 기록으로 확인할 수 있는 판단
4. 내 조건에 맞는 행동 기준
5. 윤서재의 결론
6. 관련 이전 글 연결
```

## 9. CSV 스키마

```csv
date,slot,title,keywords,persona,market_scope,image_policy,status
2026-06-08,KR_PREOPEN,지난밤 나스닥과 환율이 오늘 국장에 주는 신호,"나스닥,환율,반도체",yun_seojae,kr,stock+ai,queued
2026-06-08,US_PREOPEN,아시아 마감과 비트코인으로 보는 오늘 미장 리스크,"미장,코인,금리",yun_seojae,us,stock,queued
2026-06-08,EVERGREEN_INSIGHT,초심자가 시장 뉴스에 흔들리지 않기 위한 기록법,"투자초심자,기록,판단기준",yun_seojae,general,ai,queued
```

`slot`이 비어 있으면 자동 분류한다. 시장 브리핑 슬롯은 CSV 제목이 없어도 당일 데이터로 제목을 생성할 수 있게 한다.

## 10. 텔레그램 승인 플로우

```text
queued
  -> data_collected
  -> draft_generated
  -> quality_checked
  -> telegram_pending_approval
  -> approved
  -> naver_draft_saved
```

텔레그램 메시지는 텍스트만으로도 충분해야 한다.

- 제목
- 5줄 요약
- 본문 전문
- 데이터 출처 목록
- 리스크 문구
- 버튼: 승인, 수정 요청, 폐기, 이미지 다시 선택

승인하면 네이버 "임시저장"까지만 간다. 최종 발행은 나중에 별도 버튼으로 분리한다.

## 11. 투자 표현 안전장치

시장 브리핑 글은 "예측"보다 "시나리오"로 쓴다.

사용 금지:

- 매수하세요.
- 오늘은 무조건 오릅니다.
- 포지션 축소/유지 권고.
- 이 종목이 갑니다.
- 확실한 신호입니다.

대체 표현:

- 내가 보는 기준은 이렇다.
- 이 조건이 유지되면 강세 시나리오가 우세하다.
- 이 조건이 깨지면 관망이 합리적이다.
- 초심자는 방향보다 변동성부터 확인해야 한다.

## 12. 이미지 설계

시장 브리핑은 이미지가 많으면 글의 신뢰도를 오히려 낮춘다.

- 국장/미장 브리핑: 1~2장.
  - 대표 이미지 1장: Pexels 금융/도시/차트 이미지.
  - 보조 이미지 1장: AI 생성 "시장 지도" 느낌의 추상 이미지 또는 직접 만든 간단한 데이터 카드.
- Evergreen 글: 2~3장.
  - AI 이미지 1장.
  - 무료 이미지 1장.
  - 필요 시 간단한 표/체크리스트 이미지 1장.

Pexels 이미지는 출처 표기를 저장하고, AI 이미지는 AI 생성 여부를 메타데이터에 남긴다.

## 13. 구현 우선순위

1. DeepSeek 기본 모델명을 `deepseek-v4-flash`로 교체하고 비용 테이블 갱신.
2. `KR_PREOPEN`, `US_PREOPEN`, `EVERGREEN_INSIGHT` job type 추가.
3. 시장 데이터 수집 결과를 `market_snapshot` JSON으로 저장.
4. `market_snapshot`을 읽어 블로그 초안을 만드는 전용 프롬프트 추가.
5. 텔레그램 승인 상태 `telegram_pending_approval` 추가.
6. 네이버 업로더의 `publish()`와 `save_draft()` 분리.
7. CSV import/export에 `slot`, `market_scope`, `image_policy` 필드 추가.
8. 하루 목표를 3개로 고정하고 슬롯별 시간대 스케줄러 적용.
9. 투자 표현 안전장치와 데이터 출처 검사를 품질 게이트에 추가.
10. KRX/미국장 휴장일 판단 후 `EVERGREEN_INSIGHT`로 자동 전환.
11. 무료 데이터 부족 대응망과 `source_confidence` 점수 추가.
12. Gemini/Groq/NVIDIA/Qwen/xAI provider를 보조 라우터에 등록하고 월 예산 cap 적용.
13. 모델 자동 발견/검증/승인 후 champion 전환 기능 추가.
14. 1주일 로컬 운영 후 무료/저비용 서버 이전 검증.

## 14. 운영 방식

첫 운영은 완전 자동 발행이 아니라 "자동 생성 + 사람 승인 + 임시저장"이어야 한다. 이유는 세 가지다.

- 시장 글은 틀린 숫자 하나가 신뢰를 크게 깎는다.
- 네이버 자동화는 로그인/캡차/에디터 변화에 취약하다.
- 윤서재 페르소나의 감도는 사람이 읽고 승인하는 단계에서 더 빨리 잡힌다.

1주일 동안 승인/수정 데이터를 모으면, 어떤 문장이 승환 님의 생각과 맞고 어떤 문장이 너무 AI스러운지 학습 데이터처럼 쌓을 수 있다. 그 다음 완전 자동 발행을 검토하는 것이 안전하다.

## 15. 참고 자료

- DeepSeek API Models & Pricing: https://api-docs.deepseek.com/quick_start/pricing
- 네이버 블로그 글쓰기 Open API 종료 공지: https://developers.naver.com/notice/article/7527
- Naver Search Blog API: https://developers.naver.com/docs/serviceapi/search/blog/blog.md
- Alpha Vantage Support/Pricing: https://www.alphavantage.co/support/
- KIS Developers API category: https://apiportal.koreainvestment.com/apiservice-category
- KRX KOSPI trading hours: https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp
- Nasdaq system hours: https://www.nasdaq.com/nasdaq-system-hours-of-operation
- Pexels API documentation: https://www.pexels.com/api/documentation/
- xAI API Pricing: https://docs.x.ai/developers/pricing
- xAI X Search: https://docs.x.ai/developers/tools/x-search
- Groq Pricing: https://groq.com/pricing
- NVIDIA NIM Run Anywhere: https://docs.api.nvidia.com/nim/docs/run-anywhere
- NVIDIA LLM APIs: https://docs.api.nvidia.com/nim/reference/llm-apis
- Gemini Developer API Pricing: https://ai.google.dev/gemini-api/docs/pricing
- GitHub Secret Scanning: https://docs.github.com/en/code-security/concepts/secret-security/about-secret-scanning
- FRED API: https://fred.stlouisfed.org/docs/api/fred/
- SEC EDGAR Data APIs: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- GDELT DOC 2.0 API: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- CoinGecko API Pricing: https://www.coingecko.com/en/api/pricing
- Alibaba Cloud Model Studio: https://www.alibabacloud.com/help/en/model-studio/what-is-model-studio
- Qwen-Long: https://www.alibabacloud.com/help/en/model-studio/long-context-qwen-long
