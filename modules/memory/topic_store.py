"""topic_memory 테이블 파사드 — 저장·조회·백필 로직 캡슐화.

job_store.py의 쿼리 메서드를 래핑하는 얇은 파사드 레이어.
모든 메서드는 예외를 조용히 처리 — 파이프라인 블로킹 없음.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TopicMemoryStore:
    """발행 이력 기반 장기 기억 저장소.

    내부적으로 job_store의 topic_memory 테이블을 사용한다.
    모든 메서드는 예외를 조용히 처리 — 파이프라인 블로킹 없음.

    MCP Knowledge Graph 대비 우위:
    - Cold Start 없음: 기존 completed jobs 데이터를 즉시 백필
    - 도메인 최적화: 블로그 중복 방지 + 내부 링크 생성에 특화
    - Zero 외부 의존: 로컬 SQLite, 네트워크 없음, 장애 없음
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

    def is_duplicate_before_job(
        self,
        title: str,
        keywords: List[str],
        topic_mode: str,
        similarity_threshold: Optional[float] = None,
        platform: str = "",
        lookback_weeks: Optional[int] = None,
    ) -> bool:
        """작업 생성 전 중복 여부를 검사한다."""
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
                lookback_weeks=lookback_weeks,
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
        """topic_mode별 발행 수를 반환한다."""
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
