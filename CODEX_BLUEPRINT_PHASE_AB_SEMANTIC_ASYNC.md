# CODEX Blueprint: Phase A/B (Semantic Duplicate + Async Memory Pipeline)

## 0) 목적

현재 `topic_memory` 기반 중복 방지(키워드/제목 lexical)에서 다음 2단계를 안전하게 도입한다.

- **Phase A**: 의미 기반(semantic) 중복 탐지를 결합한 **Hybrid Similarity**
- **Phase B**: 메모리 기록/백필/임베딩 계산을 생성 흐름에서 분리한 **완전 비동기 파이프라인**

핵심 목표는 다음 3가지다.

1. 의미 중복 탐지율 향상
2. 생성 지연 시간(p95) 악화 방지
3. 기존 동작 완전 하위호환(기본 OFF, feature flag)

---

## 1) 현재 기준선 (Baseline)

이미 반영된 관련 모듈:

- `modules/memory/similarity.py`: lexical 기반 유사도
- `modules/memory/gap_analyzer.py`: pre-job duplicate 검사 및 커버리지 분석
- `modules/memory/topic_store.py`: 메모리 파사드
- `modules/automation/job_store.py`: `topic_memory`, coverage/frequency/duplicate active-job 조회
- `modules/automation/trend_job_service.py`: 큐잉 전 duplicate gate
- `modules/automation/scheduler_seed.py`: idea_vault 큐잉 전 duplicate gate
- `modules/llm/content_generator.py`: 메모리 컨텍스트 주입 + ensure_backfilled
- `modules/automation/pipeline_service.py`: 발행 후 `record_post`

현재는 의미 유사도(embedding cosine)가 없고, 일부 메모리 처리 경로가 동기 컨텍스트에서 호출될 수 있다.

---

## 2) 전체 실행 전략

**원칙: 작은 단계 + 즉시 검증 + 즉시 롤백 가능**

1. Phase A 먼저 적용 (정확도 개선)
2. Phase B는 단계적으로 적용 (지연 개선)
3. 각 단계는 독립 feature flag로 ON/OFF 가능
4. 실패 시 lexical-only + 동기 경로로 즉시 복귀 가능

---

## 3) Phase A 상세 청사진 (Hybrid Semantic Duplicate)

## A-1. 설정/플래그 추가 (기본 OFF)

### 변경 파일
- `modules/config.py`
- `config/default.yaml`

### 추가 설정 (memory 섹션)
- `semantic_enabled: bool = False`
- `semantic_provider: str = "local"`  # `local` | `openai`
- `semantic_model: str = "bge-small-ko-en"` (로컬 모델 alias)
- `semantic_weight: float = 0.55`      # hybrid 비율
- `lexical_weight: float = 0.45`
- `semantic_threshold: float = 0.62`   # cosine 기준
- `hybrid_threshold: float = 0.58`     # 최종 중복 판정
- `embedding_max_candidates: int = 80`
- `embedding_timeout_sec: float = 4.0`

### ENV override
- `MEMORY_SEMANTIC_ENABLED`
- `MEMORY_SEMANTIC_PROVIDER`
- `MEMORY_SEMANTIC_MODEL`
- `MEMORY_SEMANTIC_WEIGHT`
- `MEMORY_LEXICAL_WEIGHT`
- `MEMORY_SEMANTIC_THRESHOLD`
- `MEMORY_HYBRID_THRESHOLD`

---

## A-2. 임베딩 저장 계층 도입

### 변경 파일
- `modules/automation/job_store.py`

### 신규 테이블
- `topic_memory_embeddings`
  - `job_id TEXT PRIMARY KEY`
  - `embedding_json TEXT NOT NULL`      # float list(JSON)
  - `model_name TEXT NOT NULL`
  - `dim INTEGER NOT NULL`
  - `updated_at TEXT NOT NULL`

### 인덱스
- `idx_tme_model_updated (model_name, updated_at DESC)`

### 신규 메서드
- `upsert_topic_embedding(job_id, embedding, model_name)`
- `get_topic_embeddings(job_ids: List[str], model_name: str) -> Dict[str, List[float]]`
- `list_topic_embedding_candidates(topic_mode, platform, lookback_days, limit) -> List[Dict]`

`ensure_schema()`에서 자동 생성/마이그레이션 처리한다.

---

## A-3. 임베딩 프로바이더 추상화

### 신규 파일
- `modules/memory/embedding_provider.py`

### 구성
- `EmbeddingProvider` protocol/interface
- `LocalEmbeddingProvider` (sentence-transformers 기반)
- `OpenAIEmbeddingProvider` (API 키 있을 때 선택)
- `build_embedding_provider(config)` 팩토리

### 실패 처리
- 임베딩 실패 시 예외 전파 금지
- lexical-only fallback으로 계속 진행

---

## A-4. Hybrid 유사도 엔진

### 신규 파일
- `modules/memory/hybrid_similarity.py`

### 핵심 함수
- `cosine_similarity(vec_a, vec_b) -> float`
- `hybrid_score(lexical_score, semantic_score, lexical_weight, semantic_weight) -> float`
- `find_hybrid_similar_posts(...) -> List[dict]`

점수 전략:
- semantic unavailable => lexical score 100% 사용
- semantic available => `final = lexical*w1 + semantic*w2`

---

## A-5. 기존 duplicate gate 연결

### 변경 파일
- `modules/memory/gap_analyzer.py`
- `modules/memory/topic_store.py`
- `modules/automation/trend_job_service.py`
- `modules/automation/scheduler_seed.py`

### 동작
1. 기존 lexical 후보 조회(`query_topic_memory`) 유지
2. semantic_enabled 일 때만 hybrid 경로 수행
3. `is_duplicate_before_job()`는 최종 boolean만 반환 (호출부 영향 최소화)

---

## A-6. 저장 시 임베딩 생성

### 변경 파일
- `modules/automation/pipeline_service.py`
- `modules/memory/topic_store.py`

### 동작
- `record_post()` 완료 후 임베딩 생성/저장 시도
- 실패해도 발행 성공 흐름에 영향 없음

주의: Phase B에서 완전 비동기화로 이전될 예정이므로, A 단계에서는 우선 안전성 중심으로 최소 연결만 한다.

---

## A-7. 테스트 계획

### 신규/확장 테스트
- `tests/test_memory_semantic_phaseA.py` (신규)
  - semantic off => 기존 lexical 결과 동일
  - semantic on + 유사 의미 문장 => duplicate true
  - embedding provider 실패 => fallback 정상
- `tests/test_memory_phase25.py` (기존 확장)
  - hybrid threshold 경계값 테스트

### 성공 기준
1. 기존 테스트 100% 통과
2. semantic OFF에서 회귀 0건
3. 의미 유사 샘플 탐지율 상승 확인

---

## 4) Phase B 상세 청사진 (Async Memory Pipeline)

## B-1. 1차: 인프로세스 비동기 워커 (저위험)

### 변경 파일
- `modules/automation/scheduler_service.py`
- `modules/automation/scheduler_workers.py`
- `modules/memory/topic_store.py`
- `modules/llm/content_generator.py`
- `modules/automation/pipeline_service.py`

### 신규 구성
- `memory_event_queue: asyncio.Queue`
- `memory_worker_loop(service)` 태스크

### 이벤트 타입
- `record_post`
- `ensure_backfill`
- `build_embedding`

### 적용 방식
1. `pipeline_service._record_topic_memory()`에서 직접 기록 대신 큐 적재
2. `content_generator`의 `ensure_backfilled()` 동기 호출 제거
3. worker가 순차 처리 + 실패 시 retry/backoff

---

## B-2. 2차: 영속 큐(DB) 보강 (선택, 권장)

### 변경 파일
- `modules/automation/job_store.py`
- `modules/automation/scheduler_workers.py`

### 신규 테이블
- `memory_task_queue`
  - `id INTEGER PK`
  - `task_type TEXT`
  - `payload_json TEXT`
  - `status TEXT` (`queued|running|failed|done`)
  - `attempts INTEGER`
  - `next_run_at TEXT`
  - `created_at TEXT`
  - `updated_at TEXT`

### 목적
- 프로세스 재시작 후에도 메모리 작업 유실 방지
- 운영 관측성(실패 건수, 적체량) 확보

---

## B-3. 성능/운영 지표 추가

### 로그/메트릭
- queue depth
- task throughput/min
- retry count
- dead-letter(최종 실패) count
- 메모리 처리 평균 지연(ms)

### 알림
- DLQ 임계치 초과 시 notifier(telegram) 경고

---

## B-4. 테스트 계획

### 신규 테스트
- `tests/test_memory_async_phaseB.py`
  - 큐 적재 후 worker 처리 성공
  - 실패 재시도/backoff
  - worker down/up 재가동 시 처리 재개

### 회귀 테스트
- 생성/발행/스케줄러 주요 테스트 세트 재실행
- p95 latency 비교(변경 전/후)

---

## 5) 배포/롤백 전략

## 배포 순서
1. A-1~A-3 배포 (semantic OFF)
2. A-4~A-6 배포 후 semantic canary ON (10%)
3. B-1 배포 (async queue ON)
4. 안정화 후 B-2 영속 큐 도입

## 롤백 스위치
- `memory.semantic_enabled=false`
- `memory.async_pipeline_enabled=false` (신규 플래그)

## 롤백 기준
1. duplicate false negative 급증
2. 생성 p95 latency 15% 이상 악화
3. memory worker 실패율 5% 초과(10분 이동창)

---

## 6) 구현 순서 (실행 체크리스트)

1. Config/ENV 플래그 추가
2. `topic_memory_embeddings` 스키마 + CRUD
3. embedding provider abstraction
4. hybrid similarity + gap_analyzer 연결
5. record_post 임베딩 저장 연결
6. Phase A 테스트/검증
7. async memory queue/worker 추가 (B-1)
8. 동기 경로 제거 및 큐 경로 전환
9. B-1 테스트/성능 비교
10. 필요 시 영속 큐(B-2) 확장

---

## 7) 제미나이 검토 요청용 체크리스트

아래 항목을 제미나이에게 검토 요청한다.

1. Hybrid 가중치(`semantic_weight/lexical_weight`) 초기값 타당성
2. `hybrid_threshold` 권장값(토픽별 차등 필요 여부)
3. B-1에서 B-2(영속 큐) 전환 시점 기준
4. 임베딩 모델 선택(local/openai) 비용 대비 정확도
5. false positive 최소화를 위한 보조 규칙(예: 동일 토픽 가중치)

---

## 8) 최종 결정 포인트

코딩 시작 전 확정할 의사결정 3개:

1. 임베딩 기본 provider: `local` vs `openai`
2. B-2(영속 큐) 동시 착수 여부: `즉시` vs `B-1 안정화 후`
3. canary 범위: `토픽 1개(it)` vs `전체 토픽`

