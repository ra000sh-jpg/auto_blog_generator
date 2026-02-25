"""APScheduler 기반 자동화 스케줄러."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as time_obj, timedelta, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..collectors.idea_vault_auto_collector import IdeaVaultAutoCollector
    from ..collectors.metrics_collector import MetricsCollector
    from ..seo.feedback_analyzer import FeedbackAnalyzer
    from .job_store import JobStore
    from .notifier import TelegramNotifier
    from .pipeline_service import PipelineService
    from .trend_job_service import TrendJobService

from .resource_monitor import CpuHysteresisMonitor
from .. import constants
from ..constants import DEFAULT_FALLBACK_CATEGORY
from .scheduler_seed import run_daily_quota_seed
from .scheduler_workers import generator_worker_loop, publisher_worker_loop
from .time_utils import parse_iso
from . import scheduler_cycles

logger = logging.getLogger(__name__)

_MULTICHANNEL_SETTING_KEY = "multichannel_enabled"
_IMPLEMENTED_SUB_PLATFORMS = {"naver", "tistory"}


@dataclass
class _FallbackJob:
    """APScheduler 미설치 환경용 간단한 작업 메타데이터."""

    job_id: str
    name: str
    trigger: str
    misfire_grace_time: int


class _FallbackScheduler:
    """APScheduler 대체 스케줄러.

    테스트/개발 환경에서 apscheduler가 없을 때만 사용한다.
    """

    def __init__(self, timezone_name: str):
        self.timezone = timezone_name
        self.jobs: Dict[str, _FallbackJob] = {}
        self.running = False

    def add_job(
        self,
        func: Any,
        trigger: Any,
        id: str,
        name: str,
        replace_existing: bool = True,
        misfire_grace_time: int = 0,
    ) -> None:
        del func, replace_existing
        self.jobs[id] = _FallbackJob(
            job_id=id,
            name=name,
            trigger=str(trigger),
            misfire_grace_time=misfire_grace_time,
        )

    def start(self) -> None:
        self.running = True

    def shutdown(self, wait: bool = False) -> None:
        del wait
        self.running = False


class SchedulerService:
    """주기적 운영 작업을 스케줄링한다."""

    MISFIRE_GRACE_TIME = 86400
    ACTIVE_HOURS = (constants.ACTIVE_HOURS_START, constants.ACTIVE_HOURS_END)
    PUBLISH_ANCHOR_HOURS = (9, 12, 19)
    DRAFT_BUFFER_TARGET = 6
    DEFAULT_GENERATOR_POLL_SECONDS = constants.DEFAULT_GENERATOR_POLL_SECONDS
    DEFAULT_PUBLISHER_POLL_SECONDS = constants.DEFAULT_PUBLISHER_POLL_SECONDS
    DEFAULT_DAILY_TARGET = constants.DEFAULT_DAILY_TARGET
    DEFAULT_IDEA_VAULT_DAILY_QUOTA = constants.DEFAULT_IDEA_VAULT_DAILY_QUOTA
    WEEKLY_COMPETITION_TEST_END_WEEKDAY = 3  # 목요일(월=0)

    def __init__(
        self,
        trend_service: Optional["TrendJobService"] = None,
        pipeline_service: Optional["PipelineService"] = None,
        metrics_collector: Optional["MetricsCollector"] = None,
        feedback_analyzer: Optional["FeedbackAnalyzer"] = None,
        job_store: Optional["JobStore"] = None,
        idea_vault_collector: Optional["IdeaVaultAutoCollector"] = None,
        timezone_name: str = "Asia/Seoul",
        daily_posts_target: int = 3,
        min_post_interval_minutes: int = 60,
        publish_interval_min_minutes: int = 60,
        publish_interval_max_minutes: int = 110,
        cpu_start_threshold_percent: float = 28.0,
        cpu_stop_threshold_percent: float = 35.0,
        cpu_avg_window: int = 5,
        memory_threshold_percent: float = 80.0,
        generator_poll_seconds: int = DEFAULT_GENERATOR_POLL_SECONDS,
        publisher_poll_seconds: int = DEFAULT_PUBLISHER_POLL_SECONDS,
        random_seed: Optional[int] = None,
        notifier: Optional["TelegramNotifier"] = None,
        api_only_mode: bool = False,
    ):
        self.trend_service = trend_service
        self.pipeline_service = pipeline_service
        self.metrics_collector = metrics_collector
        self.feedback_analyzer = feedback_analyzer
        self.job_store = job_store
        self.idea_vault_collector = idea_vault_collector
        self.timezone_name = timezone_name
        self.daily_posts_target = max(1, daily_posts_target)
        self.min_post_interval_minutes = max(1, min_post_interval_minutes)
        self.publish_interval_min_minutes = max(
            self.min_post_interval_minutes,
            publish_interval_min_minutes,
        )
        self.publish_interval_max_minutes = max(
            self.publish_interval_min_minutes,
            publish_interval_max_minutes,
        )
        self.cpu_start_threshold_percent = max(1.0, cpu_start_threshold_percent)
        self.cpu_stop_threshold_percent = max(
            self.cpu_start_threshold_percent + 1.0,
            cpu_stop_threshold_percent,
        )
        self.cpu_avg_window = max(3, cpu_avg_window)
        self.memory_threshold_percent = max(1.0, memory_threshold_percent)
        self.generator_poll_seconds = max(5, generator_poll_seconds)
        self.publisher_poll_seconds = max(5, publisher_poll_seconds)
        self.random_seed = random_seed
        self.notifier = notifier
        self.api_only_mode = bool(api_only_mode)
        self._scheduler: Any = None
        self._generator_task: Optional[asyncio.Task[None]] = None
        self._publisher_task: Optional[asyncio.Task[None]] = None
        self._daily_publish_slots: List[datetime] = []
        self._publish_slot_date: Optional[date] = None
        self._publish_wait_until_utc: Optional[datetime] = None
        self._last_daily_summary_date: Optional[date] = None
        self._cpu_monitor = CpuHysteresisMonitor(
            start_threshold_percent=self.cpu_start_threshold_percent,
            stop_threshold_percent=self.cpu_stop_threshold_percent,
            sample_window=self.cpu_avg_window,
        )

    def setup_scheduler(self) -> None:
        """스케줄러를 구성하고 작업을 등록한다."""
        if self.api_only_mode:
            logger.info("API-only mode: schedule jobs are intentionally disabled")
            return

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            scheduler: Any = AsyncIOScheduler(timezone=self.timezone_name)
            cron_trigger = CronTrigger
            logger.info("APScheduler loaded")
        except Exception:
            scheduler = _FallbackScheduler(timezone_name=self.timezone_name)
            cron_trigger = lambda **kwargs: ("cron", kwargs)  # type: ignore[assignment]
            logger.warning("apscheduler not installed, fallback scheduler enabled")

        self._scheduler = scheduler

        if self.trend_service:
            self._scheduler.add_job(
                self._run_trend_collection,
                cron_trigger(hour=9, minute=0),
                id="trend_collection",
                name="트렌드 키워드 수집",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )

        if self.metrics_collector:
            self._scheduler.add_job(
                self._run_metrics_collection,
                cron_trigger(hour=6, minute=0),
                id="metrics_collection",
                name="조회수 수집",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )

        if self.feedback_analyzer:
            self._scheduler.add_job(
                self._run_feedback_analysis,
                cron_trigger(day_of_week="mon", hour=0, minute=0),
                id="feedback_analysis",
                name="피드백 분석",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )

        if self.notifier and self.notifier.enabled:
            self._scheduler.add_job(
                self._run_daily_summary_notification,
                cron_trigger(hour=22, minute=30),
                id="daily_summary_notification",
                name="일일 요약 알림",
                replace_existing=True,
                misfire_grace_time=3600,
            )

        if self.job_store:
            self._scheduler.add_job(
                self._run_daily_quota_seed,
                cron_trigger(hour=0, minute=5),
                id="daily_quota_seed",
                name="일간 큐 시드 생성",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )
            self._scheduler.add_job(
                self._run_weekly_model_competition,
                cron_trigger(hour=0, minute=6),
                id="weekly_model_competition",
                name="주간 모델 경쟁 상태 갱신",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )

        if self.idea_vault_collector:
            # Track A: 매일 06:00 / 15:00 두 번 RSS 자동 수집
            self._scheduler.add_job(
                self._run_idea_vault_auto_collect,
                cron_trigger(hour=6, minute=0),
                id="idea_vault_collect_morning",
                name="아이디어 금고 자동 수집 (오전)",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )
            self._scheduler.add_job(
                self._run_idea_vault_auto_collect,
                cron_trigger(hour=15, minute=0),
                id="idea_vault_collect_afternoon",
                name="아이디어 금고 자동 수집 (오후)",
                replace_existing=True,
                misfire_grace_time=self.MISFIRE_GRACE_TIME,
            )

        logger.info("Scheduler setup complete")

    async def start(self) -> None:
        """스케줄러를 시작하고 시작 직후 catch-up을 수행한다."""
        if self.api_only_mode:
            logger.info("API-only mode: background execution is skipped")
            return

        if self._scheduler is None:
            self.setup_scheduler()
        self._scheduler.start()
        logger.info("Scheduler started")

        # 생성/발행은 APScheduler와 분리된 비차단 워커 루프로 동작한다.
        if self.pipeline_service:
            self._generator_task = asyncio.create_task(
                self._generator_worker_loop(),
                name="scheduler-generator-worker",
            )
            self._publisher_task = asyncio.create_task(
                self._publisher_worker_loop(),
                name="scheduler-publisher-worker",
            )
            logger.info(
                "Worker loops started",
                extra={
                    "generator_poll_seconds": self.generator_poll_seconds,
                    "publisher_poll_seconds": self.publisher_poll_seconds,
                },
            )

        asyncio.create_task(self._run_startup_catchup())

    async def stop(self) -> None:
        """스케줄러를 중지한다."""
        if self.api_only_mode:
            await self._cancel_task(self._generator_task)
            await self._cancel_task(self._publisher_task)
            self._generator_task = None
            self._publisher_task = None
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("API-only scheduler stop requested")
            return

        await self._cancel_task(self._generator_task)
        await self._cancel_task(self._publisher_task)
        self._generator_task = None
        self._publisher_task = None

        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    async def _cancel_task(self, task: Optional[asyncio.Task[None]]) -> None:
        """백그라운드 태스크를 안전하게 종료한다."""
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _generator_worker_loop(self) -> None:
        """CPU 여유 시 초안 선생성을 수행하는 워커 루프."""
        await generator_worker_loop(self)

    async def _publisher_worker_loop(self) -> None:
        """시간 분포 기반 발행 워커 루프."""
        await publisher_worker_loop(self)

    async def _run_startup_catchup(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_startup_catchup(self, *args, **kwargs)

    async def _run_idea_vault_auto_collect(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_idea_vault_auto_collect(self, *args, **kwargs)

    async def _run_trend_collection(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_trend_collection(self, *args, **kwargs)

    async def _run_metrics_collection(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_metrics_collection(self, *args, **kwargs)

    async def _run_feedback_analysis(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_feedback_analysis(self, *args, **kwargs)

    async def _run_sub_job_catchup(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_sub_job_catchup(self, *args, **kwargs)

    async def _run_sub_job_publish_catchup(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_sub_job_publish_catchup(self, *args, **kwargs)

    async def _run_daily_summary_notification(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_daily_summary_notification(self, *args, **kwargs)

    async def _run_daily_target_check(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_daily_target_check(self, *args, **kwargs)

    async def _run_draft_prefetch(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_draft_prefetch(self, *args, **kwargs)

    async def _prepare_next_available_job(self, *args, **kwargs):
        return await scheduler_cycles.cycle_prepare_next_available_job(self, *args, **kwargs)

    async def _publish_next_available_job(self, *args, **kwargs):
        return await scheduler_cycles.cycle_publish_next_available_job(self, *args, **kwargs)

    def _is_publish_interval_ready(self, *args, **kwargs):
        return scheduler_cycles.cycle_is_publish_interval_ready(self, *args, **kwargs)

    def _has_resource_headroom(self, *args, **kwargs):
        return scheduler_cycles.cycle_has_resource_headroom(self, *args, **kwargs)

    def _ensure_daily_publish_slots(self, *args, **kwargs):
        return scheduler_cycles.cycle_ensure_daily_publish_slots(self, *args, **kwargs)

    def _get_publish_anchor_hours(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_publish_anchor_hours(self, *args, **kwargs)

    def _build_daily_publish_slots(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_daily_publish_slots(self, *args, **kwargs)

    async def _run_daily_quota_seed(self) -> None:
        """매일 자정에 사용자 설정 비율대로 큐를 생성한다."""
        await run_daily_quota_seed(self)

    async def trigger_seed_cycle(self) -> None:
        """시드 잡 생성 사이클을 1회 실행한다."""
        await self._run_daily_quota_seed()

    async def trigger_draft_cycle(self) -> int:
        """초안 선생성 사이클을 1회 실행하고, 최신 준비 건수를 반환한다."""
        await self._run_draft_prefetch()
        return self.get_ready_draft_count()

    async def trigger_publish_cycle(self) -> bool:
        """준비된 초안 1건을 즉시 발행하고 발행 결과를 반환한다."""
        return await self._publish_next_available_job()

    def _week_start_local(self, *args, **kwargs):
        return scheduler_cycles.cycle_week_start_local(self, *args, **kwargs)

    def _next_week_apply_at_local_iso(self, *args, **kwargs):
        return scheduler_cycles.cycle_next_week_apply_at_local_iso(self, *args, **kwargs)

    def _build_competition_candidates(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_competition_candidates(self, *args, **kwargs)

    async def _run_weekly_model_competition(self, *args, **kwargs):
        return await scheduler_cycles.cycle_run_weekly_model_competition(self, *args, **kwargs)

    def _get_configured_daily_target(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_configured_daily_target(self, *args, **kwargs)

    def _load_daily_quota_allocations(self, *args, **kwargs):
        return scheduler_cycles.cycle_load_daily_quota_allocations(self, *args, **kwargs)

    def _get_configured_idea_vault_quota(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_configured_idea_vault_quota(self, *args, **kwargs)

    def _get_idea_vault_pending_count(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_idea_vault_pending_count(self, *args, **kwargs)

    def _build_default_quota_allocations(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_default_quota_allocations(self, *args, **kwargs)

    def _normalize_topic_mode(self, *args, **kwargs):
        return scheduler_cycles.cycle_normalize_topic_mode(self, *args, **kwargs)

    def _infer_topic_mode_from_category(self, *args, **kwargs):
        return scheduler_cycles.cycle_infer_topic_mode_from_category(self, *args, **kwargs)

    def _persona_id_for_topic(self, *args, **kwargs):
        return scheduler_cycles.cycle_persona_id_for_topic(self, *args, **kwargs)

    def _build_seed_title(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_seed_title(self, *args, **kwargs)

    def _build_seed_keywords(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_seed_keywords(self, *args, **kwargs)

    def _build_vault_seed_title(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_vault_seed_title(self, *args, **kwargs)

    def _build_vault_seed_keywords(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_vault_seed_keywords(self, *args, **kwargs)

    def _build_rng_for_date(self, *args, **kwargs):
        return scheduler_cycles.cycle_build_rng_for_date(self, *args, **kwargs)

    def _get_memory_percent(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_memory_percent(self, *args, **kwargs)

    def _get_ready_draft_count(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_ready_draft_count(self, *args, **kwargs)

    def get_ready_draft_count(self) -> int:
        """외부 호출용으로 현재 ready 초안 개수를 조회한다."""
        return self._get_ready_draft_count()

    def _get_ready_count(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_ready_count(self, *args, **kwargs)

    def _get_due_count(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_due_count(self, *args, **kwargs)

    def _log_sub_job_catchup_stats(self, *args, **kwargs):
        return scheduler_cycles.cycle_log_sub_job_catchup_stats(self, *args, **kwargs)

    def _get_now_local(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_now_local(self, *args, **kwargs)

    def _today_key(self, *args, **kwargs):
        return scheduler_cycles.cycle_today_key(self, *args, **kwargs)

    def _week_key(self, *args, **kwargs):
        return scheduler_cycles.cycle_week_key(self, *args, **kwargs)

    def _is_multichannel_enabled(self, *args, **kwargs):
        return scheduler_cycles.cycle_is_multichannel_enabled(self, *args, **kwargs)

    def _master_job_kind(self, *args, **kwargs):
        return scheduler_cycles.cycle_master_job_kind(self, *args, **kwargs)

    def _sub_job_kind(self, *args, **kwargs):
        return scheduler_cycles.cycle_sub_job_kind(self, *args, **kwargs)

    def _get_today_post_count(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_today_post_count(self, *args, **kwargs)

    def get_today_post_count(self) -> int:
        """외부 호출용으로 당일 마스터 발행 건수를 조회한다."""
        return self._get_today_post_count()

    def get_next_publish_slot_kst(self) -> Optional[str]:
        """다음 발행 슬롯 시각을 KST ISO 문자열로 반환한다."""
        try:
            now_local = self._get_now_local()
            today_completed = self._get_today_post_count()
            slots = self._daily_publish_slots
            if not slots:
                return None
            idx = today_completed
            if idx < len(slots):
                return slots[idx].isoformat(timespec="seconds")
        except Exception:
            pass
        return None

    def _get_last_post_time(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_last_post_time(self, *args, **kwargs)

    def _get_today_failed_count(self, *args, **kwargs):
        return scheduler_cycles.cycle_get_today_failed_count(self, *args, **kwargs)

async def run_scheduler_forever(*args, **kwargs):
    """스케줄러를 시작하고 종료 신호까지 대기한다."""
    return await scheduler_cycles.run_scheduler_forever(SchedulerService, *args, **kwargs)
