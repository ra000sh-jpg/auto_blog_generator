# HANDOFF: 25_QUALITY_GATE_AND_AUTO_CORRECTION (3단 분리형 품질 게이트 및 자가 수정)

## 1. 개요 및 목적

Phase 24에서 생성 워커(Generator)와 발행 워커(Publisher)가 분리되어, 유휴 시간(Idle)에 초안을 비축하는 시스템이 완성되었습니다.
본 Phase 25의 핵심 목표는 비축된 초안이 발행 워커로 넘어가기 전에 **엄격한 품질 검증(Quality Gate)을 거치도록 하고, 기준 미달 시 AI가 스스로 피드백을 반영해 재작성(Auto-Correction)하는 자율 정화 루프**를 구축하는 것입니다.

저품질 포스팅으로 인한 네이버 블로그 지수 하락(저품질 블로그화)을 방지하고, 철저히 검증된 "전문가 수준"의 글만 발행되도록 통제합니다.

## 2. 핵심 구현 아키텍처 요구사항

### 2.1 🚧 3단계 품질 게이트 (Quality Evaluator) 신설

별도의 평가 모듈(`modules/quality_gate/evaluator.py`)을 구성하여 다음 3단계를 순차적으로 검증합니다.

1. **Gate 1: Rule-based 기초 검사 (Hard Filter)**
   - 금칙어/스팸 키워드 포함 여부 검사
   - 최소/최대 길이(글자 수) 제한 통과 여부
   - 필수 HTML 태그 구조 정상 여부
2. **Gate 2: 페르소나 & 톤앤매너 일치도 (Soft Filter - LLM 경량 모델)**
   - 설정된 Persona(예: 냉철한 팩트폭격기)의 어투와 형식을 준수했는지 점수화(0~100점).
3. **Gate 3: 환각(Hallucination) 및 팩트 체크 (Deep Filter)**
   - 경제/IT 등 사실 기반 카테고리의 경우, 제공된 검색 Context(RAG 데이터)와 본문 내용이 모순되지 않는지 검증(Faithfulness 검사).

### 2.2 🔄 자가 수정 루프 (Auto-Correction Loop)

품질 평가 결과가 기준 점수(예: 80점)에 미달할 경우, 즉시 실패 처리하지 않고 피드백을 통해 재작성을 유도합니다.

- Evaluator는 감점 사유가 담긴 **"수정 지시 프롬프트(Feedback)"**를 반환합니다.
- Generator는 이 Feedback을 참고하여 기존의 본문을 덧대어 1~2회 추가 수정을 진행합니다.
- 최대 재시도(Max Retries = 2)를 초과해도 기준 미달일 경우, 해당 Job을 영구 실패 처리하고 사용자(텔레그램)에게 보고합니다.

### 2.3 📦 데이터베이스(JobStore) 상태 및 메타데이터 확장

`jobs` 테이블 내 초안 검증 상태를 세분화하여 추가합니다.

- (기존) `pending_generation` → `draft_ready`
- **(신규)** `pending_generation` → `generated` (생성 완료) → `evaluating` (검증 중) → `draft_ready` (발행 대기)
- 실패 시: `failed_quality` 상태 추가.
- `metadata` JSON에 `quality_score`, `evaluation_feedback`, `retry_count` 필드 추가.

### 2.4 🧩 파이프라인 리팩토링 (`pipeline_service.py`)

초안 생성 완료 후 Quality Gate를 호출하는 로직이 결합되어야 합니다.

- `run_quality_evaluator()` : `generated` 상태의 Job을 가져와 평가 진행.
- 통과 시 `draft_ready`로 상태 변경. 반려 시 재시도 횟수 내라면 다시 `pending_generation` (혹은 `correction_needed`)으로 되돌리고 Feedback을 DB에 저장.

---

## 3. 구현 액션 플랜 (Step-by-Step)

1. **[Step 1] DB Schema & JobStore 업데이트**
   - DB 상태(`evaluating`, `failed_quality`) 및 메타데이터 갱신 로직 추가.
2. **[Step 2] Evaluator 모듈 구현 (`modules/quality_gate/evaluator.py`)**
   - 3단계 검증 로직 구현. 특히 LLM-based Evaluator는 설정된 `router_settings`의 경량 모델(예: Gemma, Qwen 등)을 최우선으로 매칭하여 비용 증가를 방어.
3. **[Step 3] 자가 수정 (Correction) 프롬프트 및 파이프라인 통합**
   - `pipeline_service.py` 내의 텍스트 생성 파트(`generate_text`)에서 이전 Feedback이 존재할 경우 프롬프트에 주입하는 분기 처리.
4. **[Step 4] 시뮬레이션 및 테스트**
   - 의도적으로 금칙어를 넣거나 페르소나를 위반한 텍스트를 Mocking으로 넣어 Evaluator가 반려하고 재작성 루프를 도는지 E2E 테스트(`tests/test_quality_gate.py`) 작성 및 통과 확인.

---

## 4. 향후 확장 로드맵 (Phase 26: 수익성 기반 지능형 카테고리 추천)

온보딩 초기 단계에서 시스템이 "가장 수익성이 좋은 카테고리"를 제안하여 사용자의 블로그 수익화를 극대화하는 기능을 추후 고도화할 예정입니다.

1. **(현재 반영됨) 1단계: 하드코딩 추천 UI (수익성 Chip)**
   - 프론트엔드 온보딩 폼에 고단가 보장 카테고리(예: IT/테크, 재테크, 건강, 부동산)를 Chip 형태로 띄워 원클릭으로 추가할 수 있게 유도.
2. **2단계: 백엔드 랭킹 API 연동**
   - 크롤러나 외부 API(네이버 검색량, 구글 트렌드, 애드센스 단가표 등)를 바탕으로 주간 단위 고수익 카테고리 TOP 10을 내려주는 별도 API 구축.
3. **3단계: 페르소나 랩 연계형 지능형 프롬프트 추천**
   - 페르소나 분석 결과와 사용자의 관심사를 바탕으로 LLM 스무딩을 거쳐 "선택하신 '전문가' 페르소나에 맞는 고수익 'IT 기기 리뷰' 주제를 추천합니다" 형태로 자연스러운 Nudge(넛지) 유도 탑재.
