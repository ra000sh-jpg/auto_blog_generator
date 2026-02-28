"""스케줄러 워커 루프 책임 분리 모듈."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .scheduler_service import SchedulerService

logger = logging.getLogger(__name__)


async def generator_worker_loop(service: "SchedulerService") -> None:
    """CPU 여유 시 초안 선생성을 수행하는 워커 루프."""
    consecutive_failures = 0
    try:
        while True:
            if service.job_store:
                try:
                    # 데몬 생존 여부 판단을 위해 최신 하트비트를 기록한다.
                    service.job_store.set_system_setting(
                        "scheduler_daemon_heartbeat_at",
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                except Exception:
                    # 하트비트 실패는 워커 실행을 막지 않는다.
                    pass
                paused_flag = service.job_store.get_system_setting("scheduler_paused", "")
                if paused_flag == "1":
                    logger.debug("Scheduler paused — skipping generator cycle")
                    await asyncio.sleep(service.generator_poll_seconds)
                    continue
            try:
                await service._run_sub_job_catchup()
                await service._run_draft_prefetch()
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                logger.error("Generator worker error: %s", exc)
                if consecutive_failures >= 5:
                    logger.critical(
                        f"[Generator Worker] 연속 {consecutive_failures}회 실패. 수동 점검 필요.",
                        extra={"consecutive_failures": consecutive_failures},
                    )
            await asyncio.sleep(service.generator_poll_seconds)
    except asyncio.CancelledError:
        logger.info("Generator worker stopped")


async def publisher_worker_loop(service: "SchedulerService") -> None:
    """시간 분포 기반 발행 워커 루프."""
    consecutive_failures = 0
    try:
        while True:
            if service.job_store:
                paused_flag = service.job_store.get_system_setting("scheduler_paused", "")
                if paused_flag == "1":
                    logger.debug("Scheduler paused — skipping publisher cycle")
                    await asyncio.sleep(service.publisher_poll_seconds)
                    continue
            try:
                await service._run_sub_job_publish_catchup()
                await service._run_daily_target_check()
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                logger.error("Publisher worker error: %s", exc)
                if consecutive_failures >= 5:
                    logger.critical(
                        f"[Publisher Worker] 연속 {consecutive_failures}회 실패. 수동 점검 필요.",
                        extra={"consecutive_failures": consecutive_failures},
                    )
            await asyncio.sleep(service.publisher_poll_seconds)
    except asyncio.CancelledError:
        logger.info("Publisher worker stopped")


async def image_collector_worker_loop(service: "SchedulerService") -> None:
    """awaiting_images 잡을 폴링해 이미지 수집 완료 시 ready_to_publish로 승격한다."""
    from .telegram_image_collector import TelegramImageCollector

    logger.info("Image collector worker loop started")
    try:
        while True:
            try:
                if (
                    not service.job_store
                    or not service.notifier
                    or not getattr(service.notifier, "enabled", False)
                ):
                    await asyncio.sleep(30)
                    continue
                paused_flag = service.job_store.get_system_setting("scheduler_paused", "")
                if paused_flag == "1":
                    await asyncio.sleep(15)
                    continue

                jobs = service.job_store.list_awaiting_images_jobs()
                if not jobs:
                    await asyncio.sleep(15)
                    continue

                # 오배정 방지를 위해 대기열의 첫 잡만 순차적으로 처리한다.
                job = jobs[0]
                output_dir = "data/images"
                if service.pipeline_service is not None:
                    output_dir = str(getattr(service.pipeline_service, "_image_output_dir", output_dir))
                collector = TelegramImageCollector(
                    job_store=service.job_store,
                    notifier=service.notifier,
                    image_output_dir=output_dir,
                )

                collected = await collector.poll_and_collect(job.job_id)
                if collected:
                    has_next = await collector.send_next_prompt(job.job_id, job.title)
                    if not has_next and collector.all_slots_received(job.job_id):
                        await _promote_to_ready(service, job, collector)
                else:
                    # sent 슬롯이 없거나 전송 실패한 경우 다음 프롬프트를 재시도한다.
                    await collector.send_next_prompt(job.job_id, job.title)
            except Exception as exc:
                logger.error("Image collector worker error: %s", exc, exc_info=True)
            await asyncio.sleep(15)
    except asyncio.CancelledError:
        logger.info("Image collector worker stopped")


async def _promote_to_ready(
    service: "SchedulerService",
    job: Any,
    collector: Any,
) -> None:
    """수집 완료 이미지를 payload에 주입하고 ready_to_publish로 전환한다."""
    if not service.job_store:
        return

    try:
        payload = service.job_store.load_prepared_payload(job.job_id)
        if not payload:
            logger.error("[ImageCollector] %s: prepared payload not found", job.job_id)
            return

        received = collector.get_received_paths(job.job_id)
        slots = collector.get_slots(job.job_id)

        thumbnail_path = next(
            (
                received[str(slot.get("slot_id"))]
                for slot in slots
                if slot.get("slot_role") == "thumbnail"
                and str(slot.get("slot_id")) in received
            ),
            "",
        )
        content_paths = [
            received[str(slot.get("slot_id"))]
            for slot in slots
            if slot.get("slot_role") != "thumbnail"
            and str(slot.get("slot_id")) in received
        ]

        payload["thumbnail"] = thumbnail_path
        payload["images"] = content_paths
        image_sources = payload.get("image_sources")
        if not isinstance(image_sources, dict):
            image_sources = {}
        for path in [thumbnail_path, *content_paths]:
            normalized_path = str(path or "").strip()
            if not normalized_path:
                continue
            image_sources[normalized_path] = {
                "kind": "ai_generated",
                "provider": "telegram_semi_auto",
            }
        payload["image_sources"] = image_sources

        saved = service.job_store.save_prepared_payload(
            job.job_id,
            payload,
            mark_ready=True,
        )
        if not saved:
            # 예외 케이스에서는 상태만 강제로 승격한다.
            service.job_store.update_job_status(job.job_id, service.job_store.STATUS_READY)
            logger.warning("[ImageCollector] %s: payload save failed, status only promoted", job.job_id)

        collector.clear_slots(job.job_id)
        logger.info(
            "[ImageCollector] %s: all images collected -> ready_to_publish (thumbnail=%s, content=%d)",
            job.job_id,
            bool(thumbnail_path),
            len(content_paths),
        )

        if service.notifier and service.notifier.enabled:
            await service.notifier.send_message(
                f"✅ 이미지 수집 완료!\n"
                f"📝 {job.title}\n"
                f"🖼 총 {len(received)}장 -> 자동 발행 대기 중"
            )
    except Exception as exc:
        logger.error("[ImageCollector] promote failed for %s: %s", job.job_id, exc, exc_info=True)
