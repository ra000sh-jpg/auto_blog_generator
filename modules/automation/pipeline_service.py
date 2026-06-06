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
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from .. import constants
from ..exceptions import ContentGenerationError, PublishError
from ..images.placement import (
    ImageInsertionPoint,
    create_naver_editor_content,
    convert_markdown_for_naver_editor,
)
from ..images.market_chart_renderer import render_market_chart
from ..images.summary_card_renderer import render_summary_card
from ..images.table_renderer import extract_and_render_tables, extract_and_render_tables_with_validation
from ..metrics import MetricsStore
from .job_store import Job, JobStore
from ..uploaders.base_publisher import PublishResult
from ..uploaders.publisher_factory import get_publisher
from ..seo.quality_gate import QualityGate, QualityGateResult
from ..seo.tag_generator import TagGenerator
from ..seo.platform_strategy import get_category_for_topic
from .draft_approval import (
    STATUS_AWAITING_APPROVAL,
    build_draft_compact_message,
    build_draft_text_attachment,
    build_inline_keyboard,
    create_draft_approval_request,
    get_approval_ttl_hours,
    is_draft_approval_enabled,
)
from .telegram_image_collector import TelegramImageCollector, is_semi_auto_mode
from .visual_sidecar import build_visual_sidecar_from_env

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
        publish_mode: Optional[str] = None,
    ) -> PublishResult:
        ...


class NotifierLike(Protocol):
    """파이프라인이 기대하는 알림 인터페이스."""

    @property
    def enabled(self) -> bool:
        ...

    async def send_message(
        self,
        text: str,
        *,
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        ...

    def notify_critical_background(
        self,
        *,
        error_code: str,
        message: str,
        job_id: str = "",
    ) -> None:
        ...

    def send_message_background(
        self,
        text: str,
        *,
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        ...

    def send_document_background(
        self,
        *,
        file_path: str,
        caption: str = "",
        filename: str = "",
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
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
        "nvidia": (0.0, 0.0),
        "nvidia_vlm": (0.0, 0.0),
        "gemini_vlm": (0.0001, 0.0004),
        "groq_vlm": (0.00011, 0.00034),
        "openai_vlm": (0.0004, 0.0016),
        "qwen_vlm": (0.00011, 0.00034),
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
        vlm_evaluator: Optional[Any] = None,  # Phase 26: VisualQualityEvaluator (optional)
        memory_store: Optional[Any] = None,  # Phase 2: TopicMemoryStore
        visual_sidecar: Optional[Any] = None,
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
        self.vlm_evaluator = vlm_evaluator  # Phase 26
        self.memory_store = memory_store  # Phase 2
        self.visual_sidecar = visual_sidecar
        self._channel_publishers: Dict[str, PublisherLike] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._image_output_dir = "data/images"
        self._summary_card_enabled = self._read_summary_card_enabled()
        self._summary_card_max_bullets = self._read_summary_card_max_bullets()
        self._market_chart_enabled = self._read_market_chart_enabled()
        if self.image_generator is not None:
            output_dir = getattr(self.image_generator, "output_dir", None)
            if not output_dir:
                client = getattr(self.image_generator, "client", None)
                output_dir = getattr(client, "output_dir", None)
            if output_dir:
                self._image_output_dir = str(output_dir)
        if self.visual_sidecar is None:
            self.visual_sidecar = build_visual_sidecar_from_env(
                output_dir=self._image_output_dir,
                job_store=self.job_store,
            )

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

        async def _execute() -> None:
            # 준비된 초안이 있으면 즉시 발행 단계로 진입한다.
            if job.prepared_payload:
                await self._publish_payload(job, job.prepared_payload)
                return

            prepared_payload = await self._build_publish_payload(job)
            if not prepared_payload:
                return
            if self._should_request_draft_approval(job=job, payload=prepared_payload):
                self._cache_payload_for_draft_approval(job, prepared_payload)
                return
            await self._publish_payload(job, prepared_payload)

        try:
            await self._run_with_job_heartbeat(
                job=job,
                stage="run_job",
                work=_execute(),
            )
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
            # 실패 종료 케이스에서도 품질 스냅샷을 남겨 원인 분석 가능성을 높인다.
            update_snapshot = getattr(self.job_store, "update_quality_snapshot", None)
            if callable(update_snapshot):
                failure_snapshot = dict(content_result.get("quality_snapshot", {}) or {})
                failure_snapshot["pipeline_failure"] = {
                    "source": "llm_quality_gate",
                    "quality_outcome": quality_outcome,
                    "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                try:
                    update_snapshot(job_id, failure_snapshot)
                except Exception:
                    logger.debug("quality snapshot update skipped", exc_info=True)

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
                if not self.job_store.check_llm_budget(job_id):
                    logger.warning(
                        "Quality self-retry skipped: LLM budget exceeded",
                        extra={"job_id": job_id},
                    )
                    self._record_failure_metrics("BUDGET_EXCEEDED")
                    self.job_store.fail_job(job_id, "BUDGET_EXCEEDED", "LLM 호출 상한 초과 (quality retry)")
                    return None
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

        seo_data = content_result.get("seo_snapshot", {})
        topic_mode = str(seo_data.get("topic_mode", "")).strip().lower()

        # semi_auto 모드에서는 이미지 자동 생성을 건너뛰고 텔레그램 수집 대기로 전환한다.
        # topic_mode 를 전달하면 토픽별 분리(방안 A) 또는 전역 semi_auto 로 판단한다.
        if is_semi_auto_mode(self.job_store, topic_mode):
            notifier_ready = bool(self.notifier and getattr(self.notifier, "enabled", False))
            raw_slots = content_result.get("image_slots")
            image_slots = raw_slots if isinstance(raw_slots, list) else []
            if notifier_ready and image_slots:
                text_only_payload = await self._build_text_only_payload(job, content_result)
                saved = self.job_store.save_prepared_payload(
                    job.job_id,
                    text_only_payload,
                    mark_ready=False,
                )
                if not saved:
                    self._record_failure_metrics("PIPELINE_ERROR")
                    self._fail_with_retry_policy(
                        job=job,
                        error_code="PIPELINE_ERROR",
                        error_message="semi_auto payload 저장 실패",
                    )
                    return None

                collector = TelegramImageCollector(
                    job_store=self.job_store,
                    notifier=self.notifier,  # type: ignore[arg-type]
                    image_output_dir=self._image_output_dir,
                )
                collector.init_slots(job.job_id, image_slots)
                self.job_store.update_job_status(job.job_id, self.job_store.STATUS_AWAITING_IMAGES)
                await collector.send_next_prompt(job.job_id, job.title)
                logger.info(
                    "[Pipeline] %s: semi_auto mode -> awaiting_images",
                    job.job_id,
                )
                return None

            if not notifier_ready:
                logger.warning(
                    "Semi-auto requested but notifier unavailable, fallback to auto image generation",
                    extra={"job_id": job_id},
                )
            elif not image_slots:
                logger.warning(
                    "Semi-auto requested but image_slots missing, fallback to auto image generation",
                    extra={"job_id": job_id},
                )

        # 이미지 생성은 실패해도 본문 발행을 계속 진행한다.
        thumbnail_path: Optional[str] = None
        content_image_paths: list[str] = []
        image_sources: dict[str, dict[str, str]] = {}
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

        tags: list[str] = self._public_publish_tags(job.tags)
        category: str = job.category
        topic_mode = str(seo_data.get("topic_mode", "")).strip()

        if not category and topic_mode:
            category = get_category_for_topic(topic_mode, job.platform)
            logger.info(
                "Category auto-assigned from topic",
                extra={"topic_mode": topic_mode, "category": category},
            )

        tags = await self._build_publish_tags(
            job=job,
            content_result=content_result,
            topic_mode=topic_mode,
            existing_tags=tags,
        )

        raw_content = content_result.get("final_content", "")
        image_concepts = content_result.get("image_placements", [])
        concept_list = [p.get("concept", "") for p in image_concepts if isinstance(p, dict)]

        raw_content = self._inject_markdown_images(
            content=raw_content,
            thumbnail_path=thumbnail_path,
            content_image_paths=content_image_paths,
        )

        quality_snapshot = content_result.get("quality_snapshot", {})
        visual_style = self._visual_style_for_job(job)

        # 마크다운 표 → PNG 렌더링 (Pillow 없으면 무시)
        try:
            raw_content, table_image_paths, table_validation = extract_and_render_tables_with_validation(
                content=raw_content,
                output_dir=self._image_output_dir,
                style=visual_style,
            )
            if table_image_paths or table_validation.issues:
                quality_snapshot = self._with_visual_text_validation(
                    quality_snapshot,
                    area="tables",
                    passed=table_validation.passed,
                    issues=table_validation.issues,
                    meta={"count": len(table_image_paths), "style": visual_style},
                )
            if table_image_paths:
                logger.info(
                    "Table images rendered",
                    extra={"job_id": job_id, "table_count": len(table_image_paths)},
                )
        except Exception as exc:
            logger.warning(
                "Table rendering skipped",
                extra={"job_id": job_id, "error": str(exc)[:200]},
            )
            table_image_paths = []
            quality_snapshot = self._with_visual_text_validation(
                quality_snapshot,
                area="tables",
                passed=False,
                issues=["table_render_exception"],
                meta={"error": str(exc)[:200], "style": visual_style},
            )

        if thumbnail_path or content_image_paths or table_image_paths:
            processed_content, image_points = create_naver_editor_content(
                content=raw_content,
                thumbnail_path=thumbnail_path,
                content_image_paths=content_image_paths,
                image_concepts=concept_list,
                table_image_paths=table_image_paths,
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
            "quality_snapshot": quality_snapshot,
            "seo_snapshot": content_result.get("seo_snapshot", {}),
            "llm_token_usage": content_result.get("llm_token_usage", {}),
            "publish_mode": self._requested_publish_mode(job, {}),
        }

    async def _build_text_only_payload(
        self,
        job: Job,
        content_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """semi_auto용 텍스트 페이로드를 생성한다."""
        seo_data = content_result.get("seo_snapshot", {})
        topic_mode = str(seo_data.get("topic_mode", "")).strip()

        tags: list[str] = self._public_publish_tags(job.tags)
        category: str = job.category
        if not category and topic_mode:
            category = get_category_for_topic(topic_mode, job.platform)
            logger.info(
                "Category auto-assigned from topic",
                extra={"topic_mode": topic_mode, "category": category},
            )

        tags = await self._build_publish_tags(
            job=job,
            content_result=content_result,
            topic_mode=topic_mode,
            existing_tags=tags,
        )

        raw_content = str(content_result.get("final_content", "")).strip()
        processed_content = convert_markdown_for_naver_editor(raw_content)
        return {
            "title": job.title,
            "content": processed_content,
            "thumbnail": "",
            "images": [],
            "image_sources": {},
            "image_points": [],
            "tags": tags,
            "category": category,
            "quality_snapshot": content_result.get("quality_snapshot", {}),
            "seo_snapshot": content_result.get("seo_snapshot", {}),
            "llm_token_usage": content_result.get("llm_token_usage", {}),
            "publish_mode": self._requested_publish_mode(job, {}),
        }

    async def _publish_payload(self, job: Job, payload: Dict[str, Any]) -> None:
        """준비된 payload를 실제 블로그에 발행한다."""
        job_id = job.job_id
        payload = self._normalize_naver_payload_for_publish(job=job, payload=payload)
        payload = self._ensure_summary_card_payload(job=job, payload=payload)
        payload = self._ensure_market_chart_payload(job=job, payload=payload)
        payload = await self._ensure_visual_sidecar_payload(job=job, payload=payload)
        publish_mode = self._requested_publish_mode(job, payload)
        if publish_mode == "publish":
            blockers = self._auto_publish_blockers(job=job, payload=payload)
            if blockers:
                blocked_payload = self._mark_auto_publish_guard(
                    payload=payload,
                    status="blocked",
                    reasons=blockers,
                )
                self._notify_auto_publish_withheld(job=job, reasons=blockers)
                self._cache_payload_for_draft_approval(job, blocked_payload)
                return
            payload = self._mark_auto_publish_guard(
                payload=payload,
                status="passed",
                reasons=[],
            )
        elif publish_mode == "draft":
            payload = dict(payload)
            payload["publish_mode"] = "draft"
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
                    normalized_meta = {
                        "kind": str(meta.get("kind", "unknown")).strip().lower() or "unknown",
                        "provider": str(meta.get("provider", "unknown")).strip().lower() or "unknown",
                    }
                    renderer = str(meta.get("renderer", "")).strip().lower()
                    if renderer:
                        normalized_meta["renderer"] = renderer
                    image_sources[normalized_path] = normalized_meta
                else:
                    image_sources[normalized_path] = {"kind": "unknown", "provider": "unknown"}
        raw_tags = payload.get("tags", [])
        tags = self._normalize_public_tags([str(tag) for tag in raw_tags]) if isinstance(raw_tags, list) else []

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
            publish_mode=str(payload.get("publish_mode", "") or "") or None,
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
            self._archive_completed_post_text(
                job=job,
                payload=payload,
                result_url=str(result.url or ""),
            )
            self._notify_saved_draft_review_link(
                job=job,
                payload=payload,
                result_url=str(result.url or ""),
            )
            self._record_model_performance(job=job, payload=payload, post_id=result.url)
            # ── 메모리 저장 (발행 성공 시, non-critical) ──
            self._record_topic_memory(job=job, payload=payload, result_url=result.url)
            mark_consumed = getattr(self.job_store, "mark_idea_vault_consumed_by_job", None)
            if mark_consumed and callable(mark_consumed):
                try:
                    mark_consumed(job_id)
                except Exception:
                    logger.debug("idea_vault consumed mark skipped", extra={"job_id": job_id})
            if self.vlm_evaluator is not None and str(result.url or "").strip():
                should_run_vlm, skip_reason = self._should_run_vlm_evaluation(
                    job_id=job_id,
                    payload=payload,
                )
                if should_run_vlm:
                    self._spawn_background_task(
                        self._run_vlm_evaluation(
                            job_id=job_id,
                            post_url=str(result.url),
                            title=str(payload.get("title", job.title)),
                        ),
                        name=f"vlm-visual-eval:{job_id}",
                    )
                else:
                    logger.info(
                        "VLM evaluation skipped",
                        extra={"job_id": job_id, "reason": skip_reason},
                    )
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

    def _normalize_naver_payload_for_publish(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """발행 직전 네이버 에디터가 이해할 수 있는 본문 형태로 보정한다.

        선생성 payload나 텔레그램 수정본에는 원문 Markdown이 다시 들어올 수 있다.
        이 단계에서 최종 보정을 한 번 더 수행해 네이버 임시저장 화면에 `##`, `|---|`
        같은 문법이 그대로 노출되는 일을 막는다.
        """
        if str(getattr(job, "platform", "") or "").strip().lower() != "naver":
            return payload

        content = str(payload.get("content", "") or "")
        if not content.strip() or not self._payload_needs_naver_normalization(content):
            return payload

        normalized = dict(payload)
        raw_image_points = normalized.get("image_points", [])
        existing_points = list(raw_image_points) if isinstance(raw_image_points, list) else []
        has_existing_image_markers = bool(re.search(r"\[IMG_\d+\]", content))
        has_existing_image_points = bool(existing_points)

        content_for_conversion = content
        table_image_paths: list[str] = []
        if self._contains_markdown_table(content):
            visual_style = self._visual_style_for_job(job)
            try:
                content_for_conversion, table_image_paths, table_validation = extract_and_render_tables_with_validation(
                    content=content,
                    output_dir=self._image_output_dir,
                    style=visual_style,
                )
                if table_image_paths or table_validation.issues:
                    normalized["quality_snapshot"] = self._with_visual_text_validation(
                        normalized.get("quality_snapshot", {}),
                        area="tables",
                        passed=table_validation.passed,
                        issues=table_validation.issues,
                        meta={"count": len(table_image_paths), "style": visual_style},
                    )
            except Exception as exc:
                logger.warning(
                    "Final table rendering skipped",
                    extra={"job_id": job.job_id, "error": str(exc)[:200]},
                )
                content_for_conversion = content
                table_image_paths = []
                normalized["quality_snapshot"] = self._with_visual_text_validation(
                    normalized.get("quality_snapshot", {}),
                    area="tables",
                    passed=False,
                    issues=["table_render_exception"],
                    meta={"error": str(exc)[:200], "style": visual_style},
                )

        if table_image_paths and not has_existing_image_points and not has_existing_image_markers:
            raw_images = normalized.get("images", [])
            content_image_paths = [str(path) for path in raw_images] if isinstance(raw_images, list) else []
            processed_content, image_points = create_naver_editor_content(
                content=content_for_conversion,
                thumbnail_path=str(normalized.get("thumbnail", "") or "") or None,
                content_image_paths=[path for path in content_image_paths if path],
                image_concepts=[],
                table_image_paths=table_image_paths,
            )
            normalized["content"] = processed_content
            normalized["image_points"] = [asdict(point) for point in image_points]
            image_sources = self._normalize_payload_image_sources(normalized.get("image_sources", {}))
            for table_path in table_image_paths:
                image_sources[str(table_path)] = {"kind": "manual", "provider": "table_renderer"}
            normalized["image_sources"] = image_sources
            logger.info(
                "Prepared payload normalized with table images",
                extra={
                    "job_id": job.job_id,
                    "table_count": len(table_image_paths),
                    "image_count": len(image_points),
                },
            )
            return normalized

        processed_content = convert_markdown_for_naver_editor(content_for_conversion)
        image_sources = self._normalize_payload_image_sources(normalized.get("image_sources", {}))
        if table_image_paths:
            next_index = self._next_image_marker_index(processed_content, existing_points)
            for table_idx, table_path in enumerate(table_image_paths):
                table_marker = f"[TABLE_{table_idx}]"
                if table_marker not in processed_content:
                    continue
                image_marker = f"[IMG_{next_index}]"
                processed_content = processed_content.replace(table_marker, image_marker, 1)
                existing_points.append(
                    {
                        "index": next_index,
                        "path": str(table_path),
                        "marker": image_marker,
                        "section_hint": f"표 {table_idx + 1}",
                        "is_thumbnail": False,
                    }
                )
                image_sources[str(table_path)] = {"kind": "manual", "provider": "table_renderer"}
                next_index += 1
            processed_content = re.sub(r"\[TABLE_\d+\]", "", processed_content)

        normalized["content"] = processed_content.strip()
        normalized["image_points"] = existing_points
        normalized["image_sources"] = image_sources
        logger.info(
            "Prepared payload markdown normalized",
            extra={
                "job_id": job.job_id,
                "table_count": len(table_image_paths),
                "content_length": len(str(normalized.get("content", ""))),
            },
        )
        return normalized

    def _ensure_summary_card_payload(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """요약 카드 이미지를 생성해 본문 상단에 삽입한다."""
        if not self._summary_card_enabled:
            return payload
        if str(getattr(job, "platform", "") or "").strip().lower() != "naver":
            return payload
        if self._payload_has_summary_card(payload):
            return payload

        content = str(payload.get("content", "") or "").strip()
        if len(content) < 350:
            return payload

        result = render_summary_card(
            title=str(payload.get("title", job.title) or job.title),
            content=content,
            output_dir=self._image_output_dir,
            max_bullets=self._summary_card_max_bullets,
            style=self._visual_style_for_job(job),
        )
        if result is None:
            return payload

        normalized = dict(payload)
        raw_points = normalized.get("image_points", [])
        image_points = list(raw_points) if isinstance(raw_points, list) else []
        marker_index = self._next_image_marker_index(content, image_points)
        marker = f"[IMG_{marker_index}]"

        normalized["content"] = self._insert_summary_card_marker(content, marker)
        image_points.append(
            {
                "index": marker_index,
                "path": result.path,
                "marker": marker,
                "section_hint": "요약 카드",
                "is_thumbnail": False,
            }
        )
        normalized["image_points"] = image_points

        image_sources = self._normalize_payload_image_sources(normalized.get("image_sources", {}))
        image_sources[result.path] = {"kind": "manual", "provider": "summary_card_renderer"}
        normalized["image_sources"] = image_sources
        normalized["quality_snapshot"] = self._with_visual_text_validation(
            normalized.get("quality_snapshot", {}),
            area="summary_card",
            passed=True,
            issues=[],
            meta={
                "bullet_count": len(result.bullets),
                "style": self._visual_style_for_job(job),
            },
        )

        logger.info(
            "Summary card attached",
            extra={
                "job_id": job.job_id,
                "path": result.path,
                "bullet_count": len(result.bullets),
            },
        )
        return normalized

    def _ensure_market_chart_payload(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """시장 스냅샷이 있으면 블로그용 그래프/지표 보드 이미지를 삽입한다."""
        if not self._market_chart_enabled:
            return payload
        if str(getattr(job, "platform", "") or "").strip().lower() != "naver":
            return payload
        if self._payload_has_market_chart(payload):
            return payload

        seo_snapshot = payload.get("seo_snapshot", {})
        if not isinstance(seo_snapshot, dict):
            return payload
        market_snapshot = seo_snapshot.get("market_snapshot", {})
        if not isinstance(market_snapshot, dict):
            return payload
        raw_points = market_snapshot.get("data_points", [])
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            return payload

        content = str(payload.get("content", "") or "").strip()
        if not content:
            return payload

        result = render_market_chart(
            market_snapshot=market_snapshot,
            title=str(payload.get("title", job.title) or job.title),
            output_dir=self._image_output_dir,
        )
        if result is None:
            return payload

        normalized = dict(payload)
        raw_points_payload = normalized.get("image_points", [])
        image_points = list(raw_points_payload) if isinstance(raw_points_payload, list) else []
        marker_index = self._next_image_marker_index(content, image_points)
        marker = f"[IMG_{marker_index}]"

        normalized["content"] = self._insert_summary_card_marker(content, marker)
        image_points.append(
            {
                "index": marker_index,
                "path": result.path,
                "marker": marker,
                "section_hint": "시장 그래프",
                "is_thumbnail": False,
            }
        )
        normalized["image_points"] = image_points

        image_sources = self._normalize_payload_image_sources(normalized.get("image_sources", {}))
        image_sources[result.path] = {"kind": "manual", "provider": "market_chart_renderer"}
        normalized["image_sources"] = image_sources

        logger.info(
            "Market chart attached",
            extra={
                "job_id": job.job_id,
                "path": result.path,
                "mode": result.mode,
                "point_count": result.point_count,
            },
        )
        return normalized

    @staticmethod
    def _payload_has_summary_card(payload: Dict[str, Any]) -> bool:
        """이미 요약 카드가 포함된 payload인지 확인한다."""
        raw_sources = payload.get("image_sources", {})
        if isinstance(raw_sources, dict):
            for meta in raw_sources.values():
                if isinstance(meta, dict) and str(meta.get("provider", "")).strip().lower() == "summary_card_renderer":
                    return True

        raw_points = payload.get("image_points", [])
        if isinstance(raw_points, list):
            for point in raw_points:
                if isinstance(point, dict) and "요약 카드" in str(point.get("section_hint", "")):
                    return True
        return False

    @staticmethod
    def _payload_has_market_chart(payload: Dict[str, Any]) -> bool:
        """이미 시장 그래프가 포함된 payload인지 확인한다."""
        raw_sources = payload.get("image_sources", {})
        if isinstance(raw_sources, dict):
            for meta in raw_sources.values():
                if isinstance(meta, dict) and str(meta.get("provider", "")).strip().lower() == "market_chart_renderer":
                    return True

        raw_points = payload.get("image_points", [])
        if isinstance(raw_points, list):
            for point in raw_points:
                if isinstance(point, dict) and "시장 그래프" in str(point.get("section_hint", "")):
                    return True
        return False

    @staticmethod
    def _insert_summary_card_marker(content: str, marker: str) -> str:
        """요약 카드 마커를 도입부 뒤, 첫 본문 섹션 앞에 둔다."""
        text = str(content or "").strip()
        if not text:
            return marker

        section_match = re.search(r"(?m)^■\s+.+$", text)
        if section_match and section_match.start() > 0:
            pos = section_match.start()
            return f"{text[:pos].rstrip()}\n\n{marker}\n\n{text[pos:].lstrip()}"

        first_blank = text.find("\n\n")
        if first_blank > 0:
            pos = first_blank + 2
            return f"{text[:pos].rstrip()}\n\n{marker}\n\n{text[pos:].lstrip()}"

        return f"{marker}\n\n{text}"

    @staticmethod
    def _payload_needs_naver_normalization(content: str) -> bool:
        """발행 직전 Markdown 보정이 필요한지 빠르게 판별한다."""
        return bool(
            re.search(r"(?m)^\s{0,3}#{1,6}\s+\S+", content)
            or re.search(r"(?m)^\s*\|.+\|\s*$", content)
            or re.search(r"\*\*[^*]+\*\*", content)
            or re.search(r"!\[[^\]]*\]\([^)]+\)", content)
        )

    @staticmethod
    def _contains_markdown_table(content: str) -> bool:
        """본문에 표 구분선이 있는 정식 마크다운 표가 포함됐는지 확인한다."""
        lines = content.splitlines()
        for index in range(len(lines) - 1):
            if not re.match(r"^\s*\|.+\|\s*$", lines[index]):
                continue
            if re.match(r"^\s*\|[\s:|\-]+\|\s*$", lines[index + 1]):
                return True
        return False

    @staticmethod
    def _next_image_marker_index(content: str, image_points: list[Any]) -> int:
        """기존 이미지 마커와 image_points를 기준으로 다음 인덱스를 계산한다."""
        indices: list[int] = []
        for match in re.finditer(r"\[IMG_(\d+)\]", content):
            try:
                indices.append(int(match.group(1)))
            except Exception:
                continue
        for point in image_points:
            if isinstance(point, dict) and "index" in point:
                try:
                    indices.append(int(point["index"]))
                except Exception:
                    continue
        return max(indices, default=-1) + 1

    @staticmethod
    def _normalize_payload_image_sources(raw_sources: Any) -> dict[str, dict[str, str]]:
        """payload의 이미지 소스 메타데이터를 dict[str, dict] 형태로 맞춘다."""
        image_sources: dict[str, dict[str, str]] = {}
        if not isinstance(raw_sources, dict):
            return image_sources
        for path, meta in raw_sources.items():
            normalized_path = str(path or "").strip()
            if not normalized_path:
                continue
            if isinstance(meta, dict):
                normalized_meta = {
                    "kind": str(meta.get("kind", "unknown")).strip().lower() or "unknown",
                    "provider": str(meta.get("provider", "unknown")).strip().lower() or "unknown",
                }
                renderer = str(meta.get("renderer", "")).strip().lower()
                if renderer:
                    normalized_meta["renderer"] = renderer
                image_sources[normalized_path] = normalized_meta
            else:
                image_sources[normalized_path] = {"kind": "unknown", "provider": "unknown"}
        return image_sources

    async def _ensure_visual_sidecar_payload(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """FreeLLMAPI 시각자료 사이드카를 선택적으로 적용한다."""
        if self.visual_sidecar is None:
            return payload
        try:
            enriched = await self.visual_sidecar.enrich_payload(job=job, payload=payload)
        except Exception as exc:
            logger.info(
                "Visual sidecar skipped",
                extra={"job_id": job.job_id, "error": str(exc)[:200]},
            )
            return payload
        if not isinstance(enriched, dict):
            return payload
        return enriched

    @staticmethod
    def _read_summary_card_enabled() -> bool:
        """요약 카드 자동 생성 사용 여부를 환경변수에서 읽는다."""
        raw = (
            os.getenv("NAVER_SUMMARY_CARD_ENABLED")
            or os.getenv("SUMMARY_CARD_ENABLED")
            or "true"
        )
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _read_summary_card_max_bullets() -> int:
        """요약 카드 bullet 개수 상한을 읽는다."""
        raw = os.getenv("SUMMARY_CARD_MAX_BULLETS", "4")
        try:
            return max(2, min(5, int(str(raw).strip())))
        except Exception:
            return 4

    @staticmethod
    def _read_market_chart_enabled() -> bool:
        """시장 그래프 자동 생성 사용 여부를 환경변수에서 읽는다."""
        raw = (
            os.getenv("NAVER_MARKET_CHART_ENABLED")
            or os.getenv("MARKET_CHART_ENABLED")
            or "true"
        )
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

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

    async def _build_publish_tags(
        self,
        *,
        job: Job,
        content_result: Dict[str, Any],
        topic_mode: str,
        existing_tags: list[str] | None = None,
    ) -> list[str]:
        """네이버에 실제 입력할 공개 태그를 만든다."""

        tags = self._public_publish_tags(existing_tags if existing_tags is not None else job.tags)
        if self._is_kr_preopen_auto_publish_job(job):
            generated = []
            if self.tag_generator:
                try:
                    tag_result = await self.tag_generator.generate(
                        title=job.title,
                        seed_keywords=job.seed_keywords,
                        platform=job.platform,
                        topic_mode=topic_mode,
                        content_summary=str(content_result.get("final_content", ""))[:300],
                    )
                    generated = list(tag_result.tags)
                except Exception:
                    logger.warning("KR preopen tag generation failed", extra={"job_id": job.job_id})
            tags = self._normalize_public_tags([*tags, *generated])
            if len(tags) < 5:
                tags = self._normalize_public_tags([*tags, *self._fallback_kr_preopen_tags(job)])
            return tags[:8]

        if self.tag_generator and not tags:
            try:
                tag_result = await self.tag_generator.generate(
                    title=job.title,
                    seed_keywords=job.seed_keywords,
                    platform=job.platform,
                    topic_mode=topic_mode,
                    content_summary=str(content_result.get("final_content", ""))[:300],
                )
                tags = self._normalize_public_tags(tag_result.tags)
                logger.info(
                    "Tags generated",
                    extra={
                        "job_id": job.job_id,
                        "platform": job.platform,
                        "tag_count": len(tags),
                        "fallback": tag_result.fallback_used,
                    },
                )
            except Exception:
                logger.warning("Tag generation failed, proceeding without tags", extra={"job_id": job.job_id})
        return tags

    def _public_publish_tags(self, raw_tags: list[str] | tuple[str, ...] | None) -> list[str]:
        """내부 운영 태그를 제거하고 네이버 입력용 태그만 반환한다."""

        if not raw_tags:
            return []
        return self._normalize_public_tags(
            [tag for tag in raw_tags if not self._is_internal_job_tag(str(tag))]
        )

    def _normalize_public_tags(self, raw_tags: list[str] | tuple[str, ...]) -> list[str]:
        """네이버 태그 입력 전에 중복/공백/길이를 정리한다."""

        tags: list[str] = []
        seen: set[str] = set()
        for raw in raw_tags:
            tag = str(raw or "").strip().lstrip("#")
            if not tag or self._is_internal_job_tag(tag):
                continue
            tag = re.sub(r"\s+", "", tag)
            tag = re.sub(r"[^0-9A-Za-z가-힣_./+-]", "", tag)
            if not tag or len(tag) < 2:
                continue
            tag = tag[:30]
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            tags.append(tag)
        return tags

    def _fallback_kr_preopen_tags(self, job: Job) -> list[str]:
        """LLM 없이 국장전 글에 넣을 5~8개 태그 후보를 만든다."""

        base = [
            *list(job.seed_keywords or [])[:5],
            "국장전브리핑",
            "오늘의증시",
            "경제공부",
            "투자공부",
            "시장체크",
            "초심자투자",
        ]
        return self._normalize_public_tags(base)

    def _is_internal_job_tag(self, tag: str) -> bool:
        """발행 메타용 태그인지 판단한다."""

        normalized = str(tag or "").strip().lower()
        if not normalized:
            return True
        exact = {
            "market_daily",
            "not_market_daily",
            "idea_vault",
            "macro_intelligence",
            "category_expansion",
        }
        prefixes = (
            "daily_slot:",
            "market_slot:",
            "market_origin_slot:",
            "market_scope:",
            "market_extra:",
            "local_date:",
            "auto_publish:",
            "publish_mode:",
            "opportunity_",
            "category_topic:",
            "category_template:",
            "category_score:",
            "category_ramp_week:",
            "category_safety:",
            "creator_source:",
            "approval_required:",
            "writing_strategy:",
            "writing_intent:",
            "writing_axis:",
            "macro_document:",
            "macro_candidate:",
        )
        return normalized in exact or normalized.startswith(prefixes)

    def _is_kr_preopen_auto_publish_job(self, job: Job) -> bool:
        """국장전 자동 공개발행 대상인지 확인한다."""

        tags = [str(tag or "").strip().lower() for tag in (job.tags or [])]
        return "auto_publish:kr_preopen" in tags and "market_slot:kr_preopen" in tags

    def _is_category_expansion_job(self, job: Job) -> bool:
        """확장 카테고리 글은 승인 대기 흐름으로 보낸다."""

        tags = [str(tag or "").strip().lower() for tag in (job.tags or [])]
        return "category_expansion" in tags or "approval_required:category_expansion" in tags

    def _tag_value(self, job: Job, prefix: str) -> str:
        normalized_prefix = str(prefix or "").strip().lower()
        for tag in job.tags or []:
            raw = str(tag or "").strip()
            if raw.lower().startswith(normalized_prefix):
                return raw[len(prefix) :].strip()
        return ""

    def _requested_publish_mode(self, job: Job, payload: Dict[str, Any]) -> str:
        raw = str(payload.get("publish_mode", "") or "").strip().lower()
        if not raw:
            raw = self._tag_value(job, "publish_mode:").lower()
        return raw if raw in {"publish", "draft"} else ""

    def _opportunity_score_for_job(self, job: Job, payload: Dict[str, Any]) -> float:
        quality = payload.get("quality_snapshot", {})
        if isinstance(quality, dict):
            raw_score = quality.get("opportunity_score")
            try:
                if raw_score is not None:
                    return float(raw_score)
            except (TypeError, ValueError):
                pass
        raw_tag = self._tag_value(job, "opportunity_score:")
        try:
            return float(raw_tag)
        except (TypeError, ValueError):
            return 0.0

    def _auto_publish_blockers(self, *, job: Job, payload: Dict[str, Any]) -> list[str]:
        """국장전 자동 공개발행 조건 미달 사유를 반환한다."""

        blockers: list[str] = []
        if not self._is_kr_preopen_auto_publish_job(job):
            blockers.append("국장전 자동발행 대상 태그가 아닙니다.")

        opportunity_score = self._opportunity_score_for_job(job, payload)
        if opportunity_score < 80.0:
            blockers.append(f"글감 기회 점수가 낮습니다. ({opportunity_score:.0f}/80)")

        seo_snapshot = payload.get("seo_snapshot", {})
        market_snapshot = seo_snapshot.get("market_snapshot", {}) if isinstance(seo_snapshot, dict) else {}
        confidence = 0.0
        data_point_count = 0
        if isinstance(market_snapshot, dict):
            try:
                confidence = float(market_snapshot.get("confidence_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            try:
                data_point_count = int(market_snapshot.get("data_point_count", 0) or 0)
            except (TypeError, ValueError):
                data_point_count = 0
        if confidence < 0.55:
            blockers.append(f"시장 데이터 신뢰도가 낮습니다. ({confidence:.2f}/0.55)")
        if data_point_count <= 0:
            blockers.append("수치/시장 근거 데이터가 없습니다.")

        tags = self._normalize_public_tags(payload.get("tags", []) if isinstance(payload.get("tags"), list) else [])
        if not (5 <= len(tags) <= 8):
            blockers.append(f"추천태그 개수가 5~8개가 아닙니다. ({len(tags)}개)")

        investment_text = f"{payload.get('title', job.title)}\n{payload.get('content', '')}"
        forbidden = self._investment_forbidden_hits(investment_text)
        if forbidden:
            blockers.append(f"투자 권유 위험 표현이 있습니다: {', '.join(forbidden[:3])}")

        quality = payload.get("quality_snapshot", {})
        visual_status = ""
        if isinstance(quality, dict):
            seo_source_count = None
            try:
                raw_source_count = seo_snapshot.get("source_context_count") if isinstance(seo_snapshot, dict) else None
                if raw_source_count is not None:
                    seo_source_count = int(raw_source_count)
            except (TypeError, ValueError):
                seo_source_count = None
            if seo_source_count is not None and seo_source_count <= 1:
                blockers.append("단일 뉴스/자료 의존도가 높아 자동 공개발행을 보류합니다.")

            visual = quality.get("visual_text_validation", {})
            if not isinstance(visual, dict):
                visual = quality.get("visual_text_sanitizer", {})
            if isinstance(visual, dict):
                visual_status = str(visual.get("status", "") or "").strip().lower()
                visual_issues = visual.get("issues", [])
                visual_failed = visual.get("passed") is False
                if visual_failed or visual_status == "failed" or visual_issues:
                    blockers.append("표/카드 텍스트 검수 이슈가 있습니다.")

        return blockers

    def _mark_auto_publish_guard(
        self,
        *,
        payload: Dict[str, Any],
        status: str,
        reasons: list[str],
    ) -> Dict[str, Any]:
        """자동 공개발행 게이트 결과를 payload에 기록한다."""

        normalized = dict(payload)
        normalized["publish_mode"] = "publish" if status == "passed" else "draft"
        quality = dict(normalized.get("quality_snapshot", {}) or {})
        quality["auto_publish_guard"] = {
            "status": status,
            "reasons": list(reasons),
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        normalized["quality_snapshot"] = quality
        return normalized

    def _notify_auto_publish_withheld(self, *, job: Job, reasons: list[str]) -> None:
        """자동 공개발행 보류 사유를 텔레그램으로 알린다."""

        if not self.notifier or not getattr(self.notifier, "enabled", False):
            return
        send_background = getattr(self.notifier, "send_message_background", None)
        if not callable(send_background):
            return
        message = "\n".join(
            [
                "국장전 자동발행 보류",
                "",
                f"제목: {job.title}",
                f"job_id: {job.job_id}",
                "",
                "사유:",
                *[f"- {reason}" for reason in reasons[:6]],
                "",
                "초안을 승인 대기로 전환했습니다.",
            ]
        )
        try:
            send_background(message, disable_notification=False)
        except Exception:
            logger.debug("Auto publish withheld notification skipped", extra={"job_id": job.job_id})

    def _investment_forbidden_hits(self, text: str) -> list[str]:
        pattern_labels = (
            (r"매수\s*(하세요|추천|관점|타이밍|신호)", "매수 유도"),
            (r"매도\s*(하세요|추천|관점|타이밍|신호)", "매도 유도"),
            (r"지금\s*사야", "즉시 매매 유도"),
            (r"무조건\s*오", "무조건 상승 단정"),
            (r"수익\s*보장", "수익 보장"),
            (r"목표가\s*확정", "목표가 확정"),
            (r"몰빵", "몰빵"),
            (r"급등주", "급등주"),
            (r"상한가\s*따라잡기", "상한가 따라잡기"),
            (r"추천\s*종목", "추천 종목"),
        )
        hits = []
        for pattern, label in pattern_labels:
            if re.search(pattern, str(text or ""), flags=re.IGNORECASE):
                hits.append(label)
        return hits

    def _visual_style_for_job(self, job: Job) -> str:
        """시장 글에는 시장노트형 시각 스타일을 적용한다."""

        tags = [str(tag or "").strip().lower() for tag in (job.tags or [])]
        if "market_daily" in tags or any(tag.startswith("market_slot:") for tag in tags):
            return "market_note"
        return "default"

    def _with_visual_text_validation(
        self,
        quality_snapshot: Any,
        *,
        area: str,
        passed: bool,
        issues: list[str] | tuple[str, ...] | None = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """표/카드 텍스트 검수 결과를 quality_snapshot에 누적한다."""

        quality = dict(quality_snapshot or {}) if isinstance(quality_snapshot, dict) else {}
        visual = dict(quality.get("visual_text_validation") or {})
        areas = dict(visual.get("areas") or {})
        normalized_issues = [str(issue) for issue in (issues or []) if str(issue).strip()]
        area_payload: Dict[str, Any] = {
            "passed": bool(passed),
            "issues": normalized_issues,
        }
        if meta:
            area_payload.update(meta)
        areas[str(area)] = area_payload

        aggregate_issues: list[str] = []
        aggregate_passed = True
        for area_name, area_value in areas.items():
            if not isinstance(area_value, dict):
                continue
            area_passed = bool(area_value.get("passed", True))
            aggregate_passed = aggregate_passed and area_passed
            for issue in area_value.get("issues", []) or []:
                aggregate_issues.append(f"{area_name}:{issue}")

        visual["passed"] = aggregate_passed
        visual["issues"] = aggregate_issues
        visual["areas"] = areas
        quality["visual_text_validation"] = visual
        return quality

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
        publish_mode: Optional[str] = None,
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
                        publish_mode=publish_mode,
                    )
                except TypeError as exc:
                    if "publish_mode" not in str(exc):
                        raise
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

    def _spawn_background_task(self, coro: Any, *, name: str) -> None:
        """백그라운드 태스크를 추적하고 예외를 로깅한다."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop for background task: %s", name)
            return

        task = loop.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                logger.debug("Background task cancelled: %s", name)
            except Exception as exc:
                logger.warning("Background task failed (%s): %s", name, exc)

        task.add_done_callback(_on_done)

    def _should_request_draft_approval(
        self,
        *,
        job: Optional[Job] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """초안 승인 모드 활성 여부를 확인한다."""
        if job is not None and payload is not None:
            publish_mode = self._requested_publish_mode(job, payload)
            if publish_mode == "publish" and self._is_kr_preopen_auto_publish_job(job):
                return False
            if self._is_category_expansion_job(job):
                return True
        try:
            return is_draft_approval_enabled(self.job_store)
        except Exception:
            logger.debug("draft approval setting check skipped", exc_info=True)
            return False

    def _cache_payload_for_draft_approval(self, job: Job, payload: Dict[str, Any]) -> bool:
        """생성 payload를 승인 대기 상태로 보관하고 텔레그램 미리보기를 전송한다."""
        saved = self.job_store.save_prepared_payload(
            job.job_id,
            payload,
            mark_ready=False,
        )
        if not saved:
            self._record_failure_metrics("PIPELINE_ERROR")
            self._fail_with_retry_policy(
                job=job,
                error_code="PIPELINE_ERROR",
                error_message="승인 대기 초안 저장 실패",
            )
            return False

        awaiting_status = getattr(self.job_store, "STATUS_AWAITING_APPROVAL", STATUS_AWAITING_APPROVAL)
        status_updated = self.job_store.update_job_status(job.job_id, awaiting_status)
        if not status_updated:
            self._record_failure_metrics("PIPELINE_ERROR")
            self._fail_with_retry_policy(
                job=job,
                error_code="PIPELINE_ERROR",
                error_message="승인 대기 상태 전환 실패",
            )
            return False

        notified = self._notify_draft_approval_request(job=job, payload=payload)
        if not notified:
            logger.warning(
                "Draft approval pending but Telegram notification was not sent",
                extra={"job_id": job.job_id},
            )
        logger.info("Draft awaiting Telegram approval", extra={"job_id": job.job_id})
        return True

    def _write_draft_text_attachment(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
        expires_at: str,
    ) -> Optional[str]:
        """초안 전체 본문을 텔레그램 첨부용 TXT로 저장한다."""

        try:
            output_dir = Path("data/drafts")
            output_dir.mkdir(parents=True, exist_ok=True)
            file_path = output_dir / f"draft_{job.job_id}.txt"
            file_path.write_text(
                build_draft_text_attachment(
                    job_id=job.job_id,
                    title=str(payload.get("title", job.title) or job.title),
                    payload=payload,
                    expires_at=expires_at,
                ),
                encoding="utf-8",
            )
            return str(file_path)
        except Exception:
            logger.debug("Draft text attachment write failed", extra={"job_id": job.job_id}, exc_info=True)
            return None

    def _notify_draft_approval_request(self, *, job: Job, payload: Dict[str, Any]) -> bool:
        """텔레그램으로 초안 미리보기와 승인 버튼을 전송한다."""
        notifier = self.notifier
        if notifier is None or not getattr(notifier, "enabled", False):
            return False

        try:
            approval = create_draft_approval_request(
                self.job_store,
                job_id=job.job_id,
                title=job.title,
                ttl_hours=get_approval_ttl_hours(self.job_store),
            )
            quality = payload.get("quality_snapshot", {})
            guard = quality.get("auto_publish_guard", {}) if isinstance(quality, dict) else {}
            guard_reasons = guard.get("reasons", []) if isinstance(guard, dict) else []
            reason = ""
            if isinstance(guard_reasons, list) and guard_reasons:
                reason = f"자동발행 보류 - {str(guard_reasons[0])[:80]}"
            message = build_draft_compact_message(
                job_id=job.job_id,
                title=job.title,
                payload=payload,
                expires_at=approval.expires_at,
                reason=reason,
            )
            reply_markup = build_inline_keyboard(approval)
            attachment_path = self._write_draft_text_attachment(
                job=job,
                payload=payload,
                expires_at=approval.expires_at,
            )
        except Exception:
            logger.debug("Draft approval message build failed", extra={"job_id": job.job_id}, exc_info=True)
            return False

        send_document_background = getattr(notifier, "send_document_background", None)
        if callable(send_document_background) and attachment_path:
            try:
                send_document_background(
                    file_path=attachment_path,
                    caption=message,
                    filename=f"draft_{job.job_id}.txt",
                    disable_notification=False,
                    reply_markup=reply_markup,
                )
                return True
            except TypeError:
                logger.debug("Notifier document method signature mismatch", extra={"job_id": job.job_id})
            except Exception:
                logger.debug("Draft document notification failed", extra={"job_id": job.job_id}, exc_info=True)

        send_background = getattr(notifier, "send_message_background", None)
        if callable(send_background):
            try:
                send_background(
                    message,
                    disable_notification=False,
                    reply_markup=reply_markup,
                )
                return True
            except TypeError:
                # 레거시 notifier 호환: reply_markup 미지원 시 텍스트만 전송한다.
                send_background(message, disable_notification=False)
                return True

        send_message = getattr(notifier, "send_message", None)
        if callable(send_message):
            try:
                coro = send_message(
                    message,
                    disable_notification=False,
                    reply_markup=reply_markup,
                )
            except TypeError:
                coro = send_message(message, disable_notification=False)
            self._spawn_background_task(coro, name=f"draft-approval-notify:{job.job_id}")
            return True
        return False

    def _archive_completed_post_text(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
        result_url: str,
    ) -> bool:
        """완료된 글 본문을 가벼운 텍스트 아카이브로 저장한다."""
        archive_fn = getattr(self.job_store, "archive_post_text", None)
        if not callable(archive_fn):
            return False

        tags_raw = payload.get("tags", [])
        tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
        image_manifest = {
            "thumbnail": str(payload.get("thumbnail", "") or ""),
            "images": payload.get("images", []) if isinstance(payload.get("images", []), list) else [],
            "image_sources": payload.get("image_sources", {}) if isinstance(payload.get("image_sources", {}), dict) else {},
            "image_points": payload.get("image_points", []) if isinstance(payload.get("image_points", []), list) else [],
        }
        publish_mode = str(payload.get("publish_mode", "") or os.getenv("NAVER_PUBLISH_MODE", "publish")).strip().lower()
        source_type = "published_draft" if publish_mode == "draft" else "published"
        try:
            return bool(
                archive_fn(
                    job_id=job.job_id,
                    title=str(payload.get("title", job.title)),
                    final_content=str(payload.get("content", "")),
                    tags=tags,
                    category=str(payload.get("category", "") or ""),
                    source_type=source_type,
                    quality_snapshot=payload.get("quality_snapshot", {}),
                    result_url=result_url,
                    image_manifest=image_manifest,
                )
            )
        except Exception:
            logger.debug("Post text archive skipped", extra={"job_id": job.job_id}, exc_info=True)
            return False

    def _notify_saved_draft_review_link(
        self,
        *,
        job: Job,
        payload: Dict[str, Any],
        result_url: str,
    ) -> bool:
        """네이버 임시저장 확인 링크를 텔레그램으로 전송한다."""
        notifier = self.notifier
        if notifier is None or not getattr(notifier, "enabled", False):
            return False

        url = str(result_url or "").strip()
        if not url:
            return False

        publish_mode = str(payload.get("publish_mode", "") or os.getenv("NAVER_PUBLISH_MODE", "publish")).strip().lower()
        is_draft_mode = publish_mode == "draft"
        headline = "네이버 임시저장 완료" if is_draft_mode else "네이버 발행 완료"
        action_line = "스마트폰에서 최종 확인 후 네이버 화면에서 직접 발행해 주세요." if is_draft_mode else "스마트폰에서 게시 상태를 확인해 주세요."
        title = str(payload.get("title", job.title)).strip()
        content_len = len(str(payload.get("content", "") or ""))
        message = "\n".join(
            [
                headline,
                "",
                f"제목: {title}",
                f"job_id: {job.job_id}",
                f"본문 길이: {content_len}자",
                "",
                "확인 링크:",
                url,
                "",
                action_line,
            ]
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": "임시저장 열기" if is_draft_mode else "게시글 열기", "url": url}],
                [
                    {"text": "확인완료", "callback_data": f"ads:v1:c:{job.job_id}"},
                    {"text": "보류", "callback_data": f"ads:v1:h:{job.job_id}"},
                ],
            ]
        }

        send_background = getattr(notifier, "send_message_background", None)
        if callable(send_background):
            try:
                send_background(
                    message,
                    disable_notification=False,
                    reply_markup=reply_markup,
                )
                return True
            except TypeError:
                send_background(message, disable_notification=False)
                return True

        send_message = getattr(notifier, "send_message", None)
        if callable(send_message):
            try:
                coro = send_message(
                    message,
                    disable_notification=False,
                    reply_markup=reply_markup,
                )
            except TypeError:
                coro = send_message(message, disable_notification=False)
            self._spawn_background_task(coro, name=f"draft-link-notify:{job.job_id}")
            return True
        return False

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

            provider_breakdown = raw.get("by_provider", {})
            if isinstance(provider_breakdown, dict) and provider_breakdown:
                recorded_any = False
                for provider_name, provider_raw in provider_breakdown.items():
                    if not isinstance(provider_raw, dict):
                        continue
                    input_tokens = max(0, int(provider_raw.get("input_tokens", 0) or 0))
                    output_tokens = max(0, int(provider_raw.get("output_tokens", 0) or 0))
                    total_tokens = input_tokens + output_tokens
                    if total_tokens <= 0:
                        continue
                    call_count = max(0, int(provider_raw.get("calls", 0) or 0))
                    model = str(provider_raw.get("model", "")).strip()
                    provider = str(provider_name or "").strip().lower() or "unknown"
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
                            "source": "provider_breakdown",
                        },
                    )
                    recorded_any = True
                if recorded_any:
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
                    "source": "aggregate",
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

    def _resolve_slot_type(self, job: Job, payload: Optional[Dict[str, Any]] = None) -> str:
        """작업의 성능 슬롯 유형(eval/main)을 판단한다."""
        del job
        eval_model_today = str(self.job_store.get_system_setting("router_eval_model_today", "")).strip()
        if not eval_model_today:
            return "main"

        def _normalize_model_id(value: str) -> str:
            normalized = str(value or "").strip().lower()
            if ":" in normalized:
                return normalized.split(":", 1)[1].strip()
            return normalized

        actual_model = ""
        if payload and isinstance(payload, dict):
            seo_snapshot = payload.get("seo_snapshot", {})
            if isinstance(seo_snapshot, dict):
                actual_model = str(seo_snapshot.get("provider_model", "")).strip()

        if _normalize_model_id(actual_model) and _normalize_model_id(actual_model) == _normalize_model_id(eval_model_today):
            return "eval"
        return "main"

    def _should_shadow_publish(self, *, job: Job, payload: Dict[str, Any]) -> bool:
        """신규 구조에서는 eval 슬롯도 실제 발행한다."""
        del job, payload
        return False

    def _estimate_text_cost_won(self, provider: str, token_usage: Dict[str, Any]) -> float:
        """토큰 사용량 기반 텍스트 비용을 KRW로 추정한다."""
        provider_key = str(provider).strip().lower()
        input_price, output_price = self.PROVIDER_PRICE_PER_1K_USD.get(
            provider_key,
            self.PROVIDER_PRICE_PER_1K_USD["default"],
        )
        total_input_tokens = 0
        total_output_tokens = 0
        for stage in ("parser", "pre_analysis", "quality_step", "voice_step", "sentence_polish"):
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
        slot_type = self._resolve_slot_type(job, payload)
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

    async def _run_vlm_evaluation(
        self,
        *,
        job_id: str,
        post_url: str,
        title: str,
    ) -> None:
        """발행 이후 시각 품질 평가를 비차단으로 수행한다."""
        if self.vlm_evaluator is None:
            return
        try:
            applied_feedback_rules: list[str] = []
            job_snapshot = self.job_store.get_job(job_id)
            if job_snapshot and isinstance(job_snapshot.quality_snapshot, dict):
                pipeline_layers = job_snapshot.quality_snapshot.get("pipeline_layers", {})
                if isinstance(pipeline_layers, dict):
                    raw_rules = pipeline_layers.get("applied_feedback_rules", [])
                    if isinstance(raw_rules, list):
                        applied_feedback_rules = [
                            str(item or "").strip()
                            for item in raw_rules
                            if str(item or "").strip()
                        ]

            eval_result = await self.vlm_evaluator.evaluate(post_url=post_url, job_id=job_id)
            detail = eval_result.to_dict() if hasattr(eval_result, "to_dict") else {"error": "invalid_result"}
            has_error = bool(str(detail.get("error", "")).strip())
            provider_used = str(detail.get("provider_used", "")).strip().lower() or "vlm"

            self._record_job_metric(
                job_id=job_id,
                metric_type="vlm_visual_eval",
                status="failed" if has_error else "success",
                error_code="VLM_EVAL_FAILED" if has_error else "",
                provider=provider_used,
                detail=detail,
            )
            self._merge_vlm_to_quality_snapshot(job_id=job_id, detail=detail)

            if not has_error:
                self._process_feedback_candidates(detail=detail)
                self._record_feedback_rule_application_result(
                    applied_rules=applied_feedback_rules,
                    detail=detail,
                )

            if not has_error and self.notifier:
                send_background = getattr(self.notifier, "send_message_background", None)
                if callable(send_background):
                    total_score = int(detail.get("total_score", 0) or 0)
                    if total_score > 0:
                        message = (
                            "📊 시각 품질 평가 완료\n"
                            f"📝 {str(title or '')[:30]}\n"
                            f"점수: {total_score}/100\n"
                            f"  레이아웃 {int(detail.get('layout', 0) or 0)}/20 · "
                            f"가독성 {int(detail.get('readability', 0) or 0)}/25\n"
                            f"  이미지 {int(detail.get('image_quality', 0) or 0)}/20 · "
                            f"일관성 {int(detail.get('visual_consistency', 0) or 0)}/15\n"
                            f"  인상 {int(detail.get('overall_impression', 0) or 0)}/20"
                        )
                        suggestions = detail.get("suggestions", [])
                        if isinstance(suggestions, list) and suggestions:
                            first = str(suggestions[0] or "").strip()
                            if first:
                                message += f"\n💡 {first}"
                        send_background(message, disable_notification=False)
        except Exception as exc:
            logger.warning("VLM visual evaluation failed (non-critical): %s", exc)

    def _should_run_vlm_evaluation(self, *, job_id: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """VLM 평가 실행 조건을 판정한다."""
        quality_snapshot = payload.get("quality_snapshot", {})
        if not isinstance(quality_snapshot, dict):
            quality_snapshot = {}
        quality_score = float(quality_snapshot.get("score", 0.0) or 0.0)
        content_length = int(quality_snapshot.get("final_content_length", 0) or 0)
        if content_length <= 0:
            content_length = len(str(payload.get("content", "") or ""))

        if quality_score > 0 and quality_score < float(constants.VLM_EARLY_EXIT_MIN_QUALITY_SCORE):
            return False, "quality_below_threshold"
        if content_length > 0 and content_length < int(constants.VLM_EARLY_EXIT_MIN_CONTENT_LENGTH):
            return False, "content_too_short"

        strategy_mode, sampling_rate = self._resolve_vlm_strategy_mode_and_sampling()
        if strategy_mode == "cost" and sampling_rate < 1.0:
            sample_value = self._stable_sample_value(job_id)
            if sample_value > sampling_rate:
                return False, "cost_sampling"
        return True, "ok"

    def _resolve_vlm_strategy_mode_and_sampling(self) -> Tuple[str, float]:
        """VLM 전략 모드와 샘플링 비율을 반환한다."""
        strategy_mode = "cost"
        sampling_rate = float(constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)
        getter = getattr(self.job_store, "get_system_setting", None)
        if getter and callable(getter):
            try:
                text_strategy = str(getter("router_strategy_mode", "cost") or "").strip().lower()
                if text_strategy not in {"cost", "balanced", "quality"}:
                    text_strategy = "cost"
                raw_vlm_strategy = str(
                    getter("router_vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE) or ""
                ).strip().lower()
                if raw_vlm_strategy in {"cost", "balanced", "quality"}:
                    strategy_mode = raw_vlm_strategy
                elif raw_vlm_strategy == "inherit":
                    strategy_mode = text_strategy
                else:
                    strategy_mode = text_strategy

                sampling_raw = getter(
                    "router_vlm_eval_sampling_rate",
                    str(constants.VLM_DEFAULT_EVAL_SAMPLING_RATE),
                )
                sampling_rate = float(sampling_raw)
            except Exception:
                strategy_mode = "cost"
                sampling_rate = float(constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)

        sampling_rate = max(0.0, min(1.0, float(sampling_rate)))
        return strategy_mode, sampling_rate

    def _stable_sample_value(self, job_id: str) -> float:
        """작업 ID 기반 샘플 값을 안정적으로 생성한다."""
        digest = hashlib.sha1(str(job_id or "").encode("utf-8")).hexdigest()
        head = digest[:8]
        return int(head, 16) / 0xFFFFFFFF

    def _process_feedback_candidates(self, *, detail: Dict[str, Any]) -> None:
        """VLM 제안을 후보 집계에 반영하고 승격 시 텔레그램 버튼 알림을 보낸다."""
        suggestions = detail.get("suggestions", [])
        if not isinstance(suggestions, list) or not suggestions:
            return

        observer = getattr(self.job_store, "record_feedback_suggestion_observation", None)
        notifier = self.notifier
        if not observer or not callable(observer):
            return
        if notifier is None or not getattr(notifier, "enabled", False):
            return

        total_score = float(detail.get("total_score", 0.0) or 0.0)
        promoted_candidates: list[Dict[str, Any]] = []
        for suggestion in suggestions[:3]:
            normalized = str(suggestion or "").strip()
            if not normalized:
                continue
            try:
                candidate = observer(
                    suggestion_text=normalized,
                    visual_score=total_score,
                )
            except Exception:
                logger.debug("feedback candidate observation skipped", exc_info=True)
                continue
            if isinstance(candidate, dict) and bool(candidate.get("promoted", False)):
                promoted_candidates.append(candidate)

        for candidate in promoted_candidates:
            self._notify_feedback_candidate(candidate=candidate)

    def _notify_feedback_candidate(self, *, candidate: Dict[str, Any]) -> None:
        """승격된 후보를 텔레그램 inline 버튼으로 승인 요청한다."""
        if self.notifier is None or not getattr(self.notifier, "enabled", False):
            return

        prepare_fn = getattr(self.job_store, "prepare_feedback_candidate_notification", None)
        if not prepare_fn or not callable(prepare_fn):
            return

        candidate_id = str(candidate.get("id", "")).strip()
        if not candidate_id:
            return

        try:
            prepared = prepare_fn(
                candidate_id,
                callback_ttl_hours=int(constants.FEEDBACK_CALLBACK_TOKEN_TTL_HOURS),
            )
        except Exception:
            logger.debug("feedback candidate prepare failed", exc_info=True)
            return
        if not isinstance(prepared, dict):
            return

        callback_token = str(prepared.get("callback_token", "")).strip()
        if not callback_token:
            return

        mention_count = int(prepared.get("mention_count", 0) or 0)
        priority_score = float(prepared.get("priority_score", 0.0) or 0.0)
        avg_visual_score = float(prepared.get("avg_visual_score", 0.0) or 0.0)
        suggestion_text = str(prepared.get("suggestion_text", "")).strip()
        if not suggestion_text:
            return

        message = (
            "📊 자동 반영 후보 감지\n\n"
            f"제안: \"{suggestion_text}\"\n"
            f"반복 등장: {mention_count}회\n"
            f"우선순위 점수: {priority_score:.1f}\n"
            f"최근 VLM 평균: {avg_visual_score:.1f}/100\n\n"
            "다음 포스트부터 자동 반영할까요?"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ 적용",
                        "callback_data": f"afl:v1:a:{candidate_id}:{callback_token}",
                    },
                    {
                        "text": "❌ 무시",
                        "callback_data": f"afl:v1:i:{candidate_id}:{callback_token}",
                    },
                    {
                        "text": "⏸ 나중에",
                        "callback_data": f"afl:v1:s:{candidate_id}:{callback_token}",
                    },
                ]
            ]
        }

        send_background = getattr(self.notifier, "send_message_background", None)
        if callable(send_background):
            try:
                send_background(
                    message,
                    disable_notification=False,
                    reply_markup=reply_markup,
                )
            except TypeError:
                # 레거시 notifier 호환: reply_markup 미지원 시 텍스트만 전송한다.
                send_background(
                    message,
                    disable_notification=False,
                )

    def _record_feedback_rule_application_result(
        self,
        *,
        applied_rules: list[str],
        detail: Dict[str, Any],
    ) -> None:
        """적용된 활성 규칙의 사후 VLM 점수를 누적한다."""
        if not applied_rules:
            return

        recorder = getattr(self.job_store, "record_feedback_rule_application", None)
        if not recorder or not callable(recorder):
            return

        total_score = float(detail.get("total_score", 0.0) or 0.0)
        if total_score <= 0:
            return

        try:
            recorder(applied_rules=applied_rules, visual_score=total_score)
        except Exception:
            logger.debug("feedback rule application metric skipped", exc_info=True)

    def _merge_vlm_to_quality_snapshot(self, *, job_id: str, detail: Dict[str, Any]) -> None:
        """quality_snapshot에 시각 평가 결과를 병합한다."""
        job = self.job_store.get_job(job_id)
        if not job:
            return
        snapshot = dict(job.quality_snapshot or {})
        snapshot["vlm_visual"] = {
            "total_score": int(detail.get("total_score", 0) or 0),
            "layout": int(detail.get("layout", 0) or 0),
            "readability": int(detail.get("readability", 0) or 0),
            "image_quality": int(detail.get("image_quality", 0) or 0),
            "visual_consistency": int(detail.get("visual_consistency", 0) or 0),
            "overall_impression": int(detail.get("overall_impression", 0) or 0),
            "suggestions": detail.get("suggestions", []),
            "provider": str(detail.get("provider_used", "")).strip(),
            "model": str(detail.get("model_used", "")).strip(),
            "model_key": str(detail.get("model_key", "")).strip(),
            "estimated_cost_krw": float(detail.get("estimated_cost_krw", 0.0) or 0.0),
            "evaluated_at": str(detail.get("evaluated_at", "")).strip(),
            "screenshot_path": str(detail.get("screenshot_path", "")).strip(),
            "error": str(detail.get("error", "")).strip(),
        }

        updater = getattr(self.job_store, "update_quality_snapshot", None)
        if updater and callable(updater):
            updater(job_id, snapshot)

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
        return bool(
            await self._run_with_job_heartbeat(
                job=job,
                stage="generation",
                work=self._process_generation_impl(job),
            )
        )

    async def _run_with_job_heartbeat(
        self,
        *,
        job: Job,
        stage: str,
        work: Any,
    ) -> Any:
        """장시간 작업 중 running lease 만료를 막기 위해 heartbeat를 주기적으로 갱신한다."""
        interval_sec = max(
            15,
            min(
                60,
                int(
                    getattr(
                        getattr(self.job_store, "config", None),
                        "heartbeat_interval_sec",
                        60,
                    )
                    or 60
                )
                - 5,
            ),
        )
        stop_event = asyncio.Event()

        async def _heartbeat_loop() -> None:
            while not stop_event.is_set():
                try:
                    self.job_store.heartbeat(job.job_id)
                except Exception:
                    logger.debug(
                        "Heartbeat update skipped",
                        extra={"job_id": job.job_id, "stage": stage},
                        exc_info=True,
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                except asyncio.TimeoutError:
                    continue

        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(),
            name=f"job-heartbeat:{job.job_id}:{stage}",
        )
        try:
            self.job_store.heartbeat(job.job_id)
            return await work
        finally:
            stop_event.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _process_generation_impl(self, job: Job) -> bool:
        """생성 단계만 수행하고 결과를 ready_to_publish로 저장한다."""
        if job.prepared_payload:
            restored = self.job_store.save_prepared_payload(job.job_id, job.prepared_payload)
            if restored:
                logger.info("Prepared draft restored", extra={"job_id": job.job_id})
            return restored

        payload = await self._build_publish_payload(job, allow_internal_retry=True)
        if not payload:
            updated = self.job_store.get_job(job.job_id)
            if updated and updated.status == self.job_store.STATUS_AWAITING_IMAGES:
                logger.info("Draft moved to awaiting_images", extra={"job_id": job.job_id})
                return True
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

        if self._should_request_draft_approval(job=job, payload=payload):
            return self._cache_payload_for_draft_approval(job, payload)

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
        return bool(
            await self._run_with_job_heartbeat(
                job=job,
                stage="publication",
                work=self._process_publication_impl(job),
            )
        )

    async def _process_publication_impl(self, job: Job) -> bool:
        """발행 단계만 수행한다. 준비된 payload가 없으면 즉시 생성 후 발행한다."""
        payload = job.prepared_payload
        if not payload:
            payload = self.job_store.load_prepared_payload(job.job_id)
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

    async def prepare_next_pending_job(
        self,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> bool:
        """대기 Job 1건을 선생성해 ready 상태로 저장한다."""
        jobs = self.job_store.claim_due_jobs(
            limit=1,
            job_kind=job_kind,
            required_tag=required_tag,
        )
        if not jobs:
            logger.debug("No pending jobs to prepare")
            return False

        job = jobs[0]
        return await self.process_generation(job)

    async def publish_next_ready_job(
        self,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> bool:
        """ready 상태 Job 1건을 발행한다."""
        jobs = self.job_store.claim_ready_jobs(
            limit=1,
            job_kind=job_kind,
            required_tag=required_tag,
        )
        if not jobs:
            logger.debug("No prepared jobs to publish")
            return False

        return await self.process_publication(jobs[0])

    async def run_next_pending_job(
        self,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> bool:
        """대기 중인 다음 Job 1건을 선점해 실행한다."""
        if await self.publish_next_ready_job(job_kind=job_kind, required_tag=required_tag):
            return True

        jobs = self.job_store.claim_due_jobs(
            limit=1,
            job_kind=job_kind,
            required_tag=required_tag,
        )
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
            import json as _json

            seo_snap = payload.get("seo_snapshot") or {}
            if isinstance(seo_snap, str):
                try:
                    seo_snap = _json.loads(seo_snap)
                except Exception:
                    seo_snap = {}

            quality_snap = payload.get("quality_snapshot") or {}
            if isinstance(quality_snap, str):
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
