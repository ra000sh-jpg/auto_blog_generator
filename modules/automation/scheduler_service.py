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

    async def _run_startup_catchup(self) -> None:
        """시작 시점에 놓친 작업을 보정 실행한다."""
        logger.info("Running startup catch-up")
        await self._run_daily_quota_seed()
        await self._run_draft_prefetch()
        await self._run_daily_target_check()
        await self._run_metrics_collection()
        await self._run_weekly_model_competition()
        await self._run_feedback_analysis()
        await self._run_sub_job_catchup()
        await self._run_sub_job_publish_catchup()

    async def _run_idea_vault_auto_collect(self) -> None:
        """RSS 피드에서 아이디어를 수집해 idea_vault 에 저장한다 (Track A)."""
        logger.info("Running idea vault auto collect")
        if not self.idea_vault_collector:
            return
        try:
            saved = await self.idea_vault_collector.run_once()
            logger.info("Idea vault auto collect: saved %d items", saved)
        except Exception as exc:
            logger.error("Idea vault auto collect failed: %s", exc)

    async def _run_trend_collection(self) -> None:
        logger.info("Running trend collection")
        if not self.trend_service:
            return
        try:
            created = self.trend_service.fetch_and_create_jobs()
            logger.info("Trend jobs created: %d", len(created))
        except Exception as exc:
            logger.error("Trend collection failed: %s", exc)

    async def _run_metrics_collection(self) -> None:
        logger.info("Running metrics collection")
        if not self.metrics_collector:
            return
        if self.job_store:
            today_key = self._today_key()
            last_key = self.job_store.get_system_setting("scheduler_last_metrics_date", "")
            if last_key == today_key:
                logger.debug("Metrics collection skipped: already collected today (%s)", today_key)
                return
        try:
            count = await self.metrics_collector.collect_all_pending()
            if self.job_store:
                self.job_store.set_system_setting("scheduler_last_metrics_date", self._today_key())
            logger.info("Metrics collected: %d", count)
        except Exception as exc:
            logger.error("Metrics collection failed: %s", exc)

    async def _run_feedback_analysis(self) -> None:
        logger.info("Running feedback analysis")
        if not self.feedback_analyzer:
            return
        if self.job_store:
            week_key = self._week_key()
            last_key = self.job_store.get_system_setting("scheduler_last_feedback_week", "")
            if last_key == week_key:
                logger.debug("Feedback analysis skipped: already analyzed week (%s)", week_key)
                return
        try:
            snapshot = await self.feedback_analyzer.run_analysis(
                platform="naver",
                trigger="scheduled",
                apply_updates=True,
            )
            if self.job_store:
                self.job_store.set_system_setting("scheduler_last_feedback_week", self._week_key())
            if snapshot:
                logger.info("Feedback analysis complete")
        except Exception as exc:
            logger.error("Feedback analysis failed: %s", exc)

    async def _run_sub_job_catchup(self) -> None:
        """완료된 마스터 잡을 기준으로 누락된 서브 잡을 생성한다."""
        if not self.job_store:
            return
        started_at = perf_counter()
        skip_reasons: Counter[str] = Counter()
        created_count = 0
        scanned_pairs = 0
        master_count = 0
        sub_channel_count = 0

        if not self._is_multichannel_enabled():
            skip_reasons["multichannel_disabled"] += 1
            self._log_sub_job_catchup_stats(
                created_count=created_count,
                scanned_pairs=scanned_pairs,
                master_count=master_count,
                sub_channel_count=sub_channel_count,
                skip_reasons=skip_reasons,
                started_at=started_at,
            )
            return

        sub_channels = self.job_store.get_active_sub_channels()
        sub_channel_count = len(sub_channels)
        if not sub_channels:
            skip_reasons["no_active_sub_channels"] += 1
            self._log_sub_job_catchup_stats(
                created_count=created_count,
                scanned_pairs=scanned_pairs,
                master_count=master_count,
                sub_channel_count=sub_channel_count,
                skip_reasons=skip_reasons,
                started_at=started_at,
            )
            return

        masters = self.job_store.list_recent_completed_jobs(
            limit=200,
            job_kind=self._master_job_kind(),
        )
        master_count = len(masters)
        if not masters:
            skip_reasons["no_recent_completed_masters"] += 1
            self._log_sub_job_catchup_stats(
                created_count=created_count,
                scanned_pairs=scanned_pairs,
                master_count=master_count,
                sub_channel_count=sub_channel_count,
                skip_reasons=skip_reasons,
                started_at=started_at,
            )
            return

        for master in masters:
            base_time = parse_iso(master.completed_at or master.updated_at)
            for channel in sub_channels:
                scanned_pairs += 1
                platform = str(channel.get("platform", "")).strip().lower()
                if platform not in _IMPLEMENTED_SUB_PLATFORMS:
                    skip_reasons["publisher_not_implemented"] += 1
                    continue

                channel_id = str(channel.get("channel_id", "")).strip()
                if not channel_id:
                    skip_reasons["missing_channel_id"] += 1
                    continue

                existing = self.job_store.get_sub_job_by_master_channel(master.job_id, channel_id)
                if existing:
                    skip_reasons["already_exists"] += 1
                    continue

                delay_minutes = max(0, int(channel.get("publish_delay_minutes", 90)))
                scheduled_at = (base_time + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
                sub_job_id = str(uuid.uuid4())
                sub_persona = str(channel.get("persona_id", "")).strip() or master.persona_id
                sub_title = f"[{str(channel.get('label', '')).strip()}] {master.title}"

                success = self.job_store.schedule_job(
                    job_id=sub_job_id,
                    title=sub_title,
                    seed_keywords=list(master.seed_keywords),
                    platform=platform,
                    persona_id=sub_persona,
                    scheduled_at=scheduled_at,
                    max_retries=max(1, int(master.max_retries)),
                    tags=list(master.tags or []),
                    category=str(master.category or ""),
                    job_kind=self._sub_job_kind(),
                    master_job_id=master.job_id,
                    channel_id=channel_id,
                    status=self.job_store.STATUS_QUEUED,
                )
                if success:
                    created_count += 1
                else:
                    skip_reasons["schedule_failed"] += 1

        self._log_sub_job_catchup_stats(
            created_count=created_count,
            scanned_pairs=scanned_pairs,
            master_count=master_count,
            sub_channel_count=sub_channel_count,
            skip_reasons=skip_reasons,
            started_at=started_at,
        )

    async def _run_sub_job_publish_catchup(self, max_jobs: int = 20) -> None:
        """예약 시각이 지난 서브 잡을 우선 처리한다."""
        if not self.job_store or not self.pipeline_service:
            return

        started_at = perf_counter()
        skip_reasons: Counter[str] = Counter()
        processed = 0
        published_count = 0
        prepared_count = 0
        limit = max(1, int(max_jobs or 20))

        for _ in range(limit):
            published = await self._publish_next_available_job(job_kind=self._sub_job_kind())
            if published:
                processed += 1
                published_count += 1
                await asyncio.sleep(constants.SCHEDULER_SUBJOB_STEP_SLEEP_SEC)
                continue

            ready_count = self._get_ready_count(job_kind=self._sub_job_kind())
            due_count = self._get_due_count(job_kind=self._sub_job_kind())
            if ready_count <= 0 and due_count <= 0:
                skip_reasons["no_due_jobs"] += 1
                break

            prepared = await self._prepare_next_available_job(job_kind=self._sub_job_kind())
            if prepared:
                processed += 1
                prepared_count += 1
                await asyncio.sleep(constants.SCHEDULER_SUBJOB_STEP_SLEEP_SEC)
                continue

            if ready_count > 0:
                skip_reasons["ready_claim_or_publish_failed"] += 1
            if due_count > 0:
                skip_reasons["due_claim_or_prepare_failed"] += 1
            break

        if processed >= limit:
            skip_reasons["max_jobs_limit_reached"] += 1

        elapsed_sec = max(0.001, perf_counter() - started_at)
        throughput_per_min = processed * 60.0 / elapsed_sec
        logger.info(
            "Sub job publish catch-up stats: processed=%d published=%d prepared=%d limit=%d throughput_per_min=%.2f skip_reasons=%s elapsed_sec=%.2f",
            processed,
            published_count,
            prepared_count,
            limit,
            throughput_per_min,
            json.dumps(dict(skip_reasons), ensure_ascii=False, sort_keys=True),
            elapsed_sec,
        )

    async def _run_daily_summary_notification(self) -> None:
        """KST 22:30 일일 요약 알림을 전송한다."""
        if not self.notifier or not self.notifier.enabled or not self.job_store:
            return

        now_local = self._get_now_local()
        if self._last_daily_summary_date == now_local.date():
            return

        completed = self._get_today_post_count()
        failed = self._get_today_failed_count()
        queue_stats = self.job_store.get_queue_stats()
        ready_count = int(queue_stats.get("ready_to_publish", 0))
        queued_count = int(queue_stats.get("queued", 0))
        configured_target = self._get_configured_daily_target()
        idea_pending_count = self._get_idea_vault_pending_count()
        idea_daily_quota = self._get_configured_idea_vault_quota(configured_target)

        try:
            sent = await self.notifier.notify_daily_summary(
                local_date=now_local.strftime("%Y-%m-%d"),
                target=configured_target,
                completed=completed,
                failed=failed,
                ready_count=ready_count,
                queued_count=queued_count,
                idea_pending_count=idea_pending_count,
                idea_daily_quota=idea_daily_quota,
            )
            if sent:
                self._last_daily_summary_date = now_local.date()
        except Exception as exc:
            logger.warning("Daily summary notify failed: %s", exc)

    async def _run_daily_target_check(self) -> None:
        """일일 목표 기반으로 준비된 초안을 1건씩 천천히 발행한다."""
        now_local = self._get_now_local()
        configured_target = self._get_configured_daily_target()
        self._ensure_daily_publish_slots(now_local.date(), configured_target)

        if not (self.ACTIVE_HOURS[0] <= now_local.hour < self.ACTIVE_HOURS[1]):
            logger.debug("Outside active hours: %s", now_local)
            return

        today_completed = self._get_today_post_count()
        remaining = configured_target - today_completed
        if remaining <= 0:
            logger.debug("Daily target reached: %d/%d", today_completed, configured_target)
            return

        if not self._is_publish_interval_ready(now_local, today_completed):
            return

        if not self.pipeline_service:
            return

        ready_count = self._get_ready_draft_count()
        if ready_count <= 0:
            logger.info(
                "No prepared draft to publish yet (%d/%d completed)",
                today_completed,
                configured_target,
            )
            return

        logger.info(
            "Daily target check passed (%d/%d), publishing prepared draft",
            today_completed,
            configured_target,
        )
        try:
            published = await self._publish_next_available_job(
                job_kind=self._master_job_kind() if self.job_store else None
            )
            if published:
                self._publish_wait_until_utc = datetime.now(timezone.utc) + timedelta(
                    minutes=self.min_post_interval_minutes
                )
            else:
                # 준비된 초안이 없거나 발행이 실패한 경우 짧은 대기 후 재확인한다.
                self._publish_wait_until_utc = datetime.now(timezone.utc) + timedelta(minutes=10)
        except Exception as exc:
            logger.error("Daily target execution failed: %s", exc)
            self._publish_wait_until_utc = datetime.now(timezone.utc) + timedelta(minutes=10)

    async def _run_draft_prefetch(self) -> None:
        """리소스 여유 시 오늘 목표치만큼 초안을 선생성한다.

        LLM 다중 호출 도중 CPU 가 급등하면 watchdog 태스크가 interrupt_event 를
        set 하고, for 루프는 매 반복마다 이를 확인해 즉시(mid-generation) 탈출한다.
        """
        if not self.pipeline_service:
            return

        ready_count = self._get_ready_draft_count()
        configured_target = self._get_configured_daily_target()
        # CPU 여유 상태일 때 버퍼 목표를 동적으로 상향 (최대 9)
        cpu_allowed, cpu_avg = self._cpu_monitor.check()
        extended_buffer = min(9, self.DRAFT_BUFFER_TARGET + 3) if cpu_allowed and cpu_avg < self._cpu_monitor.start_threshold_percent else self.DRAFT_BUFFER_TARGET
        target_buffer = max(extended_buffer, configured_target + 2)
        needed = max(0, target_buffer - ready_count)
        if needed <= 0:
            logger.debug(
                "Draft buffer is enough (%d ready, target=%d, cpu_avg=%.1f%%)",
                ready_count,
                target_buffer,
                cpu_avg,
            )
            return

        if not self._has_resource_headroom():
            logger.info("Skip draft prefetch due to high resource usage or paused state")
            return

        logger.info(
            "Draft prefetch start (need=%d, ready=%d, target=%d)",
            needed,
            ready_count,
            target_buffer,
        )

        # mid-generation 인터럽트를 위한 이벤트 + watchdog 태스크 시작
        interrupt_event = self._cpu_monitor.make_interrupt_event()
        watchdog_task = asyncio.create_task(
            self._cpu_monitor.run_interrupt_watchdog(interrupt_event),
            name="cpu-interrupt-watchdog",
        )

        prepared_count = 0
        try:
            for _ in range(needed):
                # watchdog 가 CPU 급등을 감지하면 즉시 루프 탈출
                if interrupt_event.is_set():
                    logger.info(
                        "Draft prefetch interrupted by CPU watchdog "
                        "(prepared=%d, needed=%d)",
                        prepared_count,
                        needed,
                    )
                    break
                if not self._has_resource_headroom():
                    logger.info(
                        "Draft prefetch stopped: resource headroom lost "
                        "(prepared=%d, needed=%d)",
                        prepared_count,
                        needed,
                    )
                    break
                prepared = await self._prepare_next_available_job(
                    job_kind=self._master_job_kind() if self.job_store else None
                )
                if not prepared:
                    break
                prepared_count += 1
                await asyncio.sleep(constants.SCHEDULER_DRAFT_PREFETCH_STEP_SLEEP_SEC)
        finally:
            # watchdog 정리: 루프가 끝나면 이벤트를 set 해 watchdog 코루틴도 종료
            interrupt_event.set()
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        logger.info("Draft prefetch done (prepared=%d, needed=%d)", prepared_count, needed)

    async def _prepare_next_available_job(self, job_kind: Optional[str] = None) -> bool:
        """파이프라인이 지원하는 방식으로 다음 초안을 생성한다."""
        prepare_fn = getattr(self.pipeline_service, "prepare_next_pending_job", None)
        if prepare_fn and callable(prepare_fn):
            try:
                return bool(await prepare_fn(job_kind=job_kind))
            except TypeError:
                return bool(await prepare_fn())
        return False

    async def _publish_next_available_job(self, job_kind: Optional[str] = None) -> bool:
        """파이프라인이 지원하는 방식으로 다음 발행을 실행한다."""
        publish_fn = getattr(self.pipeline_service, "publish_next_ready_job", None)
        if publish_fn and callable(publish_fn):
            try:
                return bool(await publish_fn(job_kind=job_kind))
            except TypeError:
                return bool(await publish_fn())
        run_fn = getattr(self.pipeline_service, "run_next_pending_job", None)
        if run_fn and callable(run_fn):
            try:
                return bool(await run_fn(job_kind=job_kind))
            except TypeError:
                return bool(await run_fn())
        return False

    def _is_publish_interval_ready(
        self,
        now_local: datetime,
        today_completed: int,
    ) -> bool:
        """발행 최소 간격/일간 분포 슬롯 조건을 확인한다."""
        now_utc = datetime.now(timezone.utc)

        if self._publish_wait_until_utc and now_utc < self._publish_wait_until_utc:
            wait_seconds = (self._publish_wait_until_utc - now_utc).total_seconds()
            logger.debug("Waiting publish cooldown (%.0f sec left)", wait_seconds)
            return False

        last_completed = self._get_last_post_time()
        if last_completed:
            elapsed_minutes = (now_utc - last_completed).total_seconds() / 60.0
            if elapsed_minutes < self.min_post_interval_minutes:
                logger.debug(
                    "Post interval not met (elapsed=%.1fmin, required=%dmin)",
                    elapsed_minutes,
                    self.min_post_interval_minutes,
                )
                return False

        if today_completed >= len(self._daily_publish_slots):
            logger.debug("No remaining publish slots for today")
            return False

        next_slot = self._daily_publish_slots[today_completed]
        if now_local < next_slot:
            wait_seconds = (next_slot - now_local).total_seconds()
            logger.debug(
                "Waiting next weighted publish slot (%.0f sec left)",
                wait_seconds,
            )
            return False

        return True

    def _has_resource_headroom(self) -> bool:
        """CPU/메모리 사용량이 임계값 이내인지 확인한다."""
        cpu_allowed, cpu_avg = self._cpu_monitor.check()
        memory_percent = self._get_memory_percent()
        logger.debug(
            "Resource check cpu_avg=%.1f%% mem=%.1f%% (cpu start/stop %.1f/%.1f, mem<=%.1f)",
            cpu_avg,
            memory_percent,
            self.cpu_start_threshold_percent,
            self.cpu_stop_threshold_percent,
            self.memory_threshold_percent,
        )
        return cpu_allowed and memory_percent <= self.memory_threshold_percent

    def _ensure_daily_publish_slots(self, target_date: date, daily_target: int) -> None:
        """당일 발행 슬롯을 준비한다."""
        if (
            self._publish_slot_date == target_date
            and self._daily_publish_slots
            and len(self._daily_publish_slots) == max(1, daily_target)
        ):
            return

        self._daily_publish_slots = self._build_daily_publish_slots(target_date, daily_target)
        self._publish_slot_date = target_date
        self._publish_wait_until_utc = None
        logger.info(
            "Daily weighted publish slots generated",
            extra={
                "date": target_date.isoformat(),
                "slots": [slot.isoformat() for slot in self._daily_publish_slots],
            },
        )

    def _get_publish_anchor_hours(self) -> tuple:
        """발행 앵커 시간대를 반환한다.

        DB system_settings 의 ``publish_anchor_hours`` 키에 쉼표 구분 정수 목록을
        저장하면 런타임에 반영된다.  예) ``"9,12,19"``
        설정이 없거나 파싱에 실패하면 클래스 상수 ``PUBLISH_ANCHOR_HOURS`` 를 사용한다.
        """
        if self.job_store:
            raw = self.job_store.get_system_setting("publish_anchor_hours", "")
            if raw and raw.strip():
                try:
                    hours = tuple(
                        int(h.strip())
                        for h in raw.split(",")
                        if h.strip().isdigit()
                    )
                    if hours:
                        return hours
                except Exception:
                    pass
        return self.PUBLISH_ANCHOR_HOURS

    def _build_daily_publish_slots(self, target_date: date, daily_target: Optional[int] = None) -> List[datetime]:
        """출/점/퇴 중심 가우시안 분포로 하루 발행 슬롯을 만든다.

        앵커 시간(기본 9·12·19시)마다 σ=20분 정규분포 지터(jitter)를 더해
        자연스러운 발행 시간대를 구성한다.  앵커 시간은 DB 설정
        ``publish_anchor_hours`` 에서 동적으로 읽어온다.
        """
        local_tz = self._get_now_local().tzinfo
        if local_tz is None:
            local_tz = timezone(timedelta(hours=9))

        anchor_hours = self._get_publish_anchor_hours()
        rng = self._build_rng_for_date(target_date)
        candidates: List[datetime] = []
        resolved_target = max(1, int(daily_target or self.daily_posts_target))

        for index in range(resolved_target):
            if index < len(anchor_hours):
                base_hour = anchor_hours[index]
            else:
                base_hour = rng.choices(
                    population=list(anchor_hours),
                    weights=[0.45, 0.35, 0.20][: len(anchor_hours)],
                    k=1,
                )[0]

            base_time = datetime.combine(
                target_date,
                time_obj(hour=base_hour, minute=0),
                tzinfo=local_tz,
            )
            # σ=20분 가우시안 지터, ±40분 클램프 (기존 σ=25, ±45→55 에서 조정)
            jitter_minutes = int(rng.gauss(0, 20))
            jitter_minutes = max(-40, min(40, jitter_minutes))
            slot = base_time + timedelta(minutes=jitter_minutes)

            start_bound = datetime.combine(
                target_date,
                time_obj(hour=self.ACTIVE_HOURS[0], minute=0),
                tzinfo=local_tz,
            )
            end_bound = datetime.combine(
                target_date,
                time_obj(hour=self.ACTIVE_HOURS[1] - 1, minute=55),
                tzinfo=local_tz,
            )
            if slot < start_bound:
                slot = start_bound
            if slot > end_bound:
                slot = end_bound

            candidates.append(slot)

        candidates.sort()
        min_interval = timedelta(minutes=self.min_post_interval_minutes)
        max_interval = timedelta(minutes=self.publish_interval_max_minutes)
        for index in range(1, len(candidates)):
            previous = candidates[index - 1]
            current = candidates[index]
            gap = current - previous
            if gap < min_interval:
                candidates[index] = previous + min_interval
            elif gap > max_interval:
                candidates[index] = previous + max_interval

        return candidates

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

    def _week_start_local(self, now_local: datetime) -> date:
        """현재 시각 기준 주 시작일(월요일)을 반환한다."""
        return now_local.date() - timedelta(days=now_local.weekday())

    def _next_week_apply_at_local_iso(self, week_start: date) -> str:
        """다음 주 월요일 00:05 로컬 시각 ISO를 반환한다."""
        next_week = week_start + timedelta(days=7)
        dt = datetime.combine(
            next_week,
            time_obj(hour=0, minute=5),
            tzinfo=self._get_now_local().tzinfo,
        )
        return dt.isoformat(timespec="seconds")

    def _build_competition_candidates(self) -> List[Dict[str, Any]]:
        """라우터 설정 기준으로 주간 경쟁 후보 모델을 구성한다."""
        from ..llm.llm_router import LLMRouter

        router = LLMRouter(job_store=self.job_store)
        plan = router.build_plan()
        available = list(plan.get("available_text_models", []))
        normalized: List[Dict[str, Any]] = []
        for item in available:
            model_id = str(item.get("model", "")).strip()
            provider = str(item.get("provider", "")).strip().lower()
            if not model_id or not provider:
                continue
            normalized.append(
                {
                    "model_id": model_id,
                    "provider": provider,
                    "base_quality": float(item.get("quality_score", 0) or 0),
                    "scores": [],
                    "eliminated": False,
                }
            )
        normalized.sort(key=lambda x: (-float(x.get("base_quality", 0.0)), str(x.get("model_id", ""))))
        return normalized[:3]

    async def _run_weekly_model_competition(self) -> None:
        """주간 모델 경쟁 상태를 갱신한다 (shadow -> champion_ops)."""
        if not self.job_store:
            return

        now_local = self._get_now_local()
        week_start = self._week_start_local(now_local).isoformat()
        apply_at = self._next_week_apply_at_local_iso(self._week_start_local(now_local))
        state = self.job_store.get_weekly_competition_state(week_start)

        if state is None:
            candidates = self._build_competition_candidates()
            if not candidates:
                logger.info("Weekly competition skipped: no candidates")
                return
            champion_model = str(candidates[0].get("model_id", "")).strip()
            challenger_model = str(candidates[1].get("model_id", "")).strip() if len(candidates) > 1 else ""
            self.job_store.upsert_weekly_competition_state(
                week_start=week_start,
                phase="testing",
                candidates=candidates,
                champion_model=champion_model,
                challenger_model=challenger_model,
                early_terminated=False,
                apply_at=apply_at,
            )
            self.job_store.set_system_setting("router_competition_phase", "testing")
            self.job_store.set_system_setting("router_competition_week_start", week_start)
            self.job_store.set_system_setting("router_competition_apply_at", apply_at)
            self.job_store.set_system_setting("router_shadow_mode", "true")
            self.job_store.set_system_setting("router_champion_model", champion_model)
            self.job_store.set_system_setting("router_challenger_model", challenger_model)
            logger.info(
                "Weekly competition initialized",
                extra={
                    "week_start": week_start,
                    "phase": "testing",
                    "champion_model": champion_model,
                    "challenger_model": challenger_model,
                },
            )
            return

        current_phase = str(state.get("phase", "testing")).strip().lower()
        if current_phase != "testing":
            return
        if now_local.weekday() < self.WEEKLY_COMPETITION_TEST_END_WEEKDAY:
            return

        summary = self.job_store.get_model_performance_summary(
            since=f"{week_start}T00:00:00Z",
            slot_types=["shadow", "challenger", "main"],
        )
        by_model = {str(item.get("model_id", "")): item for item in summary}
        candidates = list(state.get("candidates", []))
        ranked: List[Dict[str, Any]] = []
        for candidate in candidates:
            model_id = str(candidate.get("model_id", "")).strip()
            if not model_id:
                continue
            perf = by_model.get(model_id, {})
            avg_quality = float(perf.get("avg_quality_score", candidate.get("base_quality", 0.0)) or 0.0)
            samples = int(perf.get("samples", 0) or 0)
            avg_cost = float(perf.get("avg_cost_won", 0.0) or 0.0)
            ranked.append(
                {
                    "model_id": model_id,
                    "provider": str(candidate.get("provider", "")).strip().lower(),
                    "avg_quality_score": avg_quality,
                    "samples": samples,
                    "avg_cost_won": avg_cost,
                }
            )
        if not ranked:
            return

        ranked.sort(
            key=lambda x: (
                -float(x.get("avg_quality_score", 0.0)),
                -int(x.get("samples", 0)),
                float(x.get("avg_cost_won", 0.0)),
            )
        )
        champion = ranked[0]
        challenger = ranked[1] if len(ranked) > 1 else None

        champion_model = str(champion.get("model_id", "")).strip()
        challenger_model = str(challenger.get("model_id", "")).strip() if challenger else ""

        self.job_store.upsert_weekly_competition_state(
            week_start=week_start,
            phase="champion_ops",
            candidates=candidates,
            champion_model=champion_model,
            challenger_model=challenger_model,
            early_terminated=False,
            apply_at=apply_at,
        )
        self.job_store.record_champion_history(
            week_start=week_start,
            champion_model=champion_model,
            challenger_model=challenger_model,
            avg_champion_score=float(champion.get("avg_quality_score", 0.0)),
            topic_mode_scores={},
            cost_won=float(champion.get("avg_cost_won", 0.0)),
            early_terminated=False,
            shadow_only=True,
        )
        self.job_store.set_system_setting("router_competition_phase", "champion_ops")
        self.job_store.set_system_setting("router_competition_week_start", week_start)
        self.job_store.set_system_setting("router_competition_apply_at", apply_at)
        self.job_store.set_system_setting("router_shadow_mode", "false")
        self.job_store.set_system_setting("router_champion_model", champion_model)
        self.job_store.set_system_setting("router_challenger_model", challenger_model)

        if self.notifier and getattr(self.notifier, "enabled", False):
            message = (
                "📢 챔피언 모델 갱신\n"
                f"• week_start: {week_start}\n"
                f"• champion: {champion_model} ({float(champion.get('avg_quality_score', 0.0)):.1f}점)\n"
                f"• challenger: {challenger_model or '-'}\n"
                f"• apply_at: {apply_at}"
            )
            send_background = getattr(self.notifier, "send_message_background", None)
            if callable(send_background):
                send_background(message, disable_notification=False)

        logger.info(
            "Weekly competition promoted to champion_ops",
            extra={
                "week_start": week_start,
                "champion_model": champion_model,
                "challenger_model": challenger_model,
            },
        )

    def _get_configured_daily_target(self) -> int:
        """DB 설정을 포함한 일간 목표 발행량을 반환한다."""
        default_target = max(1, self.daily_posts_target or self.DEFAULT_DAILY_TARGET)
        if not self.job_store:
            return default_target
        get_setting = getattr(self.job_store, "get_system_setting", None)
        if not get_setting or not callable(get_setting):
            return default_target
        raw = str(get_setting("scheduler_daily_posts_target", "")).strip()
        if not raw:
            return default_target
        try:
            value = int(raw)
        except ValueError:
            return default_target
        return max(1, min(20, value))

    def _load_daily_quota_allocations(self) -> tuple[int, List[Dict[str, Any]]]:
        """DB에서 카테고리 할당량 설정을 읽어 정규화한다."""
        daily_target = self._get_configured_daily_target()
        if not self.job_store:
            return daily_target, []
        get_setting = getattr(self.job_store, "get_system_setting", None)
        if not get_setting or not callable(get_setting):
            return daily_target, self._build_default_quota_allocations(daily_target)

        raw_allocations = str(get_setting("scheduler_category_allocations", "")).strip()
        allocations: List[Dict[str, Any]] = []
        if raw_allocations:
            try:
                decoded = json.loads(raw_allocations)
                if isinstance(decoded, list):
                    for item in decoded:
                        if not isinstance(item, dict):
                            continue
                        category_name = str(item.get("category", "")).strip()
                        topic_mode = self._normalize_topic_mode(str(item.get("topic_mode", "")).strip())
                        count = max(0, int(item.get("count", 0)))
                        if not category_name:
                            continue
                        allocations.append(
                            {
                                "category": category_name,
                                "topic_mode": topic_mode,
                                "count": count,
                            }
                        )
            except Exception:
                allocations = []

        if not allocations:
            allocations = self._build_default_quota_allocations(daily_target)

        total = sum(int(item.get("count", 0)) for item in allocations)
        if total <= 0:
            allocations = self._build_default_quota_allocations(daily_target)

        return daily_target, allocations


    def _get_configured_idea_vault_quota(self, daily_target: int) -> int:
        """일간 아이디어 창고 할당량을 반환한다."""
        default_quota = min(
            max(0, int(daily_target)),
            self.DEFAULT_IDEA_VAULT_DAILY_QUOTA,
        )
        if not self.job_store:
            return default_quota
        get_setting = getattr(self.job_store, "get_system_setting", None)
        if not get_setting or not callable(get_setting):
            return default_quota
        raw = str(get_setting("scheduler_idea_vault_daily_quota", "")).strip()
        if not raw:
            return default_quota
        try:
            value = int(raw)
        except ValueError:
            return default_quota
        return max(0, min(max(0, int(daily_target)), value))

    def _get_idea_vault_pending_count(self) -> int:
        """아이디어 창고 pending 재고를 반환한다."""
        if not self.job_store:
            return 0
        getter = getattr(self.job_store, "get_idea_vault_pending_count", None)
        if not getter or not callable(getter):
            return 0
        try:
            return max(0, int(getter()))
        except Exception:
            return 0

    def _build_default_quota_allocations(self, daily_target: int) -> List[Dict[str, Any]]:
        """설정이 없을 때 기본 할당량을 만든다."""
        if not self.job_store:
            return [
                {
                    "category": DEFAULT_FALLBACK_CATEGORY,
                    "topic_mode": "cafe",
                    "count": max(1, daily_target),
                }
            ]

        get_setting = getattr(self.job_store, "get_system_setting", None)
        if not get_setting or not callable(get_setting):
            return [
                {
                    "category": DEFAULT_FALLBACK_CATEGORY,
                    "topic_mode": "cafe",
                    "count": max(1, daily_target),
                }
            ]

        raw_categories = str(get_setting("custom_categories", "[]"))
        categories: List[str] = []
        try:
            decoded = json.loads(raw_categories)
            if isinstance(decoded, list):
                for item in decoded:
                    text = str(item).strip()
                    if text and text not in categories:
                        categories.append(text)
        except Exception:
            categories = []

        if not categories:
            # DB에 저장된 fallback_category를 우선 사용하고, 없으면 전역 상수 사용
            saved_fallback = str(get_setting("fallback_category", "")).strip()
            categories = [saved_fallback if saved_fallback else DEFAULT_FALLBACK_CATEGORY]

        allocations = [
            {
                "category": category_name,
                "topic_mode": self._infer_topic_mode_from_category(category_name),
                "count": 0,
            }
            for category_name in categories
        ]
        for index in range(max(1, daily_target)):
            allocations[index % len(allocations)]["count"] = int(allocations[index % len(allocations)]["count"]) + 1
        return allocations

    def _normalize_topic_mode(self, raw_mode: str) -> str:
        """토픽 모드를 허용 범위로 정규화한다."""
        lowered = raw_mode.lower().strip()
        if lowered == "economy":
            return "finance"
        if lowered in {"cafe", "it", "parenting", "finance"}:
            return lowered
        return "cafe"

    def _infer_topic_mode_from_category(self, category_name: str) -> str:
        """카테고리 문자열 기반 토픽 모드를 추정한다."""
        lowered = str(category_name).lower()
        if any(token in lowered for token in ("경제", "finance", "투자", "주식", "재테크")):
            return "finance"
        if any(token in lowered for token in ("it", "개발", "코드", "ai", "자동화", "테크")):
            return "it"
        if any(token in lowered for token in ("육아", "아이", "부모", "가정")):
            return "parenting"
        return "cafe"

    def _persona_id_for_topic(self, topic_mode: str) -> str:
        """토픽 모드별 기본 페르소나를 반환한다."""
        mapping = {
            "cafe": "P1",
            "it": "P2",
            "parenting": "P3",
            "finance": "P4",
        }
        return mapping.get(self._normalize_topic_mode(topic_mode), "P1")

    def _build_seed_title(
        self,
        *,
        category: str,
        topic_mode: str,
        local_date: str,
        sequence: int,
    ) -> str:
        """자정 큐 시드용 제목을 생성한다."""
        label = {
            "cafe": "라이프",
            "it": "IT",
            "parenting": "육아",
            "finance": "경제",
        }.get(self._normalize_topic_mode(topic_mode), "라이프")
        return f"{local_date} {label} 브리핑 #{sequence} - {category}"

    def _build_seed_keywords(self, category: str, topic_mode: str) -> List[str]:
        """자정 큐 시드용 키워드를 생성한다."""
        base_keywords = {
            "cafe": ["일상", "노하우", "리뷰"],
            "it": ["IT", "자동화", "생산성"],
            "parenting": ["육아", "가정", "성장"],
            "finance": ["경제", "재테크", "투자"],
        }.get(self._normalize_topic_mode(topic_mode), ["일상", "정보"])

        category_token = str(category).strip()
        keywords = [category_token] if category_token else []
        for token in base_keywords:
            if token not in keywords:
                keywords.append(token)
        return keywords[:3]

    def _build_vault_seed_title(
        self,
        *,
        raw_text: str,
        local_date: str,
        sequence: int,
    ) -> str:
        """아이디어 창고 시드용 제목을 생성한다."""
        normalized = re.sub(r"\s+", " ", str(raw_text).strip())
        if not normalized:
            normalized = "아이디어 메모"
        if len(normalized) > 42:
            normalized = f"{normalized[:42].rstrip()}..."
        return f"{local_date} 아이디어 브리핑 #{sequence} - {normalized}"

    def _build_vault_seed_keywords(
        self,
        *,
        raw_text: str,
        category: str,
        topic_mode: str,
    ) -> List[str]:
        """아이디어 문장에서 시드 키워드를 생성한다."""
        keywords: List[str] = []
        category_name = str(category).strip()
        if category_name:
            keywords.append(category_name)

        tokens = re.findall(r"[가-힣A-Za-z0-9]{2,20}", str(raw_text))
        for token in tokens:
            normalized = token.strip()
            if not normalized or normalized in keywords:
                continue
            keywords.append(normalized)
            if len(keywords) >= 3:
                break

        if len(keywords) < 2:
            fallback = self._build_seed_keywords(category=category, topic_mode=topic_mode)
            for token in fallback:
                if token not in keywords:
                    keywords.append(token)
                if len(keywords) >= 3:
                    break
        return keywords[:3]

    def _build_rng_for_date(self, target_date: date) -> random.Random:
        """날짜 단위 고정 시드를 생성한다."""
        if self.random_seed is None:
            return random.Random()
        return random.Random(f"{self.random_seed}:{target_date.isoformat()}")

    def _get_memory_percent(self) -> float:
        """메모리 사용률을 퍼센트로 반환한다."""
        try:
            import psutil  # type: ignore[import-untyped]

            return float(psutil.virtual_memory().percent)
        except Exception:
            return 0.0

    def _get_ready_draft_count(self) -> int:
        """현재 ready 상태 초안 개수를 조회한다."""
        if not self.job_store:
            return 0
        get_count = getattr(self.job_store, "get_ready_to_publish_count", None)
        if get_count and callable(get_count):
            try:
                return int(get_count(job_kind=self._master_job_kind()))
            except TypeError:
                return int(get_count())
        return 0

    def get_ready_draft_count(self) -> int:
        """외부 호출용으로 현재 ready 초안 개수를 조회한다."""
        return self._get_ready_draft_count()

    def _get_ready_count(self, job_kind: Optional[str] = None) -> int:
        """지정한 잡 kind의 ready 상태 개수를 반환한다."""
        if not self.job_store:
            return 0
        get_count = getattr(self.job_store, "get_ready_to_publish_count", None)
        if get_count and callable(get_count):
            try:
                return int(get_count(job_kind=job_kind))
            except TypeError:
                return int(get_count())
        return 0

    def _get_due_count(self, job_kind: Optional[str] = None) -> int:
        """지정한 잡 kind의 실행 가능(queued/retry_wait) 개수를 반환한다."""
        if not self.job_store:
            return 0

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        normalized_kind = str(job_kind or "").strip().lower()
        with self.job_store.connection() as conn:
            if normalized_kind:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE job_kind = ?
                      AND (
                        (status = 'queued' AND scheduled_at <= ?)
                        OR
                        (status = 'retry_wait' AND next_retry_at <= ?)
                      )
                    """,
                    (normalized_kind, now, now),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE (status = 'queued' AND scheduled_at <= ?)
                       OR (status = 'retry_wait' AND next_retry_at <= ?)
                    """,
                    (now, now),
                ).fetchone()
        return int(row["total"] or 0) if row else 0

    def _log_sub_job_catchup_stats(
        self,
        *,
        created_count: int,
        scanned_pairs: int,
        master_count: int,
        sub_channel_count: int,
        skip_reasons: Counter[str],
        started_at: float,
    ) -> None:
        """서브 잡 catch-up 실행 통계를 로그로 남긴다."""
        elapsed_sec = max(0.001, perf_counter() - started_at)
        throughput_per_min = created_count * 60.0 / elapsed_sec
        scan_per_min = scanned_pairs * 60.0 / elapsed_sec
        logger.info(
            "Sub job catch-up stats: masters=%d sub_channels=%d scanned_pairs=%d created=%d throughput_per_min=%.2f scan_per_min=%.2f skip_reasons=%s elapsed_sec=%.2f",
            master_count,
            sub_channel_count,
            scanned_pairs,
            created_count,
            throughput_per_min,
            scan_per_min,
            json.dumps(dict(skip_reasons), ensure_ascii=False, sort_keys=True),
            elapsed_sec,
        )

    def _get_now_local(self) -> datetime:
        """로컬 타임존 시각을 반환한다."""
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.timezone_name))
        except Exception:
            return datetime.now()

    def _today_key(self) -> str:
        """로컬 날짜 키(YYYY-MM-DD)를 반환한다."""
        return self._get_now_local().strftime("%Y-%m-%d")

    def _week_key(self) -> str:
        """로컬 주차 키(YYYY-Www)를 반환한다."""
        local_now = self._get_now_local()
        week_start = local_now - timedelta(days=local_now.weekday())
        return week_start.strftime("%Y-W%W")

    def _is_multichannel_enabled(self) -> bool:
        """멀티채널 기능 플래그를 반환한다."""
        if not self.job_store:
            return False
        raw = str(
            self.job_store.get_system_setting(_MULTICHANNEL_SETTING_KEY, "false")
        ).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _master_job_kind(self) -> str:
        """마스터 잡 kind 상수를 안전하게 반환한다."""
        if not self.job_store:
            return "master"
        return str(getattr(self.job_store, "JOB_KIND_MASTER", "master"))

    def _sub_job_kind(self) -> str:
        """서브 잡 kind 상수를 안전하게 반환한다."""
        if not self.job_store:
            return "sub"
        return str(getattr(self.job_store, "JOB_KIND_SUB", "sub"))

    def _get_today_post_count(self) -> int:
        if not self.job_store:
            return 0
        try:
            return self.job_store.get_today_completed_count(
                job_kind=self._master_job_kind()
            )
        except TypeError:
            return self.job_store.get_today_completed_count()

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

    def _get_last_post_time(self) -> Optional[datetime]:
        if not self.job_store:
            return None
        try:
            return self.job_store.get_last_completed_time(
                job_kind=self._master_job_kind()
            )
        except TypeError:
            return self.job_store.get_last_completed_time()

    def _get_today_failed_count(self) -> int:
        if not self.job_store:
            return 0
        get_count = getattr(self.job_store, "get_today_failed_count", None)
        if get_count and callable(get_count):
            try:
                return int(get_count(job_kind=self._master_job_kind()))
            except TypeError:
                return int(get_count())
        return 0


async def run_scheduler_forever(
    daily_posts_target: Optional[int] = None,
    db_path: Optional[str] = None,
    min_post_interval_minutes: int = 60,
    publish_interval_max_minutes: int = 110,
    cpu_start_threshold_percent: float = 28.0,
    cpu_stop_threshold_percent: float = 35.0,
    cpu_avg_window: int = 5,
    memory_threshold_percent: float = 80.0,
    generator_poll_seconds: int = SchedulerService.DEFAULT_GENERATOR_POLL_SECONDS,
    publisher_poll_seconds: int = SchedulerService.DEFAULT_PUBLISHER_POLL_SECONDS,
    random_seed: Optional[int] = None,
) -> None:
    """스케줄러를 시작하고 종료 신호까지 대기한다."""
    from ..collectors.metrics_collector import MetricsCollector
    from ..config import load_config
    from ..images.runtime_factory import build_runtime_image_generator
    from ..llm import get_generator, llm_generate_fn
    from ..logging_config import setup_logging
    from ..uploaders.playwright_publisher import PlaywrightPublisher
    from .job_store import JobStore
    from .notifier import TelegramNotifier
    from .pipeline_service import PipelineService, stub_generate_fn
    from .trend_job_service import TrendJobService

    import os

    config = load_config()
    setup_logging(level=config.logging.level, log_format=config.logging.format)

    resolved_db_path = db_path or os.getenv("AUTOBLOG_DB_PATH", "data/automation.db")
    resolved_daily_posts_target = (
        int(daily_posts_target)
        if daily_posts_target is not None
        else SchedulerService.DEFAULT_DAILY_TARGET
    )
    if resolved_daily_posts_target < 1:
        resolved_daily_posts_target = SchedulerService.DEFAULT_DAILY_TARGET

    job_store = JobStore(db_path=resolved_db_path)
    trend_service = TrendJobService(job_store=job_store)
    metrics_collector = MetricsCollector(db_path=job_store.db_path)

    dry_run = False
    blog_id = ""

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    blog_id = os.getenv("NAVER_BLOG_ID", "")

    if dry_run and not blog_id:
        blog_id = "dry-run"

    notifier = TelegramNotifier.from_env()
    publisher = PlaywrightPublisher(blog_id=blog_id or "dry-run")
    image_generator = None
    try:
        image_generator = build_runtime_image_generator(
            app_config=config,
            job_store=job_store,
        )
        if image_generator:
            logger.info("Scheduler image backend: router-driven runtime factory")
    except Exception as exc:
        logger.warning("Scheduler image runtime init failed: %s", exc)

    generate_fn = stub_generate_fn
    try:
        # 스케줄러는 LLM 생성기를 기본 사용하되, 초기화 실패 시 stub로 안전 폴백한다.
        get_generator(
            config.llm,
            job_store=job_store,
            notifier=notifier,
        )
        generate_fn = llm_generate_fn
        logger.info("Scheduler generation backend: llm")
    except Exception as exc:
        logger.warning("Scheduler LLM init failed, fallback to stub: %s", exc)

    quality_evaluator = None
    feedback_analyzer = None
    try:
        from ..llm.provider_factory import create_client
        from .quality_evaluator import QualityEvaluator
        eval_client = create_client(
            provider=config.llm.primary_provider,
            model=config.llm.primary_model,
            timeout_sec=config.llm.timeout_sec,
        )
        quality_evaluator = QualityEvaluator(llm_client=eval_client)
        logger.info("Scheduler quality evaluator backend: llm")
    except Exception as exc:
        logger.warning("Scheduler quality evaluator init failed: %s", exc)

    try:
        from ..seo.feedback_analyzer import FeedbackAnalyzer
        # FeedbackAnalyzer용 LLM 클라이언트: 분석 전용이므로 동일 프로바이더 사용
        feedback_llm_client = None
        try:
            from ..llm.provider_factory import create_client as _create_fb_client
            feedback_llm_client = _create_fb_client(
                provider=config.llm.primary_provider,
                model=config.llm.primary_model,
                timeout_sec=config.llm.timeout_sec,
            )
        except Exception:
            pass
        feedback_analyzer = FeedbackAnalyzer(
            db_path=job_store.db_path,
            llm_client=feedback_llm_client,
        )
        logger.info("Scheduler feedback analyzer: enabled")
    except Exception as exc:
        logger.warning("Scheduler feedback analyzer init failed: %s", exc)

    pipeline_service = PipelineService(
        job_store=job_store,
        publisher=publisher,
        generate_fn=generate_fn,
        notifier=notifier,
        internal_retry_attempts=1,
        queue_retry_limit=1,
        retry_max_attempts=config.retry.max_retries,
        retry_backoff_base_sec=config.retry.backoff_base_sec,
        retry_backoff_max_sec=config.retry.backoff_max_sec,
        image_generator=image_generator,
        quality_evaluator=quality_evaluator,
    )

    scheduler = SchedulerService(
        trend_service=trend_service,
        pipeline_service=pipeline_service,
        metrics_collector=metrics_collector,
        feedback_analyzer=feedback_analyzer,
        job_store=job_store,
        timezone_name="Asia/Seoul",
        daily_posts_target=resolved_daily_posts_target,
        min_post_interval_minutes=min_post_interval_minutes,
        publish_interval_min_minutes=min_post_interval_minutes,
        publish_interval_max_minutes=publish_interval_max_minutes,
        cpu_start_threshold_percent=cpu_start_threshold_percent,
        cpu_stop_threshold_percent=cpu_stop_threshold_percent,
        cpu_avg_window=cpu_avg_window,
        memory_threshold_percent=memory_threshold_percent,
        generator_poll_seconds=generator_poll_seconds,
        publisher_poll_seconds=publisher_poll_seconds,
        random_seed=random_seed,
        notifier=notifier,
    )
    await scheduler.start()

    try:
        while True:
            await asyncio.sleep(constants.SCHEDULER_DAEMON_KEEPALIVE_SEC)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await scheduler.stop()
    finally:
        if image_generator:
            close_fn = getattr(image_generator, "close", None)
            if close_fn and callable(close_fn):
                try:
                    await close_fn()
                except Exception:
                    pass
