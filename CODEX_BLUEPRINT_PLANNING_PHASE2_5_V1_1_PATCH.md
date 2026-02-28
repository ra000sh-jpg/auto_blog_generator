# CODEX BLUEPRINT — Phase 2.5 보정판 (V1.1)

> 대상 원본: `CODEX_BLUEPRINT_PLANNING_PHASE2_5.md`  
> 목적: 코덱스 리뷰/실코드 대조에서 확인된 P0/P1 리스크를 선반영한다.

---

## 왜 보정이 필요한가

원본 Phase 2.5 방향은 맞지만, 현재 코드 기준으로 아래 4개 문제가 있었다.

1. `TrendJobService._has_recent_job()`가 `topic_memory`만 보면, 발행 전 중복(`queued/running/ready_to_publish`)이 통과될 수 있음.
2. `query_topic_memory(platform=)` 파라미터를 중간 삽입하면 위치 인자 호환성이 깨질 수 있음.
3. `_has_recent_job(days=7)`의 `days` 의미가 사라지면 API 계약이 불명확해짐.
4. `idea_vault` 중복 스킵 시 단순 release만 하면 같은 아이템이 반복 선점될 수 있음.

---

## PATCH A — job_store.py 보정 (P0)

### A-1. `query_topic_memory()` 시그니처: `platform`은 **맨 뒤**에 추가

기존:
```python
def query_topic_memory(
    self,
    topic_mode: str = "",
    persona_id: str = "",
    lookback_days: int = 56,
    limit: int = 30,
    min_quality_score: int = 0,
) -> List[Dict[str, Any]]:
```

보정:
```python
def query_topic_memory(
    self,
    topic_mode: str = "",
    persona_id: str = "",
    lookback_days: int = 56,
    limit: int = 30,
    min_quality_score: int = 0,
    platform: str = "",  # NEW (맨 뒤)
) -> List[Dict[str, Any]]:
```

### A-2. SELECT/결과에 `platform` 포함

`SELECT`에 `platform`을 추가하고 반환 dict에도 `platform` 키를 넣는다.

### A-3. `topic_memory` 인덱스 추가

`_init_tables()`에 아래 인덱스 추가:
```sql
CREATE INDEX IF NOT EXISTS idx_tm_platform_recorded
ON topic_memory(platform, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_tm_topic_platform_recorded
ON topic_memory(topic_mode, platform, recorded_at DESC);
```

### A-4. 신규 메서드: 발행 전 중복 조회

`jobs` 테이블을 직접 조회하는 메서드를 추가:

```python
def has_recent_similar_active_job(
    self,
    keyword: str,
    topic_mode: str = "",
    platform: str = "",
    lookback_days: int = 7,
) -> bool:
    """최근 활성 작업(queued/running/retry_wait/ready_to_publish/awaiting_images) 중
    키워드 유사 항목 존재 여부를 반환한다."""
```

권장 조건:
- status IN (`queued`, `running`, `retry_wait`, `ready_to_publish`, `awaiting_images`)
- `updated_at >= cutoff OR scheduled_at >= cutoff`
- `title LIKE ? OR seed_keywords LIKE ?`
- `topic_mode` 필터는 `jobs.category`/`seo_snapshot` 의존성이 커서 Phase 2.5에서는 optional 처리

---

## PATCH B — GapAnalyzer/TopicMemoryStore 보정 (P1)

### B-1. 하드코딩 0.50 제거

`similarity_threshold=0.50` 하드코딩 대신 설정 우선:

```python
precheck_threshold = float(
    getattr(self._config, "precheck_duplicate_threshold", 0.50)
)
```

### B-2. `days` 의미 보존을 위한 선택 파라미터

`is_duplicate_before_job()`에 `lookback_days` optional 인자를 추가하거나,
호출 측에서 `lookback_weeks = ceil(days / 7)`로 변환해 전달한다.

---

## PATCH C — config.py 확장 (P1)

현재 `MemoryConfig`에 사전 중복용 설정을 추가:

```python
precheck_duplicate_threshold: float = 0.50
idea_vault_duplicate_cooldown_days: int = 7
```

`_apply_env_overrides()`에도 추가:

```python
"MEMORY_PRECHECK_DUPLICATE_THRESHOLD": ("memory", "precheck_duplicate_threshold", float),
"MEMORY_IDEA_VAULT_DUP_COOLDOWN_DAYS": ("memory", "idea_vault_duplicate_cooldown_days", int),
```

---

## PATCH D — trend_job_service.py 보정 (P0)

### D-1. `__init__`에 `memory_store` 추가

```python
from typing import Any, List, Optional
...
memory_store: Optional[Any] = None
...
self.memory_store = memory_store
```

### D-2. `_create_job_from_keyword()` 호출 순서 수정

현재는 `_has_recent_job(keyword)`를 먼저 호출함.  
보정: `topic_mode`를 먼저 계산하고 `_has_recent_job(..., topic_mode=topic_mode)`로 전달.

### D-3. `_has_recent_job()` 2단계 검사

```python
def _has_recent_job(self, keyword: str, days: int = 7, topic_mode: str = "") -> bool:
    # 1) 활성 작업 중복 검사 (P0)
    if self.job_store.has_recent_similar_active_job(...):
        return True

    # 2) 발행 이력(topic_memory) 검사 (memory_store 있을 때만)
    if self.memory_store is not None:
        lookback_weeks = max(1, (max(1, int(days)) + 6) // 7)
        return self.memory_store.is_duplicate_before_job(
            title=keyword,
            keywords=[keyword],
            topic_mode=topic_mode,
            similarity_threshold=getattr(self.memory_store._config, "precheck_duplicate_threshold", 0.50),
            platform=self.platform,
            lookback_weeks=lookback_weeks,
        )
    return False
```

핵심:
- `days`를 실제 사용한다.
- memory_store가 없어도 활성 작업 중복은 막는다.

---

## PATCH E — scheduler_seed.py 보정 (P1)

idea_vault 큐잉 전 중복 체크는 유지하되, 반복 선점 방지 장치를 추가한다.

### E-1. 최소 보정(스키마 변경 없음)

중복으로 스킵한 아이디어 ID를 당일 배치에서 재선점하지 않도록 메모리 집합 사용:

```python
seen_duplicate_idea_ids: set[int] = set()
...
if int(claimed.get("id", 0)) in seen_duplicate_idea_ids:
    continue
...
if _is_dup:
    seen_duplicate_idea_ids.add(int(claimed.get("id", 0)))
    release_fn(idea_job_id)
    continue
```

### E-2. 권장 보정(스키마 변경)

`idea_vault`에 `next_eligible_at` 컬럼을 추가하고,
중복 스킵 시 `next_eligible_at = now + cooldown_days`로 미뤄 반복 큐잉을 방지한다.

---

## PATCH F — scheduler_service.py / scheduler_cycles.py 보정 (P1)

### F-1. `SchedulerService.__init__`에 `memory_store` 파라미터 추가

```python
memory_store: Optional[Any] = None
...
self.memory_store = memory_store
```

### F-2. `run_scheduler_forever()` 초기화 순서 조정

현재 `trend_service = TrendJobService(job_store=job_store)`가 memory_store 생성보다 먼저 있음.  
보정: `_scheduler_memory_store` 생성 후 `TrendJobService(..., memory_store=...)` 생성.

### F-3. `service_cls(...)` 생성 시도에도 `memory_store` 전달

```python
scheduler = service_cls(
    ...,
    memory_store=_scheduler_memory_store,
)
```

---

## PATCH G — 테스트/검증 보정 (P1)

원본 체크리스트의 platform 검증 코드에 `or True`가 있어 무효다. 아래로 교체:

```python
all_naver = all(str(r.get("platform", "")) == "naver" for r in results)
assert all_naver
```

추가 테스트 권장:
1. `TrendJobService._has_recent_job()`  
   - 활성 jobs 중복으로 True 반환
   - memory_store=None이어도 활성 jobs 중복은 차단
2. `query_topic_memory(platform=...)`  
   - 반환 row에 `platform` 포함
3. `scheduler_seed` idea_vault duplicate gate  
   - 중복 시 schedule_job 미호출 + release 호출

---

## 적용 순서 (보정판)

1. `job_store.py` (A 전부)  
2. `config.py` (C)  
3. `gap_analyzer.py` + `topic_store.py` (B)  
4. `trend_job_service.py` (D)  
5. `scheduler_seed.py` (E)  
6. `scheduler_cycles.py` + `scheduler_service.py` (F)  
7. 테스트 보강 (G)

---

## Definition of Done (보정판)

1. 트렌드 중복 검사가 `활성 jobs + topic_memory` 두 단계로 동작한다.  
2. `_has_recent_job(days)`에서 `days` 인자가 실제 로직에 반영된다.  
3. `query_topic_memory(platform=...)`가 하위 호환을 깨지 않는다(파라미터 맨 뒤).  
4. `topic_memory` platform 인덱스가 추가되어 조회 성능 저하를 막는다.  
5. idea_vault 중복 아이템이 같은 배치/단기 주기에서 반복 선점되지 않는다.  
6. 기존 테스트 + 신규 중복 테스트가 통과한다.

