"""스케줄러 워커 루프 책임 분리 모듈."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING

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
