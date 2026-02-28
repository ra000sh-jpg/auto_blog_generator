# CODEX BLUEPRINT — Phase 2.0: SQLite-Native 장기 기억 시스템

> **목표**: MCP Memory(Knowledge Graph)보다 나은 콘텐츠 플래닝 기억 시스템을 외부 의존성 없이 구현한다.
>
> **MCP 대비 우위 3가지:**
> 1. **Cold Start 없음** — 이미 존재하는 200+ jobs 데이터를 초기화 즉시 활용
> 2. **도메인 최적화** — 블로그 콘텐츠 중복 방지 + 내부 링크 생성에 특화된 구조
> 3. **Zero 외부 의존** — 로컬 SQLite, 네트워크 없음, 장애 없음
>
> **달성 효과 3가지:**
> 1. **중복 방지** — 최근 N주 같은 카테고리 주제를 생성 전에 LLM에 인식시킴
> 2. **내부 링크 자동화** — 유사 과거 글 URL을 프롬프트에 주입하여 본문에 링크 유도
> 3. **일관된 전문성** — 같은 카테고리 내 반복 다루면서 심화되는 글쓰기 패턴 구현

---

## 변경 파일 목록

| 파일 | 종류 | 핵심 역할 |
|------|------|---------|
| `modules/automation/job_store.py` | **수정** | `topic_memory` 테이블 추가 + 쿼리 메서드 |
| `modules/memory/__init__.py` | **신규** | 패키지 init |
| `modules/memory/topic_store.py` | **신규** | 저장·조회·백필 핵심 로직 |
| `modules/memory/similarity.py` | **신규** | 키워드 Jaccard + 제목 토큰 유사도 |
| `modules/memory/context_builder.py` | **신규** | 프롬프트 주입 텍스트 조립 |
| `modules/llm/content_generator.py` | **수정** | pre-generate 메모리 쿼리 + 주입 |
| `modules/automation/pipeline_service.py` | **수정** | post-publish 메모리 저장 |
| `modules/llm/__init__.py` | **수정** | `_build_generator`에 memory_store 주입 |
| `modules/config.py` | **수정** | `MemoryConfig` 데이터클래스 추가 |
| `config/default.yaml` | **수정** | `memory:` 섹션 추가 |

**변경하지 않는 파일:**
- `modules/rag/search_engine.py` — 기존 RSS RAG 유지
- `modules/web_search/` — Phase 1.5 웹 검색 유지
- 프론트엔드 — 변경 없음
- `requirements.txt` — 신규 의존성 없음

---

## PATCH 1 — MemoryConfig 추가

### 파일: `modules/config.py`

#### 1-A. `MemoryConfig` 데이터클래스 추가 (WebSearchConfig 뒤, AppConfig 앞)

```python
@dataclass
class MemoryConfig:
    """장기 기억 시스템 설정."""
    enabled: bool = True
    lookback_weeks: int = 8          # 최근 N주 이력 참조
    max_recent_posts: int = 5        # 프롬프트에 넣을 최근글 수
    max_similar_posts: int = 3       # 유사글 최대 수 (내부 링크 후보)
    duplicate_threshold: float = 0.65  # 이 이상이면 중복 경고 주입
    backfill_on_init: bool = True    # 첫 실행 시 기존 jobs에서 백필
    min_quality_score: int = 0       # 이 점수 이상 글만 메모리에 등록 (0=전체)
```

#### 1-B. `AppConfig`에 필드 추가

현재 마지막 필드 `web_search` 뒤에 추가:

```python
    web_search: WebSearchConfig = None  # type: ignore[assignment]
    memory: MemoryConfig = None  # type: ignore[assignment]  # NEW

    def __post_init__(self):
        if self.seo is None:
            self.seo = SEOConfig()
        if self.web_search is None:
            self.web_search = WebSearchConfig()
        if self.memory is None:  # NEW
            self.memory = MemoryConfig()
```

#### 1-C. `load_config()` return문에 추가

```python
    return AppConfig(
        ...
        web_search=WebSearchConfig(**merged.get("web_search", {})),
        memory=MemoryConfig(**merged.get("memory", {})),  # NEW
    )
```

#### 1-D. `_apply_env_overrides()` env_map에 추가 (web_search 항목 뒤)

```python
        # 장기 기억
        "MEMORY_ENABLED": ("memory", "enabled", _parse_bool),
        "MEMORY_LOOKBACK_WEEKS": ("memory", "lookback_weeks", int),
        "MEMORY_MAX_RECENT_POSTS": ("memory", "max_recent_posts", int),
        "MEMORY_MAX_SIMILAR_POSTS": ("memory", "max_similar_posts", int),
        "MEMORY_DUPLICATE_THRESHOLD": ("memory", "duplicate_threshold", float),
        "MEMORY_BACKFILL_ON_INIT": ("memory", "backfill_on_init", _parse_bool),
        "MEMORY_MIN_QUALITY_SCORE": ("memory", "min_quality_score", int),
```

---

## PATCH 2 — default.yaml 설정 추가

### 파일: `config/default.yaml`

`web_search:` 섹션 뒤에 추가:

```yaml

memory:
  enabled: true
  lookback_weeks: 8
  max_recent_posts: 5
  max_similar_posts: 3
  duplicate_threshold: 0.65
  backfill_on_init: true
  min_quality_score: 0
```

---

## PATCH 3 — topic_memory 테이블 추가

### 파일: `modules/automation/job_store.py`

#### 3-A. `_init_tables()` 메서드에 테이블 추가 (line 204, 기존 executescript 블록 안)

기존 `CREATE TABLE IF NOT EXISTS jobs (...);` 뒤, 또는 별도 `conn.executescript()`로 추가:

```sql
CREATE TABLE IF NOT EXISTS topic_memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    keywords      TEXT NOT NULL DEFAULT '[]',  -- JSON array
    topic_mode    TEXT NOT NULL DEFAULT 'cafe',
    platform      TEXT NOT NULL DEFAULT 'naver',
    persona_id    TEXT NOT NULL DEFAULT 'P1',
    summary       TEXT DEFAULT '',             -- 제목+키워드 기반 160자 요약 (LLM 호출 없음)
    result_url    TEXT DEFAULT '',
    quality_score INTEGER DEFAULT 0,
    recorded_at   TEXT NOT NULL               -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_tm_topic_recorded
    ON topic_memory(topic_mode, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_tm_persona_recorded
    ON topic_memory(persona_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_tm_recorded
    ON topic_memory(recorded_at DESC);
```

#### 3-B. `_init_tables()` 안에 마이그레이션 가드 추가 (기존 열 마이그레이션 패턴과 동일)

```python
# topic_memory 테이블 존재 여부 체크 (기존 DB 호환)
tables = {row[0] for row in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()}
if "topic_memory" not in tables:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS topic_memory (
            ...위 스키마 동일...
        );
        CREATE INDEX IF NOT EXISTS idx_tm_topic_recorded
            ON topic_memory(topic_mode, recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tm_persona_recorded
            ON topic_memory(persona_id, recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tm_recorded
            ON topic_memory(recorded_at DESC);
    """)
```

#### 3-C. `job_store.py`에 메서드 3개 추가 (클래스 끝)

```python
# ────────────────────────────────────────────
# topic_memory CRUD
# ────────────────────────────────────────────

def insert_topic_memory(
    self,
    job_id: str,
    title: str,
    keywords: List[str],
    topic_mode: str,
    platform: str,
    persona_id: str,
    summary: str,
    result_url: str,
    quality_score: int,
) -> None:
    """발행 완료 후 topic_memory에 기록한다."""
    import json as _json
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    with self.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO topic_memory
                (job_id, title, keywords, topic_mode, platform, persona_id,
                 summary, result_url, quality_score, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(job_id),
                str(title),
                _json.dumps(list(keywords), ensure_ascii=False),
                str(topic_mode),
                str(platform),
                str(persona_id),
                str(summary)[:400],
                str(result_url),
                int(quality_score),
                now_iso,
            ),
        )


def query_topic_memory(
    self,
    topic_mode: str = "",
    persona_id: str = "",
    lookback_days: int = 56,
    limit: int = 30,
    min_quality_score: int = 0,
) -> List[Dict[str, Any]]:
    """최근 발행 이력을 조회한다. topic_mode/persona_id 필터 가능."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()

    params: list = [cutoff]
    where_clauses = ["recorded_at >= ?"]
    if topic_mode:
        where_clauses.append("topic_mode = ?")
        params.append(str(topic_mode))
    if persona_id:
        where_clauses.append("persona_id = ?")
        params.append(str(persona_id))
    if min_quality_score > 0:
        where_clauses.append("quality_score >= ?")
        params.append(int(min_quality_score))
    params.append(max(1, min(int(limit), 200)))

    sql = f"""
        SELECT job_id, title, keywords, topic_mode, persona_id,
               summary, result_url, quality_score, recorded_at
        FROM topic_memory
        WHERE {' AND '.join(where_clauses)}
        ORDER BY recorded_at DESC
        LIMIT ?
    """
    with self.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = []
    import json as _json
    for row in rows:
        try:
            kw = _json.loads(row[2]) if row[2] else []
        except Exception:
            kw = []
        result.append({
            "job_id": row[0],
            "title": row[1],
            "keywords": kw,
            "topic_mode": row[3],
            "persona_id": row[4],
            "summary": row[5],
            "result_url": row[6],
            "quality_score": row[7],
            "recorded_at": row[8],
        })
    return result


def backfill_topic_memory_from_jobs(self, limit: int = 300) -> int:
    """
    기존 completed jobs 테이블에서 topic_memory를 백필한다.
    초기 실행 시 1회 호출. 이미 등록된 job_id는 INSERT OR IGNORE로 스킵.
    반환값: 새로 삽입된 행 수
    """
    import json as _json
    completed_jobs = self.list_recent_completed_jobs(limit=limit)
    inserted = 0
    for job in completed_jobs:
        if not job.result_url:
            continue
        # seo_snapshot에서 topic_mode 추출
        seo_snap = {}
        try:
            seo_raw = getattr(job, "seo_snapshot", None)
            if isinstance(seo_raw, str):
                seo_snap = _json.loads(seo_raw)
            elif isinstance(seo_raw, dict):
                seo_snap = seo_raw
        except Exception:
            pass
        topic_mode = str(seo_snap.get("topic_mode", "cafe")).strip() or "cafe"

        # quality_snapshot에서 점수 추출
        q_snap = {}
        try:
            q_raw = getattr(job, "quality_snapshot", None)
            if isinstance(q_raw, str):
                q_snap = _json.loads(q_raw)
            elif isinstance(q_raw, dict):
                q_snap = q_raw
        except Exception:
            pass
        quality_score = int(q_snap.get("score", 0))

        # 요약: 제목 + 키워드 결합 (LLM 호출 없음)
        kw_list = list(job.seed_keywords) if job.seed_keywords else []
        summary = f"{job.title} / 키워드: {', '.join(kw_list[:5])}"

        with self.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO topic_memory
                        (job_id, title, keywords, topic_mode, platform, persona_id,
                         summary, result_url, quality_score, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(job.job_id),
                        str(job.title),
                        _json.dumps(kw_list, ensure_ascii=False),
                        topic_mode,
                        str(job.platform),
                        str(job.persona_id or "P1"),
                        summary[:400],
                        str(job.result_url),
                        quality_score,
                        str(getattr(job, "completed_at", "") or ""),
                    ),
                )
                inserted += 1
            except Exception:
                pass
    return inserted
```

**중요**: `query_topic_memory`의 반환 타입 힌트를 위해 `job_store.py` 상단 import에 `Dict, Any`가 있는지 확인. 기존에 이미 있으므로 추가 불필요.

---

## PATCH 4 — memory 패키지 신규 생성 (4개 파일)

### 4-A. `modules/memory/__init__.py`

```python
"""블로그 발행 이력 기반 장기 기억 모듈."""
```

### 4-B. `modules/memory/similarity.py`

MCP Knowledge Graph의 관계 추론 대신 **도메인 최적화된 유사도** 사용.
외부 ML 라이브러리 없음. sentence-transformers 불필요.

```python
"""키워드·제목 기반 경량 유사도 계산."""

from __future__ import annotations

import re
from typing import List


def keyword_jaccard(keywords_a: List[str], keywords_b: List[str]) -> float:
    """키워드 집합 Jaccard 유사도 (0.0 ~ 1.0).

    두 글의 핵심 키워드 집합이 얼마나 겹치는지 측정.
    - 0.6 이상: 같은 주제 다른 각도
    - 0.8 이상: 사실상 중복
    """
    set_a = {k.lower().strip() for k in keywords_a if k.strip()}
    set_b = {k.lower().strip() for k in keywords_b if k.strip()}
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def title_token_overlap(title_a: str, title_b: str) -> float:
    """제목 토큰 겹침 비율 (0.0 ~ 1.0).

    한글 2자 이상 / 영문 3자 이상 토큰을 추출하여 비교.
    """
    def _tokenize(text: str) -> set:
        lowered = text.lower()
        ko_tokens = set(re.findall(r"[가-힣]{2,}", lowered))
        en_tokens = set(re.findall(r"[a-z]{3,}", lowered))
        return ko_tokens | en_tokens

    tokens_a = _tokenize(title_a)
    tokens_b = _tokenize(title_b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))


def combined_similarity(
    title: str,
    keywords: List[str],
    other_title: str,
    other_keywords: List[str],
    kw_weight: float = 0.65,
    title_weight: float = 0.35,
) -> float:
    """키워드(65%) + 제목(35%) 복합 유사도.

    Args:
        kw_weight: 키워드 Jaccard 가중치 (기본 0.65)
        title_weight: 제목 토큰 겹침 가중치 (기본 0.35)

    Returns:
        0.0 ~ 1.0 유사도 점수
    """
    kw_sim = keyword_jaccard(keywords, other_keywords)
    title_sim = title_token_overlap(title, other_title)
    return kw_weight * kw_sim + title_weight * title_sim


def find_similar_posts(
    title: str,
    keywords: List[str],
    candidates: List[dict],
    threshold: float = 0.3,
    top_k: int = 5,
) -> List[dict]:
    """후보 목록에서 유사한 과거 글을 찾는다.

    Args:
        candidates: query_topic_memory() 반환값
        threshold: 이 값 이상인 결과만 포함
        top_k: 반환할 최대 수

    Returns:
        유사도 내림차순 정렬된 과거 글 목록 (각 항목에 'similarity' 키 추가)
    """
    scored: list = []
    for post in candidates:
        sim = combined_similarity(
            title=title,
            keywords=keywords,
            other_title=str(post.get("title", "")),
            other_keywords=list(post.get("keywords", [])),
        )
        if sim >= threshold:
            scored.append({**post, "similarity": round(sim, 3)})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]
```

### 4-C. `modules/memory/context_builder.py`

MCP Memory가 줄 수 있는 텍스트 컨텍스트보다 **구조화된, 블로그 작성에 특화된** 주입 텍스트 생성.

```python
"""발행 이력을 LLM 프롬프트 텍스트로 변환한다."""

from __future__ import annotations

from typing import List


_MEMORY_HEADER = "[발행 이력 메모리 — 글쓰기 전략에 반영]"

_RULES = """\
작성 규칙:
1) 위 주제와 키워드가 65% 이상 겹치면 반드시 새로운 각도·심화 내용으로 접근할 것
2) '관련 과거 글' 항목은 본문에 자연스럽게 언급 가능 ("이전 포스팅에서 다룬 것처럼...", "[관련글]" 형식)
3) 동일 키워드 반복 시 독자에게 새로운 가치를 1개 이상 제공할 것
4) 결과 URL이 있는 과거 글은 본문 말미 '참고' 섹션에 링크로 포함 가능"""

_DUPLICATE_WARNING = """\
⚠️ 주의: 이 주제는 최근 발행글과 유사도가 높습니다 ({sim:.0%}).
반드시 차별화된 관점, 최신 정보, 또는 더 깊은 심화 내용을 포함하세요."""


def build_memory_context_text(
    recent_posts: List[dict],
    similar_posts: List[dict],
    max_recent: int = 5,
    max_similar: int = 3,
    duplicate_threshold: float = 0.65,
) -> str:
    """메모리 컨텍스트 주입 텍스트를 생성한다.

    recent_posts: query_topic_memory() 반환값 (같은 토픽 최근 글)
    similar_posts: find_similar_posts() 반환값 (유사 키워드 글, similarity 포함)

    반환값 예시:
        [발행 이력 메모리 — 글쓰기 전략에 반영]
        ▶ 최근 같은 카테고리에서 다룬 주제 (중복 주의):
        1. 드립커피 완벽 가이드 [품질:85, 2026-02-10] https://blog.naver.com/...
        2. 핸드드립 입문자 도구 추천 [품질:78, 2026-02-03]

        ▶ 키워드 유사 과거 글 (내부 링크 후보, 유사도순):
        1. 원두 로스팅 입문 [유사:72%] https://blog.naver.com/yyy

        작성 규칙:
        ...
    """
    if not recent_posts and not similar_posts:
        return ""

    lines: List[str] = [_MEMORY_HEADER, ""]

    # 섹션 1: 최근 같은 카테고리 글
    if recent_posts:
        lines.append("▶ 최근 같은 카테고리에서 다룬 주제 (중복 주의):")
        for i, post in enumerate(recent_posts[:max_recent], start=1):
            date_str = str(post.get("recorded_at", ""))[:10]
            score = int(post.get("quality_score", 0))
            url = str(post.get("result_url", "")).strip()
            title = str(post.get("title", "")).strip()
            score_tag = f", 품질:{score}" if score > 0 else ""
            url_tag = f" {url}" if url else ""
            lines.append(f"{i}. {title} [{date_str}{score_tag}]{url_tag}")
        lines.append("")

    # 섹션 2: 유사 키워드 과거 글 (내부 링크 후보)
    high_sim_posts = [p for p in similar_posts if p.get("similarity", 0) >= duplicate_threshold]
    link_posts = [p for p in similar_posts if p.get("similarity", 0) < duplicate_threshold]

    if high_sim_posts:
        top = high_sim_posts[0]
        lines.append(
            _DUPLICATE_WARNING.format(sim=top.get("similarity", 0))
        )
        lines.append("")

    if link_posts:
        lines.append("▶ 키워드 유사 과거 글 (내부 링크 후보, 유사도순):")
        for i, post in enumerate(link_posts[:max_similar], start=1):
            sim_pct = int(post.get("similarity", 0) * 100)
            url = str(post.get("result_url", "")).strip()
            title = str(post.get("title", "")).strip()
            url_tag = f" {url}" if url else ""
            lines.append(f"{i}. {title} [유사:{sim_pct}%]{url_tag}")
        lines.append("")

    lines.append(_RULES)
    return "\n".join(lines)


def is_duplicate_topic(similar_posts: List[dict], threshold: float = 0.65) -> bool:
    """유사 포스트 중 threshold 이상이 있으면 중복으로 판단."""
    if not similar_posts:
        return False
    return any(p.get("similarity", 0) >= threshold for p in similar_posts)
```

### 4-D. `modules/memory/topic_store.py`

`job_store.py`의 쿼리 메서드를 래핑하는 얇은 파사드 레이어.
`job_store.py`를 직접 수정하지 않고 메모리 로직을 캡슐화.

```python
"""topic_memory 테이블 파사드 — 저장·조회·백필 로직 캡슐화."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TopicMemoryStore:
    """발행 이력 기반 장기 기억 저장소.

    내부적으로 job_store의 topic_memory 테이블을 사용한다.
    모든 메서드는 예외를 조용히 처리 — 파이프라인 블로킹 없음.
    """

    def __init__(
        self,
        job_store: Any,  # JobStore — 순환 임포트 방지를 위해 Any 타입
        config: Any,     # MemoryConfig
    ):
        self._store = job_store
        self._config = config
        self._backfilled = False

    def ensure_backfilled(self) -> None:
        """최초 1회 기존 jobs 데이터를 백필한다."""
        if self._backfilled or not self._config.backfill_on_init:
            return
        try:
            fn = getattr(self._store, "backfill_topic_memory_from_jobs", None)
            if callable(fn):
                count = fn(limit=300)
                if count:
                    logger.info("topic_memory backfilled: %d posts", count)
        except Exception as exc:
            logger.debug("Backfill skipped: %s", exc)
        self._backfilled = True

    def record_post(
        self,
        job_id: str,
        title: str,
        keywords: List[str],
        topic_mode: str,
        platform: str,
        persona_id: str,
        result_url: str,
        quality_score: int,
    ) -> None:
        """발행 완료 시 메모리에 저장한다.

        summary는 LLM 호출 없이 제목+키워드 결합으로 생성 (비용 0).
        """
        if not self._config.enabled:
            return
        if quality_score < self._config.min_quality_score:
            logger.debug(
                "Memory record skipped: quality %d < threshold %d",
                quality_score,
                self._config.min_quality_score,
            )
            return

        # 요약: LLM 호출 없이 제목 + 키워드로 구성 (빠르고 비용 없음)
        kw_str = ", ".join(str(k) for k in keywords[:6])
        summary = f"{title} / 키워드: {kw_str}"

        try:
            fn = getattr(self._store, "insert_topic_memory", None)
            if callable(fn):
                fn(
                    job_id=job_id,
                    title=title,
                    keywords=keywords,
                    topic_mode=topic_mode,
                    platform=platform,
                    persona_id=persona_id,
                    summary=summary,
                    result_url=result_url,
                    quality_score=quality_score,
                )
        except Exception as exc:
            logger.debug("topic_memory insert failed (non-critical): %s", exc)

    def get_recent_by_topic(
        self,
        topic_mode: str,
        persona_id: str = "",
        lookback_weeks: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """같은 토픽의 최근 발행글을 반환한다."""
        if not self._config.enabled:
            return []
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        n = limit if limit is not None else self._config.max_recent_posts
        try:
            fn = getattr(self._store, "query_topic_memory", None)
            if callable(fn):
                return fn(
                    topic_mode=topic_mode,
                    persona_id=persona_id,
                    lookback_days=weeks * 7,
                    limit=n + 10,  # 유사도 필터링 여유분
                    min_quality_score=self._config.min_quality_score,
                )
        except Exception as exc:
            logger.debug("topic_memory query failed (non-critical): %s", exc)
        return []

    def get_cross_topic_recent(
        self,
        lookback_weeks: Optional[int] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """모든 토픽의 최근 발행글 (내부 링크 후보 확장용)."""
        if not self._config.enabled:
            return []
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        try:
            fn = getattr(self._store, "query_topic_memory", None)
            if callable(fn):
                return fn(
                    lookback_days=weeks * 7,
                    limit=limit,
                    min_quality_score=self._config.min_quality_score,
                )
        except Exception as exc:
            logger.debug("topic_memory cross-topic query failed: %s", exc)
        return []
```

---

## PATCH 5 — ContentGenerator에 메모리 통합

### 파일: `modules/llm/content_generator.py`

#### 5-A. `__init__` 시그니처에 파라미터 추가 (현재 `web_fetch_client` 뒤)

```python
        web_search_max_results: int = 5,
        memory_store: Optional[Any] = None,  # NEW: TopicMemoryStore
    ):
```

#### 5-B. `__init__` 본문에 저장 (`self.web_search_max_results` 줄 뒤)

```python
        self.web_search_max_results = max(1, int(web_search_max_results))
        self.memory_store = memory_store  # NEW
```

#### 5-C. `generate()` 안에 메모리 컨텍스트 수집 추가 (line ~243, news_context 수집 블록 바로 뒤)

현재 코드 흐름:
```python
        # ... (news_context 수집 완료)

        # 폴백 체인 구성
        fallback_chain = self._build_fallback_chain()
```

이 사이에 삽입:

```python
        # ── 메모리 컨텍스트 수집 (발행 이력 기반) ──
        memory_context_text = self._collect_memory_context(
            job=job,
            topic_mode=topic_mode,
        )
```

#### 5-D. `_generate_draft()` 호출 시 `memory_context_text` 전달

`_generate_draft()`의 파라미터에 `memory_context_text` 추가. 호출부(multistep / single 모두)에서 전달.

`_generate_draft()` 시그니처 변경:
```python
    async def _generate_draft(
        self,
        job: Job,
        client: BaseLLMClient,
        persona: Any,
        tone_profile: Any,
        topic_mode: str,
        news_context: Optional[List[Dict[str, str]]] = None,
        quality_only: bool = False,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
        pre_analysis: Optional[Dict[str, Any]] = None,
        memory_context_text: str = "",  # NEW
    ) -> Tuple[str, str]:
```

`_generate_draft()` 안 system_prompt 조립 부분에 memory 주입:

현재 (line ~1244 부근):
```python
        system_prompt = (
            QUALITY_LAYER_SYSTEM_PROMPT
            + cognitive_injection
            + emotional_injection
            + pre_analysis_injection
        )
```

변경:
```python
        memory_injection = (
            f"\n\n{memory_context_text}" if memory_context_text else ""
        )
        system_prompt = (
            QUALITY_LAYER_SYSTEM_PROMPT
            + cognitive_injection
            + emotional_injection
            + pre_analysis_injection
            + memory_injection  # NEW — 발행 이력 주입
        )
```

#### 5-E. `_collect_memory_context()` 신규 메서드 추가

`_collect_web_context()` 뒤에 삽입:

```python
    def _collect_memory_context(
        self,
        job: "Job",
        topic_mode: str,
    ) -> str:
        """발행 이력 기반 메모리 컨텍스트 텍스트를 생성한다.

        TopicMemoryStore가 None이거나 실패해도 빈 문자열을 반환 (non-critical).
        """
        from ..memory.similarity import find_similar_posts
        from ..memory.context_builder import build_memory_context_text

        if self.memory_store is None:
            return ""

        try:
            # 백필 보장 (최초 1회만 실행)
            ensure_fn = getattr(self.memory_store, "ensure_backfilled", None)
            if callable(ensure_fn):
                ensure_fn()

            # 같은 토픽 최근 글
            recent = self.memory_store.get_recent_by_topic(
                topic_mode=topic_mode,
                persona_id=str(job.persona_id or "P1"),
            )

            # 유사 키워드 글 (전 토픽 대상)
            cross_recent = self.memory_store.get_cross_topic_recent(limit=50)
            similar = find_similar_posts(
                title=str(job.title),
                keywords=list(job.seed_keywords),
                candidates=cross_recent,
                threshold=0.25,
                top_k=5,
            )

            text = build_memory_context_text(
                recent_posts=recent,
                similar_posts=similar,
            )
            if text:
                logger.info(
                    "Memory context injected",
                    extra={
                        "topic_mode": topic_mode,
                        "recent_count": len(recent),
                        "similar_count": len(similar),
                    },
                )
            return text

        except Exception as exc:
            logger.debug("Memory context collection failed (non-critical): %s", exc)
            return ""
```

---

## PATCH 6 — Pipeline post-publish 메모리 저장

### 파일: `modules/automation/pipeline_service.py`

#### 6-A. `PipelineService.__init__` 파라미터에 `memory_store` 추가

현재 `__init__` 시그니처 끝에 추가:
```python
        memory_store: Optional[Any] = None,  # NEW: TopicMemoryStore
```

`__init__` 본문 끝에 추가:
```python
        self.memory_store = memory_store  # NEW
```

#### 6-B. `_publish_payload()` 성공 경로에 메모리 저장 추가 (line 692 뒤)

현재 (line 692-693):
```python
            self._record_model_performance(job=job, payload=payload, post_id=result.url)
            mark_consumed = getattr(self.job_store, "mark_idea_vault_consumed_by_job", None)
```

이 사이에 삽입:

```python
            # ── 메모리 저장 (발행 성공 시, non-critical) ──
            self._record_topic_memory(job=job, payload=payload, result_url=result.url)
```

#### 6-C. `_record_topic_memory()` 신규 메서드 추가 (클래스 끝 or `_record_model_performance` 뒤)

```python
    def _record_topic_memory(
        self,
        job: "Job",
        payload: Dict[str, Any],
        result_url: str,
    ) -> None:
        """발행 완료 후 topic_memory에 이력을 기록한다.

        실패해도 예외를 전파하지 않는다 (non-critical).
        """
        if self.memory_store is None:
            return

        try:
            seo_snap = payload.get("seo_snapshot") or {}
            if isinstance(seo_snap, str):
                import json as _json
                try:
                    seo_snap = _json.loads(seo_snap)
                except Exception:
                    seo_snap = {}

            quality_snap = payload.get("quality_snapshot") or {}
            if isinstance(quality_snap, str):
                import json as _json
                try:
                    quality_snap = _json.loads(quality_snap)
                except Exception:
                    quality_snap = {}

            topic_mode = str(seo_snap.get("topic_mode", "cafe")).strip() or "cafe"
            quality_score = int(quality_snap.get("score", 0))

            self.memory_store.record_post(
                job_id=str(job.job_id),
                title=str(job.title),
                keywords=list(job.seed_keywords),
                topic_mode=topic_mode,
                platform=str(job.platform),
                persona_id=str(job.persona_id or "P1"),
                result_url=str(result_url),
                quality_score=quality_score,
            )
        except Exception as exc:
            logger.debug(
                "topic_memory record failed (non-critical): %s",
                exc,
                extra={"job_id": job.job_id},
            )
```

---

## PATCH 7 — _build_generator에 memory_store 주입

### 파일: `modules/llm/__init__.py`

#### 7-A. `_build_generator()` 안 web_search 블록 뒤에 추가 (현재 return 직전)

```python
    # ── 메모리 스토어 초기화 ──
    memory_store = None
    try:
        from ..config import load_config as _load_mem_cfg
        mem_config = _load_mem_cfg().memory
        if mem_config.enabled and job_store is not None:
            from ..memory.topic_store import TopicMemoryStore
            memory_store = TopicMemoryStore(
                job_store=job_store,
                config=mem_config,
            )
    except Exception as exc:
        logger.warning("Memory store init failed: %s", exc)
```

#### 7-B. `ContentGenerator(...)` 생성자에 파라미터 추가

```python
    return ContentGenerator(
        ...
        web_search_client=web_search_client,
        web_fetch_client=web_fetch_client,
        web_search_max_results=web_search_max_results,
        memory_store=memory_store,  # NEW
    )
```

---

## PATCH 8 — PipelineService에 memory_store 주입

### 파일: `modules/automation/pipeline_service.py` 생성 경로

`PipelineService`는 `pipeline_service.py` 안에서 직접 인스턴스화된다.
`PipelineService.__init__`에 `memory_store` 파라미터가 추가되었으므로,
실제 호출 지점에서 주입이 필요하다.

호출 지점을 찾아 (대개 `scheduler_workers.py` 또는 `scheduler_cycles.py`):

```python
# 예: scheduler_workers.py 또는 scheduler_cycles.py
from modules.memory.topic_store import TopicMemoryStore
from modules.config import load_config

_mem_config = load_config().memory
_memory_store = None
if _mem_config.enabled:
    _memory_store = TopicMemoryStore(job_store=job_store, config=_mem_config)

pipeline = PipelineService(
    ...기존 파라미터...,
    memory_store=_memory_store,  # NEW
)
```

**주의**: 코덱스는 `PipelineService`가 어디서 인스턴스화되는지 확인한 후, 해당 파일에서 주입 코드를 추가해야 한다.

---

## 설계 결정 근거

### MCP Knowledge Graph 대비 우위 상세

| 항목 | MCP Knowledge Graph | 이 시스템 (SQLite-Native) |
|------|--------------------|-----------------------|
| **콜드 스타트** | 빈 그래프, 0개 데이터 | 즉시 200+ 과거 job 백필 |
| **도메인 특화** | 범용 entity-relation | 블로그 중복/링크/톤 최적화 |
| **쿼리 표현력** | 그래프 traversal | SQL WHERE/GROUP BY/ORDER BY |
| **지연 시간** | 네트워크 RTT 수십 ms | SQLite 로컬 <1ms |
| **신뢰성** | 원격 서버 장애 가능 | 로컬 파일, 장애 없음 |
| **비용** | API 호출 비용 | 0원 |
| **프로토콜 안정성** | MCP v0.x 잦은 변경 | SQLite 30년 안정 |

### 요약 LLM 미사용 (비용 절감)

MCP 시스템에서는 "요약 생성"에 LLM을 쓰는 경우가 많다.
이 시스템은 **제목 + 키워드 결합**으로 요약을 대체한다:
```python
summary = f"{title} / 키워드: {kw_str}"  # LLM 호출 0회
```
하루 5포스트 기준 월 150회 LLM 절감.

### 유사도 엔진이 MCP Knowledge Graph보다 나은 이유

MCP Knowledge Graph는 "두 엔티티가 관련 있다"는 관계를 수동 추가하거나 LLM으로 추론해야 한다.
이 시스템의 Jaccard + 토큰 겹침은:
- **한국어 특화** — 한글 2자 이상 토큰 추출
- **키워드 중심** — 실제 SEO 키워드로 비교 (제목보다 정확)
- **속도** — 100개 후보 비교 < 1ms

---

## Graceful Degradation 체인

```
memory_store 존재?
  NO  → memory_context_text = "" (기존 동작 100% 유지)
  YES → ensure_backfilled() → get_recent + find_similar → 텍스트 생성
           │ 예외 발생?
           └─ "" 반환 (파이프라인 블로킹 없음)

post-publish:
  _record_topic_memory() 예외 → logger.debug만 (실패 전파 없음)
```

---

## 검증 체크리스트

```bash
# 1. 테이블 생성 확인
python3 -c "
from modules.automation.job_store import JobStore
store = JobStore(db_path='data/test_memory.db')
with store.connection() as conn:
    tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]
    assert 'topic_memory' in tables, 'topic_memory table missing'
    print('topic_memory table OK')
"

# 2. similarity 함수 정확도
python3 -c "
from modules.memory.similarity import keyword_jaccard, combined_similarity
assert keyword_jaccard(['드립커피','핸드드립'], ['드립커피','원두']) == 1/3
sim = combined_similarity('드립커피 가이드', ['드립커피'], '드립커피 완벽 정리', ['드립커피','핸드드립'])
assert 0.4 < sim < 0.9, f'unexpected sim: {sim}'
print('Similarity OK')
"

# 3. 백필 동작 (실제 DB)
python3 -c "
from modules.automation.job_store import JobStore
store = JobStore()
count = store.backfill_topic_memory_from_jobs(limit=50)
print(f'Backfill: {count} posts inserted')
"

# 4. ContentGenerator 시그니처
python3 -c "
import inspect
from modules.llm.content_generator import ContentGenerator
sig = inspect.signature(ContentGenerator.__init__)
assert 'memory_store' in sig.parameters
print('ContentGenerator memory_store param OK')
"

# 5. 기존 테스트 통과 (memory disabled 기본값이어도 OK)
python3 -m pytest tests/ -x -q --ignore=tests/e2e

# 6. 컨텍스트 텍스트 생성 검증
python3 -c "
from modules.memory.context_builder import build_memory_context_text
recent = [{'title':'드립커피 가이드', 'recorded_at':'2026-02-10', 'quality_score':80, 'result_url':'https://naver.com/1'}]
similar = [{'title':'핸드드립 입문', 'similarity':0.72, 'result_url':'https://naver.com/2'}]
text = build_memory_context_text(recent, similar)
assert '[발행 이력 메모리' in text
print('Context text OK:')
print(text[:200])
"
```

---

## 수용 기준 (Definition of Done)

1. `topic_memory` 테이블이 생성되고 기존 DB에 마이그레이션된다
2. `backfill_topic_memory_from_jobs()`가 기존 completed jobs를 자동 로드한다
3. `similarity.combined_similarity()`가 Jaccard 65% + 제목 35% 복합 점수를 반환한다
4. `build_memory_context_text()`가 최근글·유사글·규칙을 포함한 텍스트를 생성한다
5. `ContentGenerator.__init__`에 `memory_store` 파라미터가 존재한다
6. `_collect_memory_context()`가 메모리 없을 때 `""` 반환하고 파이프라인이 정상 동작한다
7. `PipelineService._record_topic_memory()`가 발행 성공 시 이력을 저장한다
8. `memory.enabled=true`(기본값)이고 `job_store` 존재 시 자동으로 활성화된다
9. 기존 테스트가 모두 통과한다

---

## 변경 파일 최종 요약

| 파일 | 변경 종류 | 핵심 내용 |
|------|----------|---------|
| `modules/config.py` | 수정 | MemoryConfig + AppConfig 확장 + env override |
| `config/default.yaml` | 수정 | `memory:` 섹션 (enabled=true) |
| `modules/automation/job_store.py` | 수정 | topic_memory 테이블 + insert/query/backfill 3메서드 |
| `modules/memory/__init__.py` | **신규** | 패키지 |
| `modules/memory/similarity.py` | **신규** | Jaccard + 토큰 유사도 + find_similar_posts |
| `modules/memory/context_builder.py` | **신규** | 프롬프트 주입 텍스트 빌더 |
| `modules/memory/topic_store.py` | **신규** | TopicMemoryStore 파사드 |
| `modules/llm/content_generator.py` | 수정 | memory_store 파라미터 + _collect_memory_context() |
| `modules/automation/pipeline_service.py` | 수정 | memory_store 파라미터 + _record_topic_memory() |
| `modules/llm/__init__.py` | 수정 | _build_generator에 memory_store 주입 |
