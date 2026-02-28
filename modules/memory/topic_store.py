"""topic_memory 테이블 파사드 — 저장·조회·백필 로직 캡슐화.

job_store.py의 쿼리 메서드를 래핑하는 얇은 파사드 레이어.
모든 메서드는 예외를 조용히 처리 — 파이프라인 블로킹 없음.
"""

from __future__ import annotations

import asyncio
import logging
import threading
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
        self._backfill_enqueued = False
        self._event_queue: Optional[asyncio.Queue[Any]] = None

    def bind_event_queue(self, queue: Optional[asyncio.Queue[Any]]) -> None:
        """메모리 이벤트 큐를 바인딩한다."""
        self._event_queue = queue

    def is_async_pipeline_enabled(self) -> bool:
        """메모리 비동기 파이프라인 활성화 여부를 반환한다."""
        return bool(getattr(self._config, "async_pipeline_enabled", False))

    def request_backfill(self, *, limit: int = 300) -> None:
        """백필 요청을 큐에 적재한다. 큐가 없으면 동기 백필로 폴백한다."""
        if self._backfilled or not self._config.backfill_on_init:
            return
        if self.is_async_pipeline_enabled() and self._event_queue is not None:
            if self._backfill_enqueued:
                return
            queued = self._enqueue_event(
                {
                    "type": "ensure_backfill",
                    "payload": {"limit": int(limit)},
                    "attempts": 0,
                }
            )
            if queued:
                self._backfill_enqueued = True
                return
        if self.is_async_pipeline_enabled():
            if self._backfill_enqueued:
                return
            self._backfill_enqueued = True
            worker = threading.Thread(
                target=self._run_backfill_now,
                kwargs={"limit": int(limit)},
                daemon=True,
            )
            worker.start()
            return
        self.ensure_backfilled()

    def ensure_backfilled(self) -> None:
        """최초 1회 기존 jobs 데이터를 백필한다."""
        if self._backfilled or not self._config.backfill_on_init:
            return
        self._run_backfill_now(limit=300)

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
        if self.is_async_pipeline_enabled() and self._event_queue is not None:
            queued = self._enqueue_event(
                {
                    "type": "record_post",
                    "payload": {
                        "job_id": str(job_id),
                        "title": str(title),
                        "keywords": list(keywords),
                        "topic_mode": str(topic_mode),
                        "platform": str(platform),
                        "persona_id": str(persona_id),
                        "result_url": str(result_url),
                        "quality_score": int(quality_score),
                    },
                    "attempts": 0,
                }
            )
            if queued:
                return
            logger.debug("Memory queue enqueue failed, fallback to sync record_post")
        self._record_post_now(
            job_id=str(job_id),
            title=str(title),
            keywords=list(keywords),
            topic_mode=str(topic_mode),
            platform=str(platform),
            persona_id=str(persona_id),
            result_url=str(result_url),
            quality_score=int(quality_score),
        )

    def process_memory_event(self, event: Dict[str, Any]) -> bool:
        """큐 이벤트를 처리한다."""
        event_type = str(event.get("type", "")).strip()
        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        if event_type == "ensure_backfill":
            limit = int(payload.get("limit", 300) or 300)
            self._run_backfill_now(limit=limit)
            return True
        if event_type == "record_post":
            self._record_post_now(
                job_id=str(payload.get("job_id", "")),
                title=str(payload.get("title", "")),
                keywords=list(payload.get("keywords", [])),
                topic_mode=str(payload.get("topic_mode", "cafe") or "cafe"),
                platform=str(payload.get("platform", "naver") or "naver"),
                persona_id=str(payload.get("persona_id", "P1") or "P1"),
                result_url=str(payload.get("result_url", "")),
                quality_score=int(payload.get("quality_score", 0) or 0),
            )
            return True
        logger.debug("Unknown memory event skipped: %s", event_type)
        return False

    def _run_backfill_now(self, *, limit: int = 300) -> None:
        """동기 백필을 즉시 실행한다."""
        if self._backfilled or not self._config.backfill_on_init:
            return
        try:
            fn = getattr(self._store, "backfill_topic_memory_from_jobs", None)
            if callable(fn):
                count = fn(limit=max(1, int(limit)))
                if count:
                    logger.info("topic_memory backfilled: %d posts", count)
        except Exception as exc:
            logger.debug("Backfill skipped: %s", exc)
        self._backfilled = True
        self._backfill_enqueued = False

    def _record_post_now(
        self,
        *,
        job_id: str,
        title: str,
        keywords: List[str],
        topic_mode: str,
        platform: str,
        persona_id: str,
        result_url: str,
        quality_score: int,
    ) -> None:
        """발행 이력 기록을 동기 방식으로 실행한다."""
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
            return

        # Phase A-6: 발행 직후 임베딩 저장 (실패 시 조용히 폴백)
        self._store_post_embedding(
            job_id=job_id,
            title=title,
            keywords=keywords,
            topic_mode=topic_mode,
        )

    def _enqueue_event(self, event: Dict[str, Any]) -> bool:
        """이벤트를 큐에 적재한다."""
        if self._event_queue is None:
            return False
        try:
            self._event_queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning("Memory event queue is full, fallback to sync path")
            return False
        except Exception as exc:
            logger.debug("Memory event enqueue failed: %s", exc)
            return False

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

    def _store_post_embedding(
        self,
        *,
        job_id: str,
        title: str,
        keywords: List[str],
        topic_mode: str,
    ) -> None:
        """발행 완료 글 임베딩을 생성해 topic_memory_embeddings에 저장한다."""
        try:
            from .embedding_provider import build_embedding_provider
            from .hybrid_similarity import should_apply_semantic

            if not should_apply_semantic(self._config, topic_mode=topic_mode):
                return
            provider = build_embedding_provider(self._config)
            if provider is None:
                return
            upsert_fn = getattr(self._store, "upsert_topic_embedding", None)
            if not callable(upsert_fn):
                return

            embedding_text = self._build_embedding_text(title=title, keywords=keywords)
            vectors = self._run_async_safely(provider.embed_texts([embedding_text]))
            if not vectors or not vectors[0]:
                return
            upsert_fn(
                job_id=str(job_id),
                embedding=vectors[0],
                model_name=provider.model_name,
            )
        except Exception as exc:
            logger.debug("topic_memory embedding store failed (non-critical): %s", exc)

    def _build_embedding_text(self, *, title: str, keywords: List[str]) -> str:
        """임베딩 입력 텍스트를 구성한다."""
        kw_text = ", ".join(str(keyword).strip() for keyword in keywords if str(keyword).strip())
        if kw_text:
            return f"{str(title).strip()}\n키워드: {kw_text}".strip()
        return str(title).strip()

    def _run_async_safely(self, coroutine: Any) -> List[List[float]]:
        """동기 함수 안에서 비동기 코루틴을 안전하게 실행한다."""
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        timeout_sec = float(getattr(self._config, "embedding_timeout_sec", 4.0))
        if not in_running_loop:
            try:
                return asyncio.run(asyncio.wait_for(coroutine, timeout=max(1.0, timeout_sec)))
            except Exception as exc:
                logger.debug("record_post semantic run failed (fallback): %s", exc)
                return []

        result_box: Dict[str, List[List[float]]] = {}
        error_box: Dict[str, Exception] = {}

        def _runner() -> None:
            try:
                result_box["result"] = asyncio.run(
                    asyncio.wait_for(coroutine, timeout=max(1.0, timeout_sec))
                )
            except Exception as exc:
                error_box["error"] = exc

        worker = threading.Thread(target=_runner, daemon=True)
        worker.start()
        worker.join(timeout=max(1.0, timeout_sec + 1.0))
        if worker.is_alive():
            logger.debug("record_post semantic runner timed out")
            return []
        if error_box:
            logger.debug("record_post semantic runner failed (fallback): %s", error_box["error"])
            return []
        return result_box.get("result", [])
