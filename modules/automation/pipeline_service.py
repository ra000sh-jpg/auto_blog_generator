"""
PipelineService - Job 처리 오케스트레이터

역할:
- Research → Brain → Mask → Quality → SEO → Image → Publish 순서 조율
- LLM 호출량 DB 동기화 (P0 #4)
- 중복 발행 방지 (P0 #2)
- QualityGate 분기 처리

Phase 1 한계:
- generator, quality_gate는 stub 또는 기존 모듈 연결
- image_gen은 None 허용 (Phase 2에서 추가)
"""

import asyncio
import json
import logging
import random
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from .. import constants
from ..exceptions import ContentGenerationError, PublishError
from ..images.placement import (
    ImageInsertionPoint,
    create_naver_editor_content,
    convert_markdown_for_naver_editor,
)
from ..metrics import MetricsStore
from .job_store import Job, JobStore
from ..uploaders.base_publisher import PublishResult
from ..uploaders.publisher_factory import get_publisher
from ..seo.quality_gate import QualityGate, QualityGateResult
from ..seo.tag_generator import TagGenerator
from ..seo.platform_strategy import get_category_for_topic

logger = logging.getLogger(__name__)


class PublisherLike(Protocol):
    """PipelineService가 기대하는 발행기 인터페이스."""

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[list[str]] = None,
        image_sources: Optional[dict[str, dict[str, str]]] = None,
        image_points: Optional[list[ImageInsertionPoint]] = None,
        tags: Optional[list[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        ...


class NotifierLike(Protocol):
    """파이프라인이 기대하는 알림 인터페이스."""

    def notify_critical_background(
        self,
        *,
        error_code: str,
        message: str,
        job_id: str = "",
    ) -> None:
        ...


class PipelineService:
    """
    Job 처리 파이프라인.

    Args:
        job_store: JobStore 인스턴스
        publisher: PlaywrightPublisher 인스턴스
        generate_fn: async (job: Job) -> Dict 콘텐츠 생성 함수
        image_gen_fn: async (prompt: str) -> Optional[str] 이미지 생성 함수 (None 허용)
    """

    MAX_API_CALLS_PER_JOB = 15  # LLM 호출 상한
    RETRY_LIMITED_ERRORS = frozenset({"QUALITY_FAILED", "NETWORK_TIMEOUT"})
    FREE_MODEL_PROVIDERS = frozenset({"groq", "cerebras"})
    PROVIDER_PRICE_PER_1K_USD = {
        "qwen": (0.0004, 0.0012),
        "deepseek": (0.00027, 0.0011),
        "groq": (0.0, 0.0),
        "cerebras": (0.0, 0.0),
        "gemini": (0.00035, 0.00105),
        "openai": (0.005, 0.015),
        "claude": (0.003, 0.015),
        "default": (0.001, 0.002),
    }
    USD_TO_KRW = 1400.0

    def __init__(
        self,
        job_store: JobStore,
        publisher: PublisherLike,
        generate_fn: Callable[[Job], Any],
        image_gen_fn: Optional[Callable[[str], Any]] = None,
        image_generator: Optional[Any] = None,
        metrics_store: Optional[MetricsStore] = None,
        retry_max_attempts: int = 3,
        retry_backoff_base_sec: float = 2.0,
        retry_backoff_max_sec: float = 60.0,
        tag_generator: Optional[TagGenerator] = None,
        quality_gate: Optional[QualityGate] = None,
        notifier: Optional[NotifierLike] = None,
        internal_retry_attempts: int = 1,
        queue_retry_limit: int = 1,
        quality_evaluator: Optional[Any] = None,  # Phase 25: QualityEvaluator (optional)
    ):
        self.job_store = job_store
        self.publisher = publisher
        self.generate_fn = generate_fn
        self.image_gen_fn = image_gen_fn
        self.image_generator = image_generator
        self.metrics_store = metrics_store
        self.retry_max_attempts = max(1, retry_max_attempts)
        self.retry_backoff_base_sec = retry_backoff_base_sec
        self.retry_backoff_max_sec = retry_backoff_max_sec
        self.tag_generator = tag_generator
        self.quality_gate = quality_gate or QualityGate()
        self.notifier = notifier
        self.internal_retry_attempts = max(0, internal_retry_attempts)
        self.queue_retry_limit = max(0, queue_retry_limit)
        self.quality_evaluator = quality_evaluator  # Phase 25
        self._channel_publishers: Dict[str, PublisherLike] = {}

    async def run_job(self, job: Job) -> None:
        """
        Job 전체 파이프라인 실행.

        Worker의 process_job 콜백으로 사용.
        성공/실패 상태 전이는 이 함수에서 처리.

        Args:
            job: 처리할 Job 객체
        """
        job_id = job.job_id
        total_start = perf_counter()
        logger.info(
            "Pipeline start",
            extra={"job_id": job_id, "title": job.title},
        )

        try:
            # 준비된 초안이 있으면 즉시 발행 단계로 진입한다.
            if job.prepared_payload:
                await self._publish_payload(job, job.prepared_payload)
                return

            prepared_payload = await self._build_publish_payload(job)
            if not prepared_payload:
                return
            await self._publish_payload(job, prepared_payload)
        finally:
            logger.info(
                "Pipeline finished",
                extra={
                    "job_id": job_id,
                    "duration_ms": round((perf_counter() - total_start) * 1000, 2),
                },
            )

    async def _build_publish_payload(
        self,
        job: Job,
        allow_internal_retry: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """콘텐츠 생성 결과를 발행 가능한 payload 형태로 변환한다."""
        job_id = job.job_id

        # 중복 발행된 작업은 즉시 스킵한다.
        existing_url = self.job_store.check_already_published(job_id)
        if existing_url:
            logger.info(
                "Already published, skipping",
                extra={"job_id": job_id, "url": existing_url},
            )
            return None

        # 예산 초과면 즉시 실패 처리한다.
        if not self.job_store.check_llm_budget(job_id):
            self._record_failure_metrics("BUDGET_EXCEEDED")
            self.job_store.fail_job(job_id, "BUDGET_EXCEEDED", "LLM 호출 상한 초과")
            return None

        generation_start = perf_counter()
        try:
            content_result = await self._generate_content(job)
        except ContentGenerationError as exc:
            logger.exception("Content generation failed", extra={"job_id": job_id})
            self._record_failure_metrics("PIPELINE_ERROR")
            self.job_store.fail_job(job_id, "PIPELINE_ERROR", f"생성 실패: {str(exc)[:300]}")
            return None
        logger.info(
            "Content generation complete",
            extra={
                "job_id": job_id,
                "duration_ms": round((perf_counter() - generation_start) * 1000, 2),
            },
        )
        self._record_llm_usage_metrics(
            job_id=job_id,
            token_usage=content_result.get("llm_token_usage", {}),
        )

        quality_outcome = content_result.get("quality_gate", "pass")
        if quality_outcome in {"retry_mask", "retry_all"}:
            if quality_outcome == "retry_mask":
                self._mark_mask_retry(job_id)
            self._record_job_metric(
                job_id=job_id,
                metric_type="quality_gate",
                status="failed",
                error_code="QUALITY_FAILED",
                detail={
                    "source": "llm_quality_gate",
                    "quality_outcome": quality_outcome,
                },
            )
            if allow_internal_retry:
                logger.info(
                    "Quality self-retry by regeneration",
                    extra={"job_id": job_id, "quality_outcome": quality_outcome},
                )
                return await self._build_publish_payload(job, allow_internal_retry=False)

            self._record_failure_metrics("QUALITY_FAILED")
            self._fail_with_retry_policy(
                job=job,
                error_code="QUALITY_FAILED",
                error_message=f"LLM quality gate failed: {quality_outcome}",
            )
            return None

        gate_result = self._evaluate_quality_gate(job=job, content_result=content_result)
        if not gate_result.passed:
            if allow_internal_retry:
                repaired = self.quality_gate.repair_content(
                    content=str(content_result.get("final_content", "")),
                    issues=gate_result.issues,
                    title=job.title,
                    seed_keywords=job.seed_keywords,
                )
                if repaired != str(content_result.get("final_content", "")):
                    content_result["final_content"] = repaired
                    retry_result = self._evaluate_quality_gate(
                        job=job,
                        content_result=content_result,
                        metric_type="quality_gate_retry",
                    )
                    if retry_result.passed:
                        logger.info(
                            "Quality gate passed after local repair",
                            extra={"job_id": job_id},
                        )
                    else:
                        return await self._build_publish_payload(job, allow_internal_retry=False)
                else:
                    return await self._build_publish_payload(job, allow_internal_retry=False)
            else:
                self._record_failure_metrics("QUALITY_FAILED")
                self._fail_with_retry_policy(
                    job=job,
                    error_code="QUALITY_FAILED",
                    error_message=gate_result.summary,
                )
                return None

        # 이미지 생성은 실패해도 본문 발행을 계속 진행한다.
        thumbnail_path: Optional[str] = None
        content_image_paths: list[str] = []
        image_sources: dict[str, dict[str, str]] = {}
        seo_data = content_result.get("seo_snapshot", {})
        topic_mode = str(seo_data.get("topic_mode", "")).strip().lower()
        if self.image_generator:
            image_start = perf_counter()
            try:
                image_slots = content_result.get("image_slots")
                try:
                    images = await self.image_generator.generate_for_post(
                        title=job.title,
                        keywords=job.seed_keywords,
                        image_prompts=content_result.get("image_prompts"),
                        image_slots=image_slots,
                        topic_mode=topic_mode,
                    )
                except TypeError:
                    # 하위 호환: image_slots를 지원하지 않는 구현체는 기존 시그니처로 호출한다.
                    images = await self.image_generator.generate_for_post(
                        title=job.title,
                        keywords=job.seed_keywords,
                        image_prompts=content_result.get("image_prompts"),
                    )
                thumbnail_path = images.thumbnail_path
                content_image_paths = list(images.content_paths)
                source_kind_by_path = getattr(images, "source_kind_by_path", {}) or {}
                provider_by_path = getattr(images, "provider_by_path", {}) or {}
                for path, source_kind in source_kind_by_path.items():
                    normalized_path = str(path or "").strip()
                    if not normalized_path:
                        continue
                    image_sources[normalized_path] = {
                        "kind": str(source_kind or "unknown").strip().lower() or "unknown",
                        "provider": str(provider_by_path.get(path, "unknown")).strip().lower() or "unknown",
                    }
                for path in [thumbnail_path, *content_image_paths]:
                    normalized_path = str(path or "").strip()
                    if not normalized_path:
                        continue
                    if normalized_path not in image_sources:
                        image_sources[normalized_path] = {"kind": "stock", "provider": "unknown"}

                image_generation_logs = list(getattr(images, "generation_logs", []) or [])
                if image_generation_logs:
                    self._record_image_generation_logs(job_id=job_id, logs=image_generation_logs)
                if bool(getattr(images, "free_tier_exhausted", False)):
                    exhausted_events = list(getattr(images, "free_tier_exhausted_events", []) or [])
                    self._notify_image_free_tier_exhausted(job_id=job_id, events=exhausted_events)
            except Exception:
                logger.warning("Image generation failed, continue", extra={"job_id": job_id})
            logger.info(
                "Image generation stage complete",
                extra={
                    "job_id": job_id,
                    "duration_ms": round((perf_counter() - image_start) * 1000, 2),
                },
            )
        elif self.image_gen_fn:
            image_start = perf_counter()
            try:
                prompt = (
                    content_result.get("image_prompts", [job.title])[0]
                    if content_result.get("image_prompts")
                    else job.title
                )
                thumbnail_path = await self.image_gen_fn(prompt)
            except Exception:
                logger.warning("Image generation failed, continue", extra={"job_id": job_id})
            logger.info(
                "Image generation stage complete",
                extra={
                    "job_id": job_id,
                    "duration_ms": round((perf_counter() - image_start) * 1000, 2),
                },
            )

        tags: list[str] = list(job.tags)
        category: str = job.category
        topic_mode = str(seo_data.get("topic_mode", "")).strip()

        if not category and topic_mode:
            category = get_category_for_topic(topic_mode, job.platform)
            logger.info(
                "Category auto-assigned from topic",
                extra={"topic_mode": topic_mode, "category": category},
            )

        if self.tag_generator and not tags:
            try:
                tag_result = await self.tag_generator.generate(
                    title=job.title,
                    seed_keywords=job.seed_keywords,
                    platform=job.platform,
                    topic_mode=topic_mode,
                    content_summary=content_result.get("final_content", "")[:300],
                )
                tags = tag_result.tags
                self.job_store.update_job_tags(job_id, tags, category)
                logger.info(
                    "Tags generated",
                    extra={
                        "job_id": job_id,
                        "platform": job.platform,
                        "tag_count": len(tags),
                        "fallback": tag_result.fallback_used,
                    },
                )
            except Exception:
                logger.warning("Tag generation failed, proceeding without tags", extra={"job_id": job_id})

        raw_content = content_result.get("final_content", "")
        image_concepts = content_result.get("image_placements", [])
        concept_list = [p.get("concept", "") for p in image_concepts if isinstance(p, dict)]

        raw_content = self._inject_markdown_images(
            content=raw_content,
            thumbnail_path=thumbnail_path,
            content_image_paths=content_image_paths,
        )

        if thumbnail_path or content_image_paths:
            processed_content, image_points = create_naver_editor_content(
                content=raw_content,
                thumbnail_path=thumbnail_path,
                content_image_paths=content_image_paths,
                image_concepts=concept_list,
            )
            logger.info(
                "Content processed with image markers",
                extra={
                    "job_id": job_id,
                    "image_count": len(image_points),
                    "content_length": len(processed_content),
                },
            )
        else:
            processed_content = convert_markdown_for_naver_editor(raw_content)
            image_points = []
            logger.info(
                "Content markdown converted (no images)",
                extra={"job_id": job_id, "content_length": len(processed_content)},
            )

        return {
            "title": job.title,
            "content": processed_content,
            "thumbnail": thumbnail_path or "",
            "images": content_image_paths,
            "image_sources": image_sources,
            "image_points": [asdict(point) for point in image_points],
            "tags": tags,
            "category": category,
            "quality_snapshot": content_result.get("quality_snapshot", {}),
            "seo_snapshot": content_result.get("seo_snapshot", {}),
            "llm_token_usage": content_result.get("llm_token_usage", {}),
        }

    async def _publish_payload(self, job: Job, payload: Dict[str, Any]) -> None:
        """준비된 payload를 실제 블로그에 발행한다."""
        job_id = job.job_id
        attempt_id = str(uuid.uuid4())
        self.job_store.set_publish_attempt(job_id, attempt_id)

        image_points: list[ImageInsertionPoint] = []
        for raw_point in payload.get("image_points", []):
            if not isinstance(raw_point, dict):
                continue
            try:
                image_points.append(
                    ImageInsertionPoint(
                        index=int(raw_point["index"]),
                        path=str(raw_point["path"]),
                        marker=str(raw_point["marker"]),
                        section_hint=str(raw_point["section_hint"]),
                        is_thumbnail=bool(raw_point["is_thumbnail"]),
                    )
                )
            except KeyError:
                continue

        raw_images = payload.get("images", [])
        images = [str(path) for path in raw_images] if isinstance(raw_images, list) else []
        raw_image_sources = payload.get("image_sources", {})
        image_sources: dict[str, dict[str, str]] = {}
        if isinstance(raw_image_sources, dict):
            for path, meta in raw_image_sources.items():
                normalized_path = str(path or "").strip()
                if not normalized_path:
                    continue
                if isinstance(meta, dict):
                    image_sources[normalized_path] = {
                        "kind": str(meta.get("kind", "unknown")).strip().lower() or "unknown",
                        "provider": str(meta.get("provider", "unknown")).strip().lower() or "unknown",
                    }
                else:
                    image_sources[normalized_path] = {"kind": "unknown", "provider": "unknown"}
        raw_tags = payload.get("tags", [])
        tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []

        raw_category = str(payload.get("category", "")).strip() or None
        mapped_category = raw_category
        if raw_category:
            try:
                import json
                raw_mapping = self.job_store.get_system_setting("category_mapping", "{}")
                mapping_dict = json.loads(raw_mapping)
                if isinstance(mapping_dict, dict) and raw_category in mapping_dict:
                    resolved_name = mapping_dict[raw_category].strip()
                    if resolved_name:
                        mapped_category = resolved_name
            except Exception:
                pass

        shadow_mode_enabled = self._should_shadow_publish(job=job, payload=payload)
        if shadow_mode_enabled:
            shadow_url = f"shadow://{job_id}/{attempt_id}"
            self._record_job_metric(
                job_id=job_id,
                metric_type="publish",
                status="shadow",
                detail={"message": "shadow mode: publish skipped"},
            )
            self.job_store.complete_job(
                job_id=job_id,
                result_url=shadow_url,
                thumbnail_url=str(payload.get("thumbnail", "")),
                quality_snapshot=payload.get("quality_snapshot", {}),
                seo_snapshot=payload.get("seo_snapshot", {}),
            )
            self._record_model_performance(job=job, payload=payload, post_id=shadow_url)
            mark_consumed = getattr(self.job_store, "mark_idea_vault_consumed_by_job", None)
            if mark_consumed and callable(mark_consumed):
                try:
                    mark_consumed(job_id)
                except Exception:
                    logger.debug("idea_vault consumed mark skipped", extra={"job_id": job_id})
            if self.metrics_store:
                self.metrics_store.record_jobs_total("completed")
            logger.info(
                "Shadow publish completed",
                extra={"job_id": job_id, "url": shadow_url},
            )
            return

        try:
            publisher = self._resolve_publisher_for_job(job)
        except Exception as exc:
            result = PublishResult(
                success=False,
                error_code="PUBLISHER_NOT_AVAILABLE",
                error_message=str(exc)[:300],
            )
            self._record_failure_metrics(result.error_code)
            self._record_job_metric(
                job_id=job_id,
                metric_type="publish",
                status="failed",
                error_code=result.error_code,
                detail={"message": result.error_message},
            )
            self._fail_with_retry_policy(
                job=job,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            return

        result, publish_duration_sec = await self._publish_with_retry(
            publisher=publisher,
            job_id=job_id,
            title=str(payload.get("title", job.title)),
            content=str(payload.get("content", "")),
            thumbnail=str(payload.get("thumbnail", "")) or None,
            images=[path for path in images if path],
            image_sources=image_sources,
            image_points=image_points,
            tags=[tag for tag in tags if tag] or None,
            category=mapped_category,
        )
        if self.metrics_store:
            self.metrics_store.record_publish_duration_seconds(publish_duration_sec)

        if result.success:
            self._record_job_metric(
                job_id=job_id,
                metric_type="publish",
                status="success",
                duration_ms=publish_duration_sec * 1000.0,
            )
            self.job_store.complete_job(
                job_id=job_id,
                result_url=result.url,
                thumbnail_url=str(payload.get("thumbnail", "")),
                quality_snapshot=payload.get("quality_snapshot", {}),
                seo_snapshot=payload.get("seo_snapshot", {}),
            )
            self._record_model_performance(job=job, payload=payload, post_id=result.url)
            mark_consumed = getattr(self.job_store, "mark_idea_vault_consumed_by_job", None)
            if mark_consumed and callable(mark_consumed):
                try:
                    mark_consumed(job_id)
                except Exception:
                    logger.debug("idea_vault consumed mark skipped", extra={"job_id": job_id})
            if self.metrics_store:
                self.metrics_store.record_jobs_total("completed")
            logger.info(
                "Pipeline done",
                extra={"job_id": job_id, "url": result.url},
            )
            return

        self._record_failure_metrics(result.error_code)
        self._record_job_metric(
            job_id=job_id,
            metric_type="publish",
            status="failed",
            error_code=result.error_code,
            duration_ms=publish_duration_sec * 1000.0,
            detail={"message": result.error_message[:300]},
        )
        logger.warning(
            "Publish failed",
            extra={"job_id": job_id, "error_code": result.error_code},
        )
        self._fail_with_retry_policy(
            job=job,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        if self.notifier:
            self.notifier.notify_critical_background(
                error_code=result.error_code,
                message=result.error_message,
                job_id=job_id,
            )

    async def _generate_content(self, job: Job) -> Dict[str, Any]:
        """
        콘텐츠 생성 + LLM 카운터 증가.

        generate_fn이 동기/비동기 모두 허용.
        """
        try:
            if asyncio.iscoroutinefunction(self.generate_fn):
                result = await self.generate_fn(job)
            else:
                # 동기 함수는 executor에서 실행
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, self.generate_fn, job)
        except Exception as exc:
            raise ContentGenerationError(str(exc)) from exc

        if not isinstance(result, dict):
            raise ContentGenerationError("생성 결과 형식이 dict가 아닙니다.")

        # LLM 호출 횟수 DB 기록 (Brain + Mask + SEO = 3)
        llm_calls = result.get("llm_calls_used", 3)
        total = self.job_store.increment_llm_calls(job.job_id, llm_calls)
        if self.metrics_store:
            self.metrics_store.record_llm_calls_total(int(llm_calls))
        logger.debug(f"LLM calls for {job.job_id}: +{llm_calls} = {total}")

        return result

    async def _publish_with_retry(
        self,
        publisher: PublisherLike,
        job_id: str,
        title: str,
        content: str,
        thumbnail: Optional[str],
        images: Optional[list[str]] = None,
        image_sources: Optional[dict[str, dict[str, str]]] = None,
        image_points: Optional[list[ImageInsertionPoint]] = None,
        tags: Optional[list[str]] = None,
        category: Optional[str] = None,
    ) -> Tuple[PublishResult, float]:
        """발행을 재시도 정책과 함께 수행한다."""
        publish_start = perf_counter()
        last_result = PublishResult(
            success=False,
            error_code="PUBLISH_FAILED",
            error_message="발행 시도 실패",
        )

        total_attempts = min(
            self.retry_max_attempts,
            self.internal_retry_attempts + 1,
        )
        total_attempts = max(1, total_attempts)

        for attempt in range(1, total_attempts + 1):
            try:
                result = await publisher.publish(
                    title=title,
                    content=content,
                    thumbnail=thumbnail,
                    images=images,
                    image_sources=image_sources,
                    image_points=image_points,
                    tags=tags,
                    category=category,
                )
            except PublishError as exc:
                result = PublishResult(
                    success=False,
                    error_code=exc.error_code,
                    error_message=str(exc),
                )
                logger.warning(
                    "PublishError captured",
                    extra={
                        "job_id": job_id,
                        "error_code": exc.error_code,
                        "retryable": exc.retryable,
                        "attempt": attempt,
                    },
                )
            except Exception as exc:
                result = PublishResult(
                    success=False,
                    error_code="PIPELINE_ERROR",
                    error_message=str(exc)[:300],
                )
                logger.exception(
                    "Publisher unexpected error",
                    extra={"job_id": job_id, "attempt": attempt},
                )

            if result.success:
                return result, perf_counter() - publish_start

            last_result = result
            retryable = self._is_retryable_publish_error(result.error_code, publisher)
            if not retryable or attempt >= total_attempts:
                return last_result, perf_counter() - publish_start

            delay_sec = self._retry_delay_for_attempt(attempt)
            logger.warning(
                "Retry publish",
                extra={
                    "job_id": job_id,
                    "attempt": attempt,
                    "next_delay_sec": round(delay_sec, 3),
                    "error_code": result.error_code,
                },
            )
            await asyncio.sleep(delay_sec)

        return last_result, perf_counter() - publish_start

    def _is_retryable_publish_error(self, error_code: str, publisher: PublisherLike) -> bool:
        retryable_errors: set[str] = set(getattr(publisher, "RETRYABLE_ERRORS", set()))
        return error_code in retryable_errors

    def _resolve_publisher_for_job(self, job: Job) -> PublisherLike:
        """잡 메타데이터를 기준으로 발행기를 선택한다."""
        if job.job_kind != self.job_store.JOB_KIND_SUB:
            return self.publisher

        channel_id = str(job.channel_id or "").strip()
        if not channel_id:
            return self.publisher

        cached = self._channel_publishers.get(channel_id)
        if cached is not None:
            return cached

        get_channel_fn = getattr(self.job_store, "get_channel", None)
        if not callable(get_channel_fn):
            raise RuntimeError("JobStore.get_channel is not available")

        channel = get_channel_fn(channel_id)
        if not isinstance(channel, dict):
            raise RuntimeError(f"channel not found: {channel_id}")
        if not bool(channel.get("active", False)):
            raise RuntimeError(f"channel is inactive: {channel_id}")

        publisher = get_publisher(channel)
        self._channel_publishers[channel_id] = publisher
        return publisher

    def _retry_delay_for_attempt(self, attempt: int) -> float:
        base_delay = self.retry_backoff_base_sec * (2 ** (attempt - 1))
        capped_delay = min(base_delay, self.retry_backoff_max_sec)
        return capped_delay + random.uniform(0.0, 0.5)

    def _record_failure_metrics(self, error_code: str) -> None:
        if self.metrics_store:
            self.metrics_store.record_jobs_total("failed")
            self.metrics_store.record_errors_total(error_code)

    def _record_job_metric(
        self,
        *,
        job_id: str,
        metric_type: str,
        status: str,
        duration_ms: float = 0.0,
        error_code: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        provider: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        record = getattr(self.job_store, "record_job_metric", None)
        if record and callable(record):
            record(
                job_id=job_id,
                metric_type=metric_type,
                status=status,
                duration_ms=duration_ms,
                error_code=error_code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider=provider,
                detail=detail or {},
            )

    def _record_image_generation_logs(
        self,
        *,
        job_id: str,
        logs: list[Dict[str, Any]],
    ) -> None:
        """이미지 슬롯 실행 로그를 DB에 저장한다."""
        record_fn = getattr(self.job_store, "record_image_generation_log", None)
        if not callable(record_fn):
            return

        for row in logs:
            if not isinstance(row, dict):
                continue
            slot_id = str(row.get("slot_id", "")).strip()
            if not slot_id:
                continue
            try:
                record_fn(
                    post_id=job_id,
                    slot_id=slot_id,
                    slot_role=str(row.get("slot_role", "content")).strip().lower() or "content",
                    provider=str(row.get("provider", "unknown")).strip().lower() or "unknown",
                    status=str(row.get("status", "failed")).strip().lower() or "failed",
                    latency_ms=float(row.get("latency_ms", 0.0) or 0.0),
                    fallback_reason=str(row.get("fallback_reason", "")).strip(),
                    cost_usd=float(row.get("cost_usd", 0.0) or 0.0),
                    source_url=str(row.get("source_url", "")).strip(),
                )
            except Exception as exc:
                logger.warning("record_image_generation_log failed: %s", exc)

    def _notify_image_free_tier_exhausted(
        self,
        *,
        job_id: str,
        events: list[Dict[str, Any]],
    ) -> None:
        """무료 이미지 티어 소진 알림을 일일 1회로 제한해 전송한다."""
        if not self.notifier:
            return
        send_background = getattr(self.notifier, "send_message_background", None)
        if not callable(send_background):
            return

        normalized_providers: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            provider = str(event.get("provider", "")).strip().lower()
            if provider and provider not in normalized_providers:
                normalized_providers.append(provider)
        if not normalized_providers:
            normalized_providers = ["together_flux"]

        kst_date = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")
        should_send = False
        for provider in normalized_providers:
            dedupe_key = f"image_free_tier_alert_{provider}_{kst_date}"
            already_sent = str(self.job_store.get_system_setting(dedupe_key, "0")).strip() == "1"
            if already_sent:
                continue
            self.job_store.set_system_setting(dedupe_key, "1")
            should_send = True

        if not should_send:
            return

        provider_text = ", ".join(normalized_providers)
        text = (
            "🚨 [이미지 무료 티어 소진 감지]\n"
            f"- job_id: {job_id}\n"
            f"- provider: {provider_text}\n"
            "- 조치: 유료 자동 승격 없이 Pexels 실사진으로 폴백 처리"
        )
        try:
            send_background(text, disable_notification=False)
        except Exception as exc:
            logger.warning("Image free-tier alert send failed: %s", exc)

    def _record_llm_usage_metrics(
        self,
        *,
        job_id: str,
        token_usage: Any,
    ) -> None:
        """생성 결과에 포함된 토큰 사용량을 단계별 메트릭으로 기록한다."""
        if not isinstance(token_usage, dict):
            return

        for metric_type in ("parser", "quality_step", "voice_step"):
            raw = token_usage.get(metric_type, {})
            if not isinstance(raw, dict):
                continue

            input_tokens = max(0, int(raw.get("input_tokens", 0) or 0))
            output_tokens = max(0, int(raw.get("output_tokens", 0) or 0))
            total_tokens = input_tokens + output_tokens
            if total_tokens <= 0:
                continue

            provider = str(raw.get("provider", "")).strip()
            model = str(raw.get("model", "")).strip()
            call_count = max(0, int(raw.get("calls", 0) or 0))

            self._record_job_metric(
                job_id=job_id,
                metric_type=metric_type,
                status="ok",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider=provider,
                detail={
                    "model": model,
                    "calls": call_count,
                    "total_tokens": total_tokens,
                },
            )

    def _evaluate_quality_gate(
        self,
        *,
        job: Job,
        content_result: Dict[str, Any],
        metric_type: str = "quality_gate",
    ) -> QualityGateResult:
        start = perf_counter()
        seo_snapshot = content_result.get("seo_snapshot", {})
        topic_mode = str(seo_snapshot.get("topic_mode", ""))
        rag_context = content_result.get("rag_context", [])
        gate_result = self.quality_gate.evaluate(
            title=job.title,
            content=str(content_result.get("final_content", "")),
            seed_keywords=list(job.seed_keywords),
            topic_mode=topic_mode,
            rag_context=rag_context if isinstance(rag_context, list) else [],
        )

        snapshot = dict(content_result.get("quality_snapshot", {}) or {})
        snapshot["firewall"] = gate_result.to_dict()
        content_result["quality_snapshot"] = snapshot

        self._record_job_metric(
            job_id=job.job_id,
            metric_type=metric_type,
            status="pass" if gate_result.passed else "failed",
            duration_ms=(perf_counter() - start) * 1000.0,
            error_code=gate_result.error_code,
            detail=gate_result.to_dict(),
            )
        return gate_result

    def _normalize_category_name(self, value: str) -> str:
        """카테고리 문자열 비교용 정규화."""
        return re.sub(r"\s+", "", str(value or "").lower())

    def _resolve_slot_type(self, job: Job) -> str:
        """작업의 성능 슬롯 유형(main/shadow/challenger)을 판단한다."""
        fallback_category = str(
            self.job_store.get_system_setting("fallback_category", "다양한 생각들")
        ).strip() or "다양한 생각들"
        normalized_job_category = self._normalize_category_name(job.category)
        normalized_fallback = self._normalize_category_name(fallback_category)
        if normalized_job_category and normalized_job_category == normalized_fallback:
            phase = str(self.job_store.get_system_setting("router_competition_phase", "idle")).strip().lower()
            if phase == "champion_ops":
                return "challenger"
            return "shadow"
        return "main"

    def _should_shadow_publish(self, *, job: Job, payload: Dict[str, Any]) -> bool:
        """Shadow 테스트 모드에서 실제 발행을 건너뛸지 판단한다."""
        del payload
        shadow_mode = str(self.job_store.get_system_setting("router_shadow_mode", "false")).strip().lower()
        phase = str(self.job_store.get_system_setting("router_competition_phase", "idle")).strip().lower()
        slot_type = self._resolve_slot_type(job)
        return shadow_mode in {"1", "true", "yes", "on"} and phase == "testing" and slot_type == "shadow"

    def _estimate_text_cost_won(self, provider: str, token_usage: Dict[str, Any]) -> float:
        """토큰 사용량 기반 텍스트 비용을 KRW로 추정한다."""
        provider_key = str(provider).strip().lower()
        input_price, output_price = self.PROVIDER_PRICE_PER_1K_USD.get(
            provider_key,
            self.PROVIDER_PRICE_PER_1K_USD["default"],
        )
        total_input_tokens = 0
        total_output_tokens = 0
        for stage in ("parser", "quality_step", "voice_step"):
            stage_data = token_usage.get(stage, {})
            if not isinstance(stage_data, dict):
                continue
            total_input_tokens += int(stage_data.get("input_tokens", 0) or 0)
            total_output_tokens += int(stage_data.get("output_tokens", 0) or 0)
        cost_usd = ((total_input_tokens / 1000.0) * input_price) + ((total_output_tokens / 1000.0) * output_price)
        return round(cost_usd * self.USD_TO_KRW, 4)

    def _record_model_performance(self, *, job: Job, payload: Dict[str, Any], post_id: str) -> None:
        """모델 성능 로그를 저장한다."""
        recorder = getattr(self.job_store, "record_model_performance", None)
        if not recorder or not callable(recorder):
            return

        seo_snapshot = payload.get("seo_snapshot", {}) if isinstance(payload.get("seo_snapshot"), dict) else {}
        quality_snapshot = payload.get("quality_snapshot", {}) if isinstance(payload.get("quality_snapshot"), dict) else {}
        token_usage = payload.get("llm_token_usage", {}) if isinstance(payload.get("llm_token_usage"), dict) else {}

        provider = str(seo_snapshot.get("provider_used", "")).strip().lower()
        model_id = str(seo_snapshot.get("provider_model", "")).strip()
        if not provider:
            provider = "unknown"
        if not model_id:
            model_id = provider

        topic_mode = str(seo_snapshot.get("topic_mode", "cafe")).strip().lower() or "cafe"
        quality_score = float(quality_snapshot.get("score", 0.0) or 0.0)
        cost_won = self._estimate_text_cost_won(provider, token_usage)
        slot_type = self._resolve_slot_type(job)
        is_free_model = provider in self.FREE_MODEL_PROVIDERS

        try:
            recorder(
                model_id=model_id,
                provider=provider,
                topic_mode=topic_mode,
                quality_score=quality_score,
                cost_won=cost_won,
                is_free_model=is_free_model,
                slot_type=slot_type,
                post_id=post_id,
                feedback_source="ai_evaluator",
            )
        except Exception:
            logger.debug("model_performance_log skipped", extra={"job_id": job.job_id}, exc_info=True)

    def _fail_with_retry_policy(
        self,
        *,
        job: Job,
        error_code: str,
        error_message: str,
    ) -> None:
        force_final = False
        if (
            error_code in self.RETRY_LIMITED_ERRORS
            and job.retry_count >= self.queue_retry_limit
        ):
            force_final = True

        self.job_store.fail_job(
            job.job_id,
            error_code,
            error_message[:500],
            force_final=force_final,
        )

    async def process_generation(self, job: Job) -> bool:
        """생성 단계만 수행하고 결과를 ready_to_publish로 저장한다."""
        if job.prepared_payload:
            restored = self.job_store.save_prepared_payload(job.job_id, job.prepared_payload)
            if restored:
                logger.info("Prepared draft restored", extra={"job_id": job.job_id})
            return restored

        payload = await self._build_publish_payload(job, allow_internal_retry=True)
        if not payload:
            return False

        # ──────────────────────────────────────────────────────────
        # Phase 25: Gate 2 - LLM 기반 페르소나 톤앤매너 평가
        # quality_evaluator가 주입된 경우에만 실행 (선택적)
        # ──────────────────────────────────────────────────────────
        if self.quality_evaluator is not None:
            # 페르소나 설명 문자열 수집
            persona_id = str(getattr(job, "persona_id", ""))
            persona_desc = f"persona_id={persona_id}"
            # content_generator가 quality_snapshot에 persona 정보를 저장한 경우 활용
            q_snap = payload.get("quality_snapshot", {})
            if isinstance(q_snap, dict):
                tone = q_snap.get("tone_hint") or q_snap.get("persona_tone", "")
                if tone:
                    persona_desc = f"persona_id={persona_id}, tone={tone}"

            final_content = str(payload.get("final_content", ""))

            # 현재 평가 횟수는 job 메타 quality_snapshot에 체크포인트로 저장
            q_meta = q_snap if isinstance(q_snap, dict) else {}
            eval_retry_count = int(q_meta.get("evaluator_retry_count", 0))

            eval_result = await self.quality_evaluator.evaluate(
                content=final_content,
                persona_desc=persona_desc,
                retry_count=eval_retry_count,
            )

            # 점수를 quality_snapshot에 기록
            if not isinstance(payload.get("quality_snapshot"), dict):
                payload["quality_snapshot"] = {}
            payload["quality_snapshot"]["gate2_score"] = eval_result.score
            payload["quality_snapshot"]["gate2_passed"] = eval_result.passed
            payload["quality_snapshot"]["gate2_feedback"] = eval_result.feedback

            if not eval_result.passed:
                if eval_result.gate == "correction_needed":
                    # 재작성 루프: feedback을 generate_fn에 주입하고 재생성
                    correction_prompt = self.quality_evaluator.build_correction_prompt(
                        original_content=final_content,
                        feedback=eval_result.feedback,
                        persona_desc=persona_desc,
                    )
                    logger.info(
                        "Gate 2 correction loop triggered (retry %d/%d)",
                        eval_retry_count + 1,
                        self.quality_evaluator.max_retries,
                        extra={"job_id": job.job_id},
                    )
                    # Job 메타데이터에 피드백 기록 후 재시도
                    with self.job_store.connection() as conn:
                        from .time_utils import now_utc
                        import json as _json
                        _snap = {**q_meta,
                                 "evaluator_retry_count": eval_retry_count + 1,
                                 "evaluator_feedback": eval_result.feedback,
                                 "correction_prompt": correction_prompt}
                        conn.execute(
                            "UPDATE jobs SET quality_snapshot=?, updated_at=? WHERE job_id=?",
                            (_json.dumps(_snap), now_utc(), job.job_id)
                        )
                    # 재생성 시도 (한 번만): 기존 _build_publish_payload 재사용
                    payload = await self._build_publish_payload(job, allow_internal_retry=False)
                    if not payload:
                        return False

                elif eval_result.gate == "rejected":
                    # 최대 재시도 초과: 품질 실패 상태로 확정
                    logger.warning(
                        "Gate 2 rejected: QUALITY_REJECTED (job=%s, score=%d)",
                        job.job_id, eval_result.score,
                    )
                    self._fail_with_retry_policy(
                        job=job,
                        error_code="QUALITY_REJECTED",
                        error_message=f"페르소나 일치도 미달 (score={eval_result.score}/100): {eval_result.feedback[:200]}",
                    )
                    if self.notifier:
                        self.notifier.notify_critical_background(
                            error_code="QUALITY_REJECTED",
                            message=(
                                f"🚫 [Quality Gate 2] Job 품질 반려\n"
                                f"• job_id: {job.job_id}\n"
                                f"• score: {eval_result.score}/100\n"
                                f"• 사유: {eval_result.feedback[:150]}"
                            ),
                            job_id=job.job_id,
                        )
                    return False
        # ──────────────────────────────────────────────────────────

        saved = self.job_store.save_prepared_payload(job.job_id, payload)
        if saved:
            logger.info("Draft prepared", extra={"job_id": job.job_id})
            return True

        logger.warning("Draft save failed", extra={"job_id": job.job_id})
        self._fail_with_retry_policy(
            job=job,
            error_code="PIPELINE_ERROR",
            error_message="초안 저장 실패",
        )
        return False

    async def process_publication(self, job: Job) -> bool:
        """발행 단계만 수행한다. 준비된 payload가 없으면 즉시 생성 후 발행한다."""
        payload = job.prepared_payload
        if not payload:
            payload = await self._build_publish_payload(job, allow_internal_retry=True)
            if not payload:
                return False

        await self._publish_payload(job, payload)
        updated = self.job_store.get_job(job.job_id)
        return bool(updated and updated.status == self.job_store.STATUS_COMPLETED)

    def _mark_mask_retry(self, job_id: str) -> None:
        """기존 retry_mask 플래그 호환성을 유지한다."""
        job = self.job_store.get_job(job_id)
        if not job:
            return

        snapshot = job.quality_snapshot or {}
        snapshot["mask_retry_done"] = True

        with self.job_store.connection() as conn:
            from .time_utils import now_utc

            conn.execute(
                "UPDATE jobs SET quality_snapshot = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(snapshot), now_utc(), job_id),
            )

    async def prepare_next_pending_job(self, job_kind: Optional[str] = None) -> bool:
        """대기 Job 1건을 선생성해 ready 상태로 저장한다."""
        jobs = self.job_store.claim_due_jobs(limit=1, job_kind=job_kind)
        if not jobs:
            logger.debug("No pending jobs to prepare")
            return False

        job = jobs[0]
        return await self.process_generation(job)

    async def publish_next_ready_job(self, job_kind: Optional[str] = None) -> bool:
        """ready 상태 Job 1건을 발행한다."""
        jobs = self.job_store.claim_ready_jobs(limit=1, job_kind=job_kind)
        if not jobs:
            logger.debug("No prepared jobs to publish")
            return False

        return await self.process_publication(jobs[0])

    async def run_next_pending_job(self, job_kind: Optional[str] = None) -> bool:
        """대기 중인 다음 Job 1건을 선점해 실행한다."""
        if await self.publish_next_ready_job(job_kind=job_kind):
            return True

        jobs = self.job_store.claim_due_jobs(limit=1, job_kind=job_kind)
        if not jobs:
            logger.debug("No pending jobs to run")
            return False

        await self.run_job(jobs[0])
        return True

    def _inject_markdown_images(
        self,
        *,
        content: str,
        thumbnail_path: Optional[str],
        content_image_paths: list[str],
    ) -> str:
        """본문에 이미지 마크다운 장치를 삽입한다."""
        text = str(content or "").strip()
        if not text:
            return text
        if re.search(r"!\[[^\]]*\]\([^)]+\)", text):
            return text

        markdown_urls: list[str] = []
        if thumbnail_path:
            markdown_urls.append(str(thumbnail_path))
        for path in content_image_paths:
            normalized = str(path).strip()
            if normalized:
                markdown_urls.append(normalized)
        if not markdown_urls:
            return text

        image_blocks = [f"![img]({url})" for url in markdown_urls]
        lines = text.splitlines()
        insert_index = max(1, len(lines) // 2)

        merged_lines = [
            *lines[:insert_index],
            "",
            *image_blocks,
            "",
            *lines[insert_index:],
        ]
        return "\n".join(merged_lines).strip()

# ──────────────────────────────────────────────────────────────────────────────
# Stub generate_fn (Phase 1 로컬 테스트용)
# 실제 generator 모듈 연결 시 교체
# ──────────────────────────────────────────────────────────────────────────────

async def stub_generate_fn(job: Job) -> Dict[str, Any]:
    """
    Phase 1 테스트용 콘텐츠 생성 stub.

    실제 LLM 호출 없이 더미 콘텐츠 반환.
    """
    await asyncio.sleep(constants.PIPELINE_STUB_ASYNC_DELAY_SEC)  # 비동기 시뮬레이션

    keywords = ", ".join(job.seed_keywords[:3])
    content = f"""# {job.title}

안녕하세요! 오늘은 {keywords}에 대해 알아보겠습니다.

## 소개

{job.title}는 많은 분들이 관심을 가지고 있는 주제입니다.
{keywords}를 중심으로 자세히 살펴보겠습니다.

## 주요 내용

1. {job.seed_keywords[0] if job.seed_keywords else "키워드"} 의 개념과 중요성
2. 실제 활용 방법
3. 추천 팁과 노하우

## 실전 체크리스트

- 핵심 키워드 재정리: {keywords}
- 독자가 바로 실행할 수 있는 단계별 순서 작성
- 실패 사례와 예방법을 함께 정리

## 자주 묻는 질문

Q. {job.title}를 처음 시작할 때 무엇이 가장 중요할까요?  
A. 기본 개념을 빠르게 익힌 뒤 작은 단위로 바로 적용해보는 것이 중요합니다.

Q. {keywords}를 동시에 다뤄도 괜찮을까요?  
A. 우선순위를 정해 1~2개부터 실행하고, 성과를 본 뒤 확장하는 것이 안정적입니다.

## 마무리

오늘 알아본 {job.title} 내용이 도움이 되셨으면 합니다.
{keywords}에 대해 더 궁금하신 점은 댓글로 남겨주세요!
"""

    return {
        "final_content": content,
        "quality_gate": "pass",
        "quality_snapshot": {"score": 80, "issues": []},
        "seo_snapshot": {
            "keyword_count": 5,
            "density": 2.1,
            "provider_used": "stub",
            "provider_model": "stub",
            "provider_fallback_from": "",
        },
        "image_prompts": [f"{job.title} 블로그 썸네일"],
        "llm_calls_used": 3,
        "provider_used": "stub",
        "provider_model": "stub",
        "provider_fallback_from": "",
    }
