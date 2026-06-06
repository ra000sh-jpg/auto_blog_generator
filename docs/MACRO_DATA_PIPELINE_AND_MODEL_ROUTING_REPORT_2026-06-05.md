# 정부 매크로 데이터 파이프라인 및 LLM 라우팅 적용 보고서

작성일: 2026-06-05

## 1. 결론

추가 기능의 방향은 좋다. 지금 프로그램이 지향하는 "가볍지만 품질 좋은 네이버 블로그 자동 생성기"와 가장 잘 맞는 확장이다. 다만 처음부터 산업부, 관세청, 한국은행, 통계청을 모두 자동화하면 실패 지점이 너무 많아진다. 1차 개발은 **산업통상부 수출입 보도자료 감지 -> 원문/본문 저장 -> 핵심 수치 추출 -> 블로그 후보 3~5개 생성 -> 텔레그램 검토 대기**까지만 잡는 것이 안전하다.

모델 운용은 **DeepSeek 우선, Qwen 보조**가 현실적이다. DeepSeek V4 Flash는 비용과 성능 균형이 좋고, DeepSeek V4 Pro는 통찰형 글의 최종 해석/문체 정리에 적합하다. Qwen은 한국어 문장 안정성, 빠른 백업, 저가 모델 후보(Qwen Flash 계열) 확장 가능성 때문에 보조 모델로 두는 편이 좋다.

신모델 자동 전환 기능은 **부분 적용 상태**다. 자동 승격 정책과 챔피언 전환 로직은 있지만, 공식 모델/가격 페이지에서 새 텍스트 모델을 자동 발견해 후보로 등록하는 기능은 아직 없다. VLM 쪽에는 discovery/pricing worker가 있으므로 같은 패턴을 텍스트 모델에도 이식하면 된다.

## 2. 붙여넣은 요구사항 해석

첨부 텍스트의 핵심 요구는 "정부 발표를 복붙하는 블로그"가 아니다. 공식 데이터를 기반으로 독자가 판단 기준을 얻는 글을 만드는 것이다.

핵심 문장:

> 정보는 많지만 기준은 부족하다. 좋은 글은 더 많은 정보를 주는 글이 아니라, 독자의 판단 기준을 선명하게 만드는 글이다.

이 문장을 시스템 기준으로 삼아야 한다. 따라서 매크로 글은 다음 순서를 따른다.

1. 이번 발표의 핵심 숫자
2. 숫자가 바뀐 배경
3. 초보자가 착각하기 쉬운 부분
4. 산업별 의미
5. 투자자가 볼 지점
6. 자영업자/생활인이 체감할 지점
7. 단기 노이즈인지 구조 변화인지
8. 다음 달 체크포인트

금지 기준도 명확하다.

- 수출 증가 = 무조건 좋음
- 수출 감소 = 무조건 나쁨
- 특정 종목 매수 추천
- 근거 없는 전망
- 클릭베이트
- 정부 발표 원문 베껴 쓰기

## 3. 데이터 소스 적용 순서

### 1차: 산업통상부

목표는 월별 수출입 동향 보도자료다. 산업통상부 사이트의 보도자료/검색 페이지에서 "수출입", "수출입동향", "수출입 동향" 키워드가 들어간 최신 문서를 감지한다. PDF와 HTML을 우선 처리하고, HWP만 있으면 `unsupported`로 저장하되 원문 URL은 반드시 남긴다.

산업통상부는 사이트 구조가 바뀔 수 있으므로, 크롤러는 "목록 파서"와 "상세 페이지 파서"를 분리해야 한다. 목록 파서가 깨져도 직접 검색 URL 또는 원문 URL 수동 입력으로 복구할 수 있어야 한다.

### 2차: 관세청 수출입무역통계

관세청 수출입무역통계 사이트는 수출입 총괄, 수출입 실적, 10대 품목/국가, 보도자료, 수출입 잠정통계 메뉴를 제공한다. 산업부 월간 자료보다 빠른 1~20일 잠정치가 있어 국장 전/미장 전 브리핑의 선행 데이터로 쓰기 좋다.

적용 방식은 "선행 지표"다. 블로그 글 전체를 관세청 데이터만으로 쓰기보다, 산업부 월간 수출입 글의 중간 점검 자료로 붙이는 편이 좋다.

### 3차: 한국은행 ECOS

금리, 환율, 경기, 소비, 통화량, 경제전망을 가져오는 층이다. 한국은행 ECOS Open API는 공식 API 키 신청 방식으로 접근한다. 여기서는 수출입 수치의 배경을 해석할 때 필요한 환율, 금리, 경기 지표를 보강한다.

### 4차: KOSIS/통계청

고용, 물가, 인구, 소비 동향을 붙이는 층이다. KOSIS 공유서비스는 통계목록, 통계자료, 통계설명, 통계주요지표 API를 제공한다. 생활인/자영업자 관점 글에서 특히 유용하다.

## 4. 1차 구현 범위

1차는 아래까지만 완성한다.

1. 산업통상부 최신 보도자료 확인
2. "수출입" 키워드 포함 문서 감지
3. 중복 방지 해시 생성
4. 원문 URL과 첨부 URL 저장
5. PDF 또는 HTML 본문 추출
6. 핵심 수치 추출
7. 블로그 후보 3~5개 생성
8. 품질 점수 산정
9. 텔레그램으로 후보 전송
10. 사람이 승인하거나 수정본을 입력할 때만 발행 대기로 이동

자동 발행은 1차 범위에서 제외한다. 매크로 데이터는 숫자 오류가 글 신뢰도를 바로 망가뜨릴 수 있기 때문이다.

## 5. 추천 모듈 설계

새 패키지는 `modules/macro/` 아래에 둔다.

```text
modules/macro/
  __init__.py
  models.py
  source_config.py
  collector.py
  document_parser.py
  metric_extractor.py
  insight_generator.py
  topic_generator.py
  quality_evaluator.py
  store.py
```

역할은 다음과 같다.

- `MacroDataCollector`: 기관별 목록 확인, 신규 문서 감지, 첨부 URL 수집, 중복 해시 생성
- `MacroDocumentParser`: PDF/HTML 본문 추출, HWP는 1차에서 링크만 저장
- `MacroMetricExtractor`: 수출 증가율, 수입 증가율, 무역수지, 품목/국가별 증감률 추출
- `MacroInsightGenerator`: 숫자의 배경, 산업별 의미, 투자자/생활인 관점 생성
- `MacroBlogTopicGenerator`: 한 문서에서 제목 후보 3~5개 생성
- `MacroQualityEvaluator`: 데이터 정확성, 출처 표시, 통찰, 투자 관련성, 생활 관련성, 문체 일관성 평가
- `MacroStore`: DB 저장과 상태 전환 담당

상태값은 첨부 제안을 그대로 따른다.

```text
MacroDocument.status
new -> downloaded -> parsed -> analyzed
failed
unsupported

MacroBlogCandidate.status
draft -> needs_review -> approved -> published
rejected
```

## 6. 저장 구조

현재 프로젝트는 SQLite 기반 `JobStore`를 폭넓게 쓰고 있다. Prisma를 바로 추가하기보다, 1차는 기존 `JobStore`에 테이블을 추가하는 방식이 더 가볍다.

추천 테이블:

```sql
macro_documents(
  id,
  source,
  title,
  published_at,
  url,
  file_url,
  file_type,
  local_path,
  status,
  hash,
  raw_text,
  parsed_json,
  metrics_json,
  insight_json,
  error_message,
  created_at,
  updated_at
)

macro_blog_candidates(
  id,
  macro_document_id,
  title,
  angle,
  target_reader,
  outline_json,
  draft_body,
  quality_json,
  status,
  created_at,
  updated_at
)
```

텍스트 보존은 압축보다 "필요한 텍스트만 보관"이 우선이다. 원문 파일은 1차에서 필수가 아니며, 원문 URL, 추출 본문, 핵심 수치 JSON, 인사이트 JSON, 후보 제목/개요만 보관하면 충분하다.

## 7. 품질 평가 기준

매크로 글은 기존 블로그 품질 평가와 별도로 아래 항목을 추가한다.

```json
{
  "dataAccuracyScore": 0,
  "sourceCitationScore": 0,
  "insightScore": 0,
  "investmentRelevanceScore": 0,
  "smallBusinessRelevanceScore": 0,
  "nonClickbaitScore": 0,
  "personaConsistencyScore": 0,
  "philosophyFrameScore": 0
}
```

판정 기준:

- 85점 미만: 자동 재작성
- 85~91점: `needs_review`
- 92점 이상: 텔레그램 승인 요청
- 95점 이상: 우수 샘플로 저장

중요한 안전장치:

- 숫자는 반드시 원문 근거 문장과 함께 저장한다.
- LLM이 만든 수치는 사용하지 않는다.
- 추출 수치가 불확실하면 `needs_review`로 보낸다.
- "다음 달 체크포인트"는 예측이 아니라 확인할 지표 목록으로 쓴다.

## 8. DeepSeek vs Qwen 가격/성능 비교

공식 가격 기준으로 보면 DeepSeek V4 Flash/Pro는 현재 코드의 방향과 잘 맞는다. DeepSeek 공식 문서는 V4 Flash와 V4 Pro 모두 JSON 출력과 tool calls를 지원하며, 컨텍스트 1M, 최대 출력 384K로 표시한다. 가격은 V4 Flash가 입력 cache miss $0.14/1M, 출력 $0.28/1M이고, V4 Pro가 입력 $0.435/1M, 출력 $0.87/1M이다. 기존 `deepseek-chat`, `deepseek-reasoner` 이름은 2026-07-24 15:59 UTC에 deprecated 예정이라고 명시되어 있어, 현재 코드가 V4 Flash/Pro로 옮겨간 것은 맞는 방향이다.

Qwen은 엔드포인트/배포 모드별 가격 차이가 있다. Alibaba Cloud 공식 문서 기준 Global `qwen-plus`는 0~128K 구간에서 입력 $0.115/1M, non-thinking 출력 $0.287/1M, thinking 출력 $1.147/1M이다. 다만 US deployment의 `qwen-plus-us`는 0~256K 구간 입력 $0.4/1M, non-thinking 출력 $1.2/1M, thinking 출력 $4/1M이다. 현재 `QwenClient`의 기본 base URL이 `dashscope-us.aliyuncs.com`이므로, 운영비 산정은 보수적으로 US 가격에 맞추는 편이 안전하다.

### 추천 배분

| 역할 | 1순위 | 2순위 | 이유 |
| --- | --- | --- | --- |
| 산업부/관세청 숫자 추출 | DeepSeek V4 Flash | Qwen Flash 추가 후 사용 | 저렴하고 JSON/tool 대응이 좋음 |
| 매크로 해석/통찰 생성 | DeepSeek V4 Pro | Qwen Plus | 철학적 해석과 투자 맥락이 중요 |
| 최종 문체 정리 | DeepSeek V4 Pro | Qwen Plus | "함께 공부하는 쉬운 문체" 유지 |
| 빠른 요약/텔레그램 후보 | DeepSeek V4 Flash | Qwen Plus | 속도와 안정성 우선 |
| 무료/저가 보조 파서 | Groq | Qwen Flash 후보 | 단순 분류/제목 후보는 고급 모델 불필요 |

운영 기본값은 다음이 좋다.

```text
기본 초안: deepseek-v4-flash
품질 강화/최종 해석: deepseek-v4-pro
문체 백업/장애 대체: qwen-plus
추가 검토 후보: qwen-flash 또는 qwen3.5-flash
```

후속 작업에서 `TEXT_MODEL_MATRIX`에는 `qwen-flash`를 추가했다. 아직 `qwen3.5-flash`, `qwen3.5-plus`는 라우터 실사용 후보가 아니므로, 공식 카탈로그에 먼저 발견 후보로 쌓고 별도 A/B 테스트를 거친 뒤 활성화하는 편이 안전하다.

## 9. 현재 신모델 자동 전환 기능 점검

확인한 코드:

- `modules/llm/model_upgrade_policy.py`
- `modules/automation/scheduler_cycles.py`
- `modules/llm/llm_router.py`
- `modules/automation/vlm_discovery_worker.py`
- `modules/automation/vlm_pricing_worker.py`
- `config/model_registry/latest.json`

적용된 부분:

1. `decide_model_upgrade()`가 비용, 품질, JSON, tool calls, 숫자 환각, 페르소나 점수, 연속 성공 횟수를 기준으로 자동 승격 여부를 판단한다.
2. 매일 평가 대상 모델을 고르고, 누적 성능이 충분하면 챔피언 모델을 자동 교체하는 로직이 있다.
3. VLM은 discovery/pricing/validation worker와 스케줄러가 있다.
4. `TEXT_MODEL_MATRIX`에 등록된 모델은 설정 저장 시 `router_registered_models`로 병합될 수 있다.

부족한 부분:

1. 텍스트 모델은 공식 페이지에서 새 모델을 자동 발견하는 worker가 없다.
2. `config/model_registry/latest.json`에는 업데이트 방식이 "수동 업데이트 후 Git commit"으로 적혀 있다.
3. 현재 로컬 DB의 `router_registered_models` 값이 빈 배열이었다. 즉, 로직은 있지만 실제 경쟁 후보 등록 상태는 아직 비어 있었다.
4. `TEXT_MODEL_MATRIX`에 없는 신모델은 자동 평가 대상이 될 수 없다.
5. 신모델 가격 변경을 감지해 `TEXT_MODEL_MATRIX` 또는 DB 카탈로그에 반영하는 경로가 없다.

따라서 결론은 **자동 전환 정책은 있음, 자동 발견/등록은 아직 미완성**이다.

## 10. 신모델 자동 업데이트 보강안

VLM worker 패턴을 텍스트 모델에도 이식한다.

추가 모듈:

```text
modules/automation/text_model_discovery_worker.py
modules/automation/text_model_pricing_worker.py
modules/automation/text_model_validation_worker.py
modules/automation/model_upgrade_orchestrator.py
```

추가 테이블:

```sql
text_model_catalog(
  provider,
  model,
  label,
  status,
  input_cost_per_1m,
  output_cost_per_1m,
  supports_json,
  supports_tool_calls,
  quality_score,
  speed_score,
  source_url,
  last_checked_at,
  metadata_json
)

text_model_discovery_events(
  event_type,
  provider,
  model,
  detail_json,
  created_at
)

text_model_validation_runs(
  provider,
  model,
  success,
  latency_ms,
  quality_score,
  persona_score,
  numeric_hallucination_count,
  created_at
)
```

자동 전환 규칙:

1. 공식 문서에서 새 모델 또는 가격 변경 감지
2. 후보는 바로 active 하지 않고 `detected`로 저장
3. API 헬스 체크
4. JSON 출력 체크
5. tool calls 지원 체크
6. 한국어 블로그 샘플 생성
7. 숫자 환각 검사
8. 페르소나/문체 점수 검사
9. 3회 이상 연속 성공
10. `decide_model_upgrade()` 통과 시 shadow 후보로 승격
11. 비용이 더 비싸면 텔레그램 승인 없이는 자동 교체 금지

이렇게 하면 "새 모델이 나왔다고 바로 바꾸는 위험"을 피하면서도, 실제로 더 싸고 좋은 모델은 자동으로 올라올 수 있다.

## 11. 실행 명령 설계

1차 구현 후 추천 CLI:

```bash
python3 scripts/macro_check.py --source motie --limit 10
python3 scripts/macro_parse.py --document-id <id>
python3 scripts/macro_analyze.py --document-id <id>
python3 scripts/macro_generate_candidates.py --document-id <id>
python3 scripts/macro_all.py --source motie --limit 5
```

서버 API는 2차로 붙인다.

```text
POST /api/macro/check
POST /api/macro/parse
POST /api/macro/analyze
POST /api/macro/generate
GET  /api/macro/documents
GET  /api/macro/candidates
```

## 12. 개발 순서

1. `macro_documents`, `macro_blog_candidates` 테이블 추가
2. `MacroDocument`, `MacroBlogCandidate` 데이터 모델 추가
3. 산업부 source config 추가
4. 산업부 목록/상세 collector 구현
5. HTML/PDF parser 구현
6. 핵심 수치 extractor 구현
7. 인사이트/후보 제목 generator 구현
8. 품질 evaluator 구현
9. 텔레그램 후보 전송 연결
10. `scripts/macro_all.py` 스모크 추가
11. fixture 기반 테스트 추가
12. 통과 후 관세청 선행 데이터로 확장
13. 텍스트 모델 discovery/pricing worker 추가
14. Qwen Flash 계열 후보 추가 및 A/B 평가

## 13. 리스크와 방어책

| 리스크 | 방어책 |
| --- | --- |
| 산업부 사이트 구조 변경 | 목록/상세 파서를 분리하고 실패 시 원문 URL만 저장 |
| HWP만 제공 | 1차에서는 `unsupported` 처리, 나중에 변환기 추가 |
| 숫자 환각 | 수치마다 원문 근거 문장 저장 |
| 정부 자료 복붙 | 요약/해석 생성 시 원문 문장 장문 인용 금지 |
| 투자 조언 과장 | 특정 종목 매수/매도 문장 금지 |
| 신모델 자동 교체 사고 | 더 비싼 모델은 텔레그램 승인 필수 |
| DB 등록 모델 비어 있음 | 라우터 초기화 또는 설정 저장 시 모델 등록 동기화 강제 |

## 14. 검증 결과

수행한 확인:

```text
./.venv/bin/python -m pytest \
  tests/test_market_briefing_design.py::test_model_upgrade_requires_budget_and_quality \
  tests/test_market_briefing_design.py::test_provider_factory_supports_deepseek_v4_default -q
```

결과:

```text
2 passed
```

추가 확인:

- `model_upgrade_policy.py`의 자동 승격 정책 존재 확인
- `scheduler_cycles.py`의 daily eval/champion switch 존재 확인
- `llm_router.py`의 `TEXT_MODEL_MATRIX`와 registered model 동기화 함수 확인
- VLM discovery/pricing worker는 존재하지만 텍스트 모델용 worker는 미존재 확인
- 현재 로컬 DB의 `router_registered_models`는 빈 배열 확인

## 15. 2026-06-05 후속 적용 결과

이번 후속 작업에서 아래 항목을 적용했다.

1. `text_model_catalog`, `text_model_price_history`, `text_model_discovery_events` 테이블 추가
2. DeepSeek/Qwen 공식 가격 페이지를 읽는 `TextModelDiscoveryWorker` 추가
3. `qwen-flash`를 `TEXT_MODEL_MATRIX`에 추가
4. `router_registered_models`를 명시적 allowlist로 해석하도록 보강
5. `.env` 키 fallback을 `LLMRouter`에 추가하되, DB에 명시 키 설정이 있을 때는 DB를 우선하도록 제한
6. 챔피언 자동 교체는 기본 비활성화하고, 성능 우수 후보를 추천 설정으로만 기록하도록 변경
7. 실제 로컬 DB에 텍스트 모델 카탈로그 63개 동기화
8. 실제 라우터 active 후보를 `qwen-flash`, `qwen-turbo`, `qwen-plus`, `deepseek-v4-flash`, `deepseek-v4-pro` 5개로 정리

현재 실제 plan은 비용 전략 기준으로 `deepseek-v4-flash`를 품질 단계 1순위로 보고, fallback에 `qwen-turbo`, `qwen-flash`, `deepseek-v4-pro`를 둔다. 이는 "DeepSeek 우선, Qwen 보조" 운영 방향과 맞다.

아직 남은 작업은 텍스트 모델 validation worker다. 지금은 공식 문서 발견과 등록까지는 되었지만, 새로 발견된 미등록 모델을 자동 샘플 생성/숫자 환각 검사/문체 검사까지 돌려 `active`로 승격하는 단계는 다음 차례다.

## 16. 참고 링크

- DeepSeek Models & Pricing: https://api-docs.deepseek.com/quick_start/pricing
- Alibaba Cloud Model Studio Pricing: https://www.alibabacloud.com/help/en/model-studio/model-pricing
- 산업통상부: https://www.motie.go.kr/
- 관세청 수출입무역통계: https://tradedata.go.kr/cts/index.do?os=0
- 한국은행 ECOS Open API: https://ecos.bok.or.kr/api/
- KOSIS OpenAPI: https://kosis.kr/openapi/index/
- 공공데이터포털: https://www.data.go.kr/
