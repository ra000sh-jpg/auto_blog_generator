"""발행 이력 기반 커버리지/중복 분석 엔진."""

from __future__ import annotations

import asyncio
import logging
import threading
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
        lexical_threshold = (
            float(similarity_threshold)
            if similarity_threshold is not None
            else float(getattr(self._config, "precheck_duplicate_threshold", 0.50))
        )
        hybrid_threshold = (
            float(similarity_threshold)
            if similarity_threshold is not None
            else float(getattr(self._config, "hybrid_threshold", lexical_threshold))
        )
        try:
            from .embedding_provider import build_embedding_provider
            from .hybrid_similarity import (
                compute_semantic_scores,
                find_similar_posts_with_optional_semantic,
                should_apply_semantic,
            )

            fn = getattr(self._store, "query_topic_memory", None)
            if not callable(fn):
                return False
            max_candidates = int(getattr(self._config, "embedding_max_candidates", 80) or 80)
            limit = max(10, min(max_candidates, 200))

            candidates = fn(
                topic_mode=topic_mode,
                lookback_days=max(1, int(weeks)) * 7,
                limit=limit,
                platform=platform,
            )
            if not candidates:
                return False

            semantic_enabled = should_apply_semantic(self._config, topic_mode=topic_mode)
            semantic_scores: Dict[str, float] = {}
            if semantic_enabled:
                provider = build_embedding_provider(self._config)
                if provider is not None:
                    semantic_scores = self._run_async_safely(
                        compute_semantic_scores(
                            title=title,
                            keywords=keywords,
                            candidates=candidates,
                            embedding_provider=provider,
                            job_store=self._store,
                            model_name=provider.model_name,
                            max_candidates=max_candidates,
                        )
                    )

            similar = find_similar_posts_with_optional_semantic(
                title=title,
                keywords=keywords,
                candidates=candidates,
                threshold=hybrid_threshold if semantic_enabled else lexical_threshold,
                top_k=1,
                semantic_enabled=semantic_enabled and bool(semantic_scores),
                semantic_scores=semantic_scores,
                lexical_weight=float(getattr(self._config, "lexical_weight", 0.45)),
                semantic_weight=float(getattr(self._config, "semantic_weight", 0.55)),
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
                        "lexical_similarity": top.get("lexical_similarity", None),
                        "semantic_similarity": top.get("semantic_similarity", None),
                    },
                )
                return True
            return False
        except Exception as exc:
            logger.debug("is_duplicate_before_job failed (non-critical): %s", exc)
            return False

    def _run_async_safely(self, coroutine: Any) -> Dict[str, float]:
        """동기 함수 안에서 비동기 코루틴을 안전하게 실행한다."""
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if not in_running_loop:
            try:
                return asyncio.run(coroutine)
            except Exception as exc:
                logger.debug("Semantic coroutine run failed (fallback lexical): %s", exc)
                return {}

        result_box: Dict[str, Dict[str, float]] = {}
        error_box: Dict[str, Exception] = {}
        timeout_sec = float(getattr(self._config, "embedding_timeout_sec", 4.0))

        def _runner() -> None:
            try:
                result_box["result"] = asyncio.run(coroutine)
            except Exception as exc:
                error_box["error"] = exc

        worker = threading.Thread(target=_runner, daemon=True)
        worker.start()
        worker.join(timeout=max(1.0, timeout_sec + 1.0))
        if worker.is_alive():
            logger.debug("Semantic coroutine timed out in background runner")
            return {}
        if error_box:
            logger.debug(
                "Semantic coroutine failed in background runner (fallback lexical): %s",
                error_box["error"],
            )
            return {}
        return result_box.get("result", {})
