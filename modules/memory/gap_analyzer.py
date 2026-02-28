"""발행 이력 기반 커버리지/중복 분석 엔진."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class GapAnalyzer:
    """topic_memory 기반 스마트 플래닝 분석기."""

    def __init__(
        self,
        job_store: Any,  # JobStore
        config: Any,  # MemoryConfig
    ):
        self._store = job_store
        self._config = config

    def get_coverage_stats(
        self,
        lookback_weeks: Optional[int] = None,
        platform: str = "",
    ) -> Dict[str, int]:
        """topic_mode별 발행 수를 반환한다."""
        if not self._config.enabled:
            return {}
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        try:
            fn = getattr(self._store, "get_topic_coverage_stats", None)
            if callable(fn):
                return fn(lookback_days=max(1, int(weeks)) * 7, platform=platform)
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
        """목표치보다 발행 수가 부족한 topic_mode를 반환한다."""
        stats = self.get_coverage_stats(lookback_weeks=lookback_weeks, platform=platform)
        pairs = []
        for topic in known_topics:
            count = int(stats.get(topic, 0))
            if count < int(target_per_topic):
                pairs.append((topic, count))
        pairs.sort(key=lambda item: item[1])
        return [topic for topic, _ in pairs]

    def get_keyword_frequencies(
        self,
        topic_mode: str = "",
        lookback_weeks: Optional[int] = None,
        top_n: int = 30,
    ) -> List[Tuple[str, int]]:
        """키워드별 사용 빈도를 반환한다."""
        if not self._config.enabled:
            return []
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        try:
            fn = getattr(self._store, "get_keyword_frequencies", None)
            if callable(fn):
                return fn(
                    topic_mode=topic_mode,
                    lookback_days=max(1, int(weeks)) * 7,
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
        """최근 기간 내 반복 사용된 키워드인지 판단한다."""
        freqs = self.get_keyword_frequencies(
            topic_mode=topic_mode,
            lookback_weeks=lookback_weeks,
        )
        target = str(keyword).strip().lower()
        if not target:
            return False
        for freq_keyword, count in freqs:
            if freq_keyword == target and int(count) >= int(threshold):
                return True
        return False

    def is_duplicate_before_job(
        self,
        title: str,
        keywords: List[str],
        topic_mode: str,
        similarity_threshold: Optional[float] = None,
        lookback_weeks: Optional[int] = None,
        platform: str = "",
    ) -> bool:
        """작업 생성 전 topic_memory 유사도를 검사한다."""
        if not self._config.enabled:
            return False
        weeks = lookback_weeks if lookback_weeks is not None else self._config.lookback_weeks
        threshold = (
            float(similarity_threshold)
            if similarity_threshold is not None
            else float(getattr(self._config, "precheck_duplicate_threshold", 0.50))
        )
        try:
            from .similarity import find_similar_posts

            fn = getattr(self._store, "query_topic_memory", None)
            if not callable(fn):
                return False

            candidates = fn(
                topic_mode=topic_mode,
                lookback_days=max(1, int(weeks)) * 7,
                limit=50,
                platform=platform,
            )
            if not candidates:
                return False

            similar = find_similar_posts(
                title=title,
                keywords=keywords,
                candidates=candidates,
                threshold=threshold,
                top_k=1,
            )
            if similar:
                top = similar[0]
                logger.info(
                    "Pre-job duplicate detected",
                    extra={
                        "title": str(title)[:60],
                        "topic_mode": topic_mode,
                        "similar_title": str(top.get("title", ""))[:60],
                        "similarity": top.get("similarity", 0),
                    },
                )
                return True
            return False
        except Exception as exc:
            logger.debug("is_duplicate_before_job failed (non-critical): %s", exc)
            return False
