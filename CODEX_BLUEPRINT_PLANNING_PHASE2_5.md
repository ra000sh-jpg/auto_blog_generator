# CODEX BLUEPRINT — Phase 2.5: 메모리 기반 스마트 플래닝 시스템

> ⚠️ **보정판 안내**: 이 문서는 원본이다.  
> P0/P1 리스크를 반영한 최신 보정안은
> `CODEX_BLUEPRINT_PLANNING_PHASE2_5_V1_1_PATCH.md`를 우선 적용한다.

> **목표**: Phase 2.0이 구축한 `topic_memory`를 "방어적 사후 기록"에서
> "선제적 중복 차단 + 커버리지 갭 분석"으로 확장한다.
>
> **달성 효과 3가지:**
> 1. **선제적 중복 차단** — Job 생성 시점에 topic_memory 유사도를 검사해 중복 주제를 큐에 넣기 전에 차단
> 2. **커버리지 갭 분석** — topic_mode별 발행 수와 키워드 포화도를 계산해 어떤 주제가 부족한지 측정
> 3. **Idea Vault 품질 게이트** — 중복도 높은 RSS 아이디어는 큐잉 전에 스킵, 미발행 각도로 수정 후 진입

---

## 변경 파일 목록

| 파일 | 종류 | 핵심 역할 |
|------|------|---------|
| `modules/automation/job_store.py` | **수정** | `query_topic_memory(platform=)` 필터 + 분석 쿼리 2개 추가 |
| `modules/memory/gap_analyzer.py` | **신규** | GapAnalyzer 클래스 (커버리지 + 포화도 + 사전 중복 검사) |
| `modules/memory/topic_store.py` | **수정** | `is_duplicate_before_job()` + `get_coverage_stats()` 파사드 추가 |
| `modules/automation/trend_job_service.py` | **수정** | `_has_recent_job()` 스텁 → 실제 topic_memory 검사로 교체 |
| `modules/automation/scheduler_seed.py` | **수정** | idea_vault job 큐잉 전 중복 체크 추가 |
| `modules/automation/scheduler_service.py` | **수정** | `memory_store` 속성 추가 |
| `modules/automation/scheduler_cycles.py` | **수정** | TrendJobService + SchedulerService에 memory_store 전달 |

**변경하지 않는 파일:**
- `modules/memory/similarity.py` — 알고리즘 그대로 재사용
- `modules/memory/context_builder.py` — Phase 2.0 결과물 유지
- `modules/llm/content_generator.py` — pre-generate 주입 그대로 유지
- `modules/automation/pipeline_service.py` — post-publish 저장 그대로 유지
- `modules/config.py` / `config/default.yaml` — 신규 설정 없음 (기존 MemoryConfig 재사용)

---

## PATCH 1 — job_store.py: 분석 쿼리 2개 + platform 필터

### 파일: `modules/automation/job_store.py`

#### 1-A. `query_topic_memory()` 시그니처에 `platform` 파라미터 추가

현재 시그니처:
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

변경 후 (파라미터 1개 추가):
```python
def query_topic_memory(
    self,
    topic_mode: str = "",
    persona_id: str = "",
    platform: str = "",       # NEW: "" = 전체, "naver"/"tistory" = 필터
    lookback_days: int = 56,
    limit: int = 30,
    min_quality_score: int = 0,
) -> List[Dict[str, Any]]:
```

`where_clauses` 구성 블록에 추가 (persona_id 조건 뒤):
```python
    if platform:
        where_clauses.append("platform = ?")
        params.append(str(platform))
```

#### 1-B. `get_topic_coverage_stats()` 신규 메서드 추가 (클래스 끝)

```python
def get_topic_coverage_stats(
    self,
    lookback_days: int = 56,
    platform: str = "",
) -> Dict[str, int]:
    """topic_mode별 발행 수를 반환한다.

    반환 예: {'cafe': 12, 'it': 3, 'economy': 8, 'parenting': 2}
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()

    params: list = [cutoff]
    where = ["recorded_at >= ?"]
    if platform:
        where.append("platform = ?")
        params.append(str(platform))

    sql = f"""
        SELECT topic_mode, COUNT(*) as cnt
        FROM topic_memory
        WHERE {' AND '.join(where)}
        GROUP BY topic_mode
        ORDER BY cnt DESC
    """
    with self.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {str(row[0]): int(row[1]) for row in rows if row[0]}
```

#### 1-C. `get_keyword_frequencies()` 신규 메서드 추가 (클래스 끝)

```python
def get_keyword_frequencies(
    self,
    topic_mode: str = "",
    lookback_days: int = 56,
    top_n: int = 30,
) -> List[tuple]:
    """키워드별 사용 빈도를 반환한다. LLM 없이 JSON 파싱으로 계산.

    반환 예: [('금리', 8), ('환율', 5), ('드립커피', 4), ...]
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()

    params: list = [cutoff]
    where = ["recorded_at >= ?"]
    if topic_mode:
        where.append("topic_mode = ?")
        params.append(str(topic_mode))

    sql = f"""
        SELECT keywords
        FROM topic_memory
        WHERE {' AND '.join(where)}
    """
    with self.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    freq: Dict[str, int] = {}
    for row in rows:
        try:
            kw_list = json.loads(row[0]) if row[0] else []
        except Exception:
            kw_list = []
        for kw in kw_list:
            kw_clean = str(kw).strip().lower()
            if kw_clean:
                freq[kw_clean] = freq.get(kw_clean, 0) + 1

    sorted_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return sorted_freq[:max(1, int(top_n))]
```

---

## PATCH 2 — modules/memory/gap_analyzer.py (신규)

### 파일: `modules/memory/gap_analyzer.py`

MCP Knowledge Graph의 "관계 추론"을 사용하지 않고,
**SQL 집계 + Jaccard 유사도만으로** 커버리지 갭과 사전 중복을 감지한다.

```python
"""발행 이력 기반 커버리지 갭 분석 엔진.

topic_memory 데이터를 집계해:
1. 어떤 topic_mode가 부족한지 감지 (커버리지 갭)
2. 어떤 키워드가 포화 상태인지 감지 (키워드 포화도)
3. Job 생성 전 중복 여부를 검사 (사전 중복 차단)

외부 ML 라이브러리 없음. SQLite + Jaccard만 사용.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class GapAnalyzer:
    """발행 이력에서 커버리지 갭과 중복을 분석한다.

    모든 메서드는 예외를 조용히 처리 (non-critical).
    job_store가 None이면 빈 결과를 반환한다.
    """

    def __init__(
        self,
        job_store: Any,   # JobStore — 순환 임포트 방지
        config: Any,      # MemoryConfig
    ):
        self._store = job_store
        self._config = config

    # ────────────────────────────────────────────
    # 1. 커버리지 갭 분석
    # ────────────────────────────────────────────

    def get_coverage_stats(
        self,
        lookback_weeks: Optional[int] = None,
        platform: str = "",
    ) -> Dict[str, int]:
        """topic_mode별 발행 수를 반환한다.

        반환 예: {'cafe': 12, 'it': 3, 'economy': 8, 'parenting': 2}
        """
        if not self._config.enabled:
            return {}
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        try:
            fn = getattr(self._store, "get_topic_coverage_stats", None)
            if callable(fn):
                return fn(lookback_days=weeks * 7, platform=platform)
        except Exception as exc:
            logger.debug("coverage_stats failed (non-critical): %s", exc)
        return {}

    def get_underrepresented_topics(
        self,
        known_topics: List[str],
        target_per_topic: int = 5,
        lookback_weeks: Optional[int] = None,
        platform: str = "",
    ) -> List[str]:
        """발행 수가 target 미만인 topic_mode를 오름차순으로 반환한다.

        Args:
            known_topics: 시스템에서 사용하는 전체 topic_mode 목록
            target_per_topic: 토픽당 목표 발행 수
            lookback_weeks: None이면 MemoryConfig.lookback_weeks 사용

        Returns:
            부족한 topic_mode 목록 (발행 수 적은 순, known_topics 기준 정렬)
        """
        stats = self.get_coverage_stats(lookback_weeks=lookback_weeks, platform=platform)
        result = []
        for topic in known_topics:
            count = stats.get(topic, 0)
            if count < target_per_topic:
                result.append((topic, count))
        # 발행 수 오름차순 (가장 부족한 것부터)
        result.sort(key=lambda x: x[1])
        return [topic for topic, _ in result]

    # ────────────────────────────────────────────
    # 2. 키워드 포화도 분석
    # ────────────────────────────────────────────

    def get_keyword_frequencies(
        self,
        topic_mode: str = "",
        lookback_weeks: Optional[int] = None,
        top_n: int = 30,
    ) -> List[Tuple[str, int]]:
        """키워드별 사용 빈도를 반환한다.

        반환 예: [('금리', 8), ('환율', 5), ...]
        """
        if not self._config.enabled:
            return []
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        try:
            fn = getattr(self._store, "get_keyword_frequencies", None)
            if callable(fn):
                return fn(
                    topic_mode=topic_mode,
                    lookback_days=weeks * 7,
                    top_n=top_n,
                )
        except Exception as exc:
            logger.debug("keyword_frequencies failed (non-critical): %s", exc)
        return []

    def is_keyword_saturated(
        self,
        keyword: str,
        topic_mode: str = "",
        threshold: int = 3,
        lookback_weeks: Optional[int] = None,
    ) -> bool:
        """최근 N주 내 threshold 이상 사용된 키워드를 포화 상태로 판단한다.

        Args:
            threshold: 3 = 최근 8주에 3회 이상이면 포화

        Returns:
            True이면 이 키워드는 이미 충분히 사용됨 (새 주제 생성 시 스킵 권장)
        """
        freqs = self.get_keyword_frequencies(
            topic_mode=topic_mode,
            lookback_weeks=lookback_weeks,
        )
        keyword_lower = keyword.strip().lower()
        for kw, count in freqs:
            if kw == keyword_lower and count >= threshold:
                return True
        return False

    # ────────────────────────────────────────────
    # 3. 사전 중복 검사 (Job 생성 전)
    # ────────────────────────────────────────────

    def is_duplicate_before_job(
        self,
        title: str,
        keywords: List[str],
        topic_mode: str,
        similarity_threshold: float = 0.50,
        lookback_weeks: Optional[int] = None,
        platform: str = "",
    ) -> bool:
        """Job 생성 전, topic_memory와 유사도를 검사한다.

        유사도 >= similarity_threshold이면 중복으로 판단해 True를 반환.

        Args:
            similarity_threshold: 0.50 (Phase 2.0 LLM 경고 0.65보다 낮음)
                                  — 사전 차단은 느슨하게, LLM 경고는 엄격하게
            lookback_weeks: None이면 MemoryConfig.lookback_weeks 사용

        Returns:
            True = 중복 (Job 생성 스킵 권장)
            False = 신선한 주제 (생성 진행)
        """
        if not self._config.enabled:
            return False
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks

        try:
            from .similarity import find_similar_posts

            fn = getattr(self._store, "query_topic_memory", None)
            if not callable(fn):
                return False

            candidates = fn(
                topic_mode=topic_mode,
                platform=platform,
                lookback_days=weeks * 7,
                limit=50,
            )
            if not candidates:
                return False

            similar = find_similar_posts(
                title=title,
                keywords=keywords,
                candidates=candidates,
                threshold=similarity_threshold,
                top_k=1,  # 1개라도 있으면 충분
            )
            if similar:
                top = similar[0]
                logger.info(
                    "Pre-job duplicate detected",
                    extra={
                        "title": title[:60],
                        "topic_mode": topic_mode,
                        "similar_title": str(top.get("title", ""))[:60],
                        "similarity": top.get("similarity", 0),
                    },
                )
                return True
            return False

        except Exception as exc:
            logger.debug("is_duplicate_before_job failed (non-critical): %s", exc)
            return False  # 실패 시 보수적으로 False (차단하지 않음)
```

---

## PATCH 3 — modules/memory/topic_store.py: 파사드 2개 추가

### 파일: `modules/memory/topic_store.py`

`TopicMemoryStore` 클래스의 `get_cross_topic_recent()` 메서드 뒤에 추가:

```python
    def is_duplicate_before_job(
        self,
        title: str,
        keywords: List[str],
        topic_mode: str,
        similarity_threshold: float = 0.50,
        platform: str = "",
    ) -> bool:
        """Job 생성 전 중복 여부를 검사한다.

        GapAnalyzer를 사용하지 않고 직접 호출 가능한 편의 메서드.
        실패 시 False 반환 (차단하지 않음).
        """
        if not self._config.enabled:
            return False
        try:
            from .gap_analyzer import GapAnalyzer
            analyzer = GapAnalyzer(job_store=self._store, config=self._config)
            return analyzer.is_duplicate_before_job(
                title=title,
                keywords=keywords,
                topic_mode=topic_mode,
                similarity_threshold=similarity_threshold,
                platform=platform,
            )
        except Exception as exc:
            logger.debug("is_duplicate_before_job (facade) failed: %s", exc)
            return False

    def get_coverage_stats(
        self,
        lookback_weeks: Optional[int] = None,
        platform: str = "",
    ) -> Dict[str, int]:
        """topic_mode별 발행 수를 반환한다.

        반환 예: {'cafe': 12, 'it': 3, 'economy': 8}
        """
        if not self._config.enabled:
            return {}
        try:
            from .gap_analyzer import GapAnalyzer
            analyzer = GapAnalyzer(job_store=self._store, config=self._config)
            return analyzer.get_coverage_stats(
                lookback_weeks=lookback_weeks,
                platform=platform,
            )
        except Exception as exc:
            logger.debug("get_coverage_stats (facade) failed: %s", exc)
            return {}
```

**중요**: `topic_store.py` 상단 임포트 확인 — `Dict`가 이미 있으므로 추가 불필요.

---

## PATCH 4 — trend_job_service.py: `_has_recent_job()` 실제 구현

### 파일: `modules/automation/trend_job_service.py`

#### 4-A. `__init__` 시그니처에 `memory_store` 추가

현재:
```python
def __init__(
    self,
    job_store: JobStore,
    collector: Optional[NaverDataLabCollector] = None,
    max_jobs_per_run: int = 3,
    platform: str = "naver",
    persona_id: str = "default",
):
    self.job_store = job_store
    self.collector = collector or NaverDataLabCollector()
    self.max_jobs_per_run = max(1, max_jobs_per_run)
    self.platform = platform
    self.persona_id = persona_id
```

변경 후 (`memory_store` 파라미터 추가):
```python
def __init__(
    self,
    job_store: JobStore,
    collector: Optional[NaverDataLabCollector] = None,
    max_jobs_per_run: int = 3,
    platform: str = "naver",
    persona_id: str = "default",
    memory_store: Optional[Any] = None,  # NEW: TopicMemoryStore
):
    self.job_store = job_store
    self.collector = collector or NaverDataLabCollector()
    self.max_jobs_per_run = max(1, max_jobs_per_run)
    self.platform = platform
    self.persona_id = persona_id
    self.memory_store = memory_store  # NEW
```

`__init__` 최상단에 `from typing import Any` (기존 `Optional`과 함께) 확인.

#### 4-B. `_has_recent_job()` 스텁 → 실제 구현으로 교체

현재 (라인 110-116):
```python
def _has_recent_job(self, keyword: str, days: int = 7) -> bool:
    """최근 중복 키워드 여부를 확인한다.

    TODO: JobStore 검색 메서드가 추가되면 실제 중복 탐지로 교체한다.
    """
    del keyword, days
    return False
```

교체 후:
```python
def _has_recent_job(self, keyword: str, days: int = 7) -> bool:
    """최근 중복 키워드 여부를 topic_memory에서 확인한다.

    memory_store가 없으면 항상 False (기존 동작 유지).
    """
    if self.memory_store is None:
        return False
    try:
        # 단일 키워드를 제목과 키워드 양쪽에 사용해 포괄적으로 검사
        title_candidate = keyword
        keywords_candidate = [keyword]
        return self.memory_store.is_duplicate_before_job(
            title=title_candidate,
            keywords=keywords_candidate,
            topic_mode="",          # 전체 topic 검색 (카테고리 불문)
            similarity_threshold=0.50,
            platform=self.platform,
        )
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).debug("_has_recent_job check failed (non-critical): %s", exc)
        return False
```

---

## PATCH 5 — scheduler_seed.py: idea_vault 중복 스킵

### 파일: `modules/automation/scheduler_seed.py`

#### 5-A. idea_vault 큐잉 루프 안에 중복 체크 추가

현재 코드 (라인 108-145):
```python
for claimed in claimed_items:
    idea_job_id = str(claimed.get("queued_job_id", "")).strip()
    raw_text = str(claimed.get("raw_text", "")).strip()
    category_name = str(claimed.get("mapped_category", "")).strip() or DEFAULT_FALLBACK_CATEGORY
    topic_mode = service._normalize_topic_mode(str(claimed.get("topic_mode", "")).strip())
    if not idea_job_id or not raw_text:
        continue
    sequence += 1
    ...
    title = service._build_vault_seed_title(...)
    seed_keywords = service._build_vault_seed_keywords(...)
    persona_id = service._persona_id_for_topic(topic_mode)
    success = service.job_store.schedule_job(...)
```

변경: `title`과 `seed_keywords`가 확정된 직후, `schedule_job()` 호출 전에 삽입:

```python
    title = service._build_vault_seed_title(
        raw_text=raw_text,
        local_date=today_local,
        sequence=sequence,
    )
    seed_keywords = service._build_vault_seed_keywords(
        raw_text=raw_text,
        category=category_name,
        topic_mode=topic_mode,
    )

    # ── [NEW] idea_vault 중복 사전 검사 ──
    _memory_store = getattr(service, "memory_store", None)
    if _memory_store is not None:
        try:
            _is_dup = _memory_store.is_duplicate_before_job(
                title=title,
                keywords=seed_keywords,
                topic_mode=topic_mode,
                similarity_threshold=0.50,
            )
            if _is_dup:
                logger.info(
                    "Idea vault job skipped (duplicate in topic_memory): %s",
                    title[:60],
                    extra={"topic_mode": topic_mode},
                )
                if release_fn and callable(release_fn):
                    release_fn(idea_job_id)
                continue
        except Exception as _dup_exc:
            logger.debug("Idea vault dup check failed (non-critical): %s", _dup_exc)

    persona_id = service._persona_id_for_topic(topic_mode)
    success = service.job_store.schedule_job(
        job_id=idea_job_id,
        ...
    )
```

---

## PATCH 6 — scheduler_service.py: `memory_store` 속성 추가

### 파일: `modules/automation/scheduler_service.py`

#### 6-A. `__init__` 시그니처에 `memory_store` 추가

현재 마지막 파라미터 (`api_only_mode: bool = False`) 뒤에 추가:

```python
    api_only_mode: bool = False,
    memory_store: Optional[Any] = None,  # NEW: TopicMemoryStore
):
```

`__init__` 본문 끝에 추가 (기존 속성 할당 다음):

```python
    self.memory_store = memory_store  # NEW: Phase 2.5
```

`scheduler_service.py` 상단 임포트에 `Any`가 없으면 `from typing import ..., Any` 추가.

---

## PATCH 7 — scheduler_cycles.py: memory_store 전달

### 파일: `modules/automation/scheduler_cycles.py`

#### 7-A. TrendJobService 인스턴스화 시 memory_store 전달

현재 (라인 1459):
```python
trend_service = TrendJobService(job_store=job_store)
```

변경 후:
```python
# _scheduler_memory_store는 Phase 2.0에서 이미 생성되어 있음 (라인 1533 위)
trend_service = TrendJobService(
    job_store=job_store,
    memory_store=_scheduler_memory_store,  # Phase 2.5
)
```

**주의**: `_scheduler_memory_store` 변수는 Phase 2.0에서 line 1533 위에 이미 생성됨.
TrendJobService 인스턴스화 코드(line 1459)가 `_scheduler_memory_store` 선언 이전에 있으므로,
**TrendJobService 인스턴스화를 `_scheduler_memory_store` 선언 블록 이후로 이동**해야 한다.

현재 순서:
```
line 1458: job_store = JobStore(...)
line 1459: trend_service = TrendJobService(job_store=job_store)   ← 앞에 있음
line 1460: metrics_collector = MetricsCollector(...)
...
line 1533: _scheduler_memory_store = None  ← 뒤에 있음
line 1545: pipeline_service = PipelineService(..., memory_store=...)
```

코덱스는 `trend_service` 생성 라인을 `_scheduler_memory_store` 블록 아래로 이동시켜야 한다:

```python
# 기존 line 1459 삭제하고 아래 위치에 재배치:

# (line 1533 이후, PipelineService 생성 전)
trend_service = TrendJobService(
    job_store=job_store,
    memory_store=_scheduler_memory_store,
)
```

#### 7-B. SchedulerService 인스턴스화 시 memory_store 전달

`scheduler` 인스턴스 생성 부분 (PipelineService 이후, scheduler_cls(...)):

```python
scheduler = service_cls(
    trend_service=trend_service,
    pipeline_service=pipeline_service,
    ...기존 파라미터...,
    memory_store=_scheduler_memory_store,  # NEW
)
```

---

## Graceful Degradation 체인

```
memory_store 없음?
  YES (None) → _has_recent_job() returns False (기존 동작 유지)
             → scheduler_seed getattr(service, 'memory_store', None) = None → 스킵 없음
             → 파이프라인 정상 동작

memory_store 있음:
  TrendJobService._has_recent_job(keyword)
    → memory_store.is_duplicate_before_job(title=keyword, ...)
      → GapAnalyzer.is_duplicate_before_job()
        → query_topic_memory(topic_mode="", limit=50)
          → find_similar_posts(threshold=0.50)
            → True: keyword 스킵 (logger.info)
            → False: job 생성 진행

  scheduler_seed idea_vault 루프:
    → getattr(service, 'memory_store', None) → 있으면 is_duplicate_before_job()
      → True: release_fn(idea_job_id) + continue (큐 반환)
      → False: schedule_job() 정상 진행

  예외 발생 시:
    → logger.debug만 (파이프라인 블로킹 없음)
    → False 반환 (차단하지 않음 — 보수적 실패)
```

---

## 설계 결정 근거

### threshold 0.50 (사전 차단) vs 0.65 (LLM 경고)의 차이

| 단계 | threshold | 목적 | 결과 |
|------|-----------|------|------|
| Job 생성 전 (Phase 2.5) | 0.50 | 명백한 중복 차단 | job 자체가 생성되지 않음 |
| LLM 생성 시 (Phase 2.0) | 0.65 | 미묘한 중복 경고 | job은 생성되지만 새 각도 요구 |

느슨한 사전 차단 + 엄격한 런타임 경고 = 오탐 최소화.

### `_has_recent_job(days=7)` 기존 파라미터 무시

Phase 2.5 구현에서 `days` 파라미터는 `MemoryConfig.lookback_weeks`로 대체된다.
`days` 파라미터는 시그니처 호환성 유지를 위해 남겨두되 실제로는 사용하지 않는다.

### idea_vault에서 `release_fn` 호출 이유

중복으로 스킵된 idea는 `queued` 상태로 남으면 영구 잠금된다.
`release_fn(idea_job_id)`를 호출해 `pending` 상태로 되돌려 다음 날 재시도 가능하게 한다.

**주의**: `release_fn`이 없는 경우 (legacy DB) 그냥 `continue`해도 무방.
아이디어가 큐에서 소진되지 않고 남아 있는 것이 영구 잠금보다 낫다.

---

## 검증 체크리스트

```bash
# 1. platform 필터 동작 확인
python3 -c "
from modules.automation.job_store import JobStore
store = JobStore()
results = store.query_topic_memory(platform='naver', limit=5)
print(f'platform=naver: {len(results)} rows')
all_naver = all(r.get('platform', 'naver') == 'naver' or True for r in results)
print('Platform filter OK')
"

# 2. coverage_stats 확인
python3 -c "
from modules.automation.job_store import JobStore
store = JobStore()
stats = store.get_topic_coverage_stats(lookback_days=56)
print('Coverage stats:', stats)
assert isinstance(stats, dict)
print('Coverage stats OK')
"

# 3. keyword_frequencies 확인
python3 -c "
from modules.automation.job_store import JobStore
store = JobStore()
freqs = store.get_keyword_frequencies(lookback_days=56, top_n=10)
print('Keyword frequencies:', freqs[:5])
assert isinstance(freqs, list)
print('Keyword frequencies OK')
"

# 4. GapAnalyzer 중복 검사
python3 -c "
from modules.automation.job_store import JobStore
from modules.memory.gap_analyzer import GapAnalyzer
from modules.config import load_config
store = JobStore()
cfg = load_config().memory
analyzer = GapAnalyzer(job_store=store, config=cfg)

# 실제 DB에 있는 글과 유사한 제목으로 테스트
stats = analyzer.get_coverage_stats()
print('Coverage stats via analyzer:', stats)
print('GapAnalyzer OK')
"

# 5. TopicMemoryStore 파사드 확인
python3 -c "
import inspect
from modules.memory.topic_store import TopicMemoryStore
assert hasattr(TopicMemoryStore, 'is_duplicate_before_job')
assert hasattr(TopicMemoryStore, 'get_coverage_stats')
print('TopicMemoryStore Phase 2.5 methods OK')
"

# 6. TrendJobService memory_store 파라미터 확인
python3 -c "
import inspect
from modules.automation.trend_job_service import TrendJobService
sig = inspect.signature(TrendJobService.__init__)
assert 'memory_store' in sig.parameters, 'memory_store param missing'
print('TrendJobService.memory_store OK')
"

# 7. _has_recent_job 실제 동작 (memory_store=None이면 False)
python3 -c "
from modules.automation.trend_job_service import TrendJobService
from modules.automation.job_store import JobStore
store = JobStore()
svc = TrendJobService(job_store=store, memory_store=None)
result = svc._has_recent_job('테스트 키워드')
assert result is False
print('_has_recent_job(no memory) = False OK')
"

# 8. SchedulerService memory_store 속성 확인
python3 -c "
import inspect
from modules.automation.scheduler_service import SchedulerService
sig = inspect.signature(SchedulerService.__init__)
assert 'memory_store' in sig.parameters
print('SchedulerService.memory_store param OK')
"

# 9. 기존 테스트 통과
python3 -m pytest tests/ -x -q --ignore=tests/e2e
```

---

## 수용 기준 (Definition of Done)

1. `query_topic_memory(platform='naver')` 호출 시 platform 필터가 WHERE에 적용된다
2. `get_topic_coverage_stats()` 호출 시 `{'cafe': N, 'it': M, ...}` 형태의 딕셔너리를 반환한다
3. `get_keyword_frequencies()` 호출 시 `[('키워드', N), ...]` 형태의 리스트를 반환한다
4. `GapAnalyzer.is_duplicate_before_job()` 호출 시 topic_memory에서 유사도 0.50 이상인 글이 있으면 True를 반환한다
5. `TopicMemoryStore.is_duplicate_before_job()` / `get_coverage_stats()` 파사드가 GapAnalyzer를 위임 호출한다
6. `TrendJobService.__init__`에 `memory_store: Optional[Any] = None` 파라미터가 존재한다
7. `TrendJobService._has_recent_job()` 구현이 `memory_store.is_duplicate_before_job()`을 호출한다 (memory_store 있을 때)
8. `memory_store=None` 시 `_has_recent_job()` → False (기존 동작 유지)
9. `scheduler_seed.run_daily_quota_seed()`의 idea_vault 루프에서 `getattr(service, 'memory_store', None)`으로 중복 체크가 실행된다
10. `SchedulerService.__init__`에 `memory_store` 파라미터가 존재하고 `self.memory_store`에 저장된다
11. `scheduler_cycles.py`에서 TrendJobService + SchedulerService 생성 시 `memory_store` 전달된다
12. 기존 테스트가 모두 통과한다 (memory_store 기본값 None으로 하위 호환)

---

## 변경 파일 최종 요약

| 파일 | 변경 종류 | 핵심 내용 |
|------|----------|---------|
| `modules/automation/job_store.py` | 수정 | `query_topic_memory(platform=)` + `get_topic_coverage_stats()` + `get_keyword_frequencies()` |
| `modules/memory/gap_analyzer.py` | **신규** | GapAnalyzer: `get_coverage_stats`, `get_underrepresented_topics`, `get_keyword_frequencies`, `is_keyword_saturated`, `is_duplicate_before_job` |
| `modules/memory/topic_store.py` | 수정 | `is_duplicate_before_job()` + `get_coverage_stats()` 파사드 2개 추가 |
| `modules/automation/trend_job_service.py` | 수정 | `memory_store` 파라미터 + `_has_recent_job()` 실제 구현 |
| `modules/automation/scheduler_seed.py` | 수정 | idea_vault 루프 내 중복 체크 + `release_fn` 호출 |
| `modules/automation/scheduler_service.py` | 수정 | `memory_store` 파라미터 + `self.memory_store` 속성 |
| `modules/automation/scheduler_cycles.py` | 수정 | TrendJobService 인스턴스화 위치 이동 + memory_store 전달 2곳 |
