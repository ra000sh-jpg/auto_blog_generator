"""APScheduler 기반 자동화 스케줄러."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time as time_obj, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..collectors.metrics_collector import MetricsCollector
    from ..seo.feedback_analyzer import FeedbackAnalyzer
    from .job_store import JobStore
    from .notifier import TelegramNotifier
    from .pipeline_service import PipelineService
    from .trend_job_service import TrendJobService

from .resource_monitor import CpuHysteresisMonitor

logger = logging.getLogger(__name__)


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
    ACTIVE_HOURS = (8, 22)  # 08:00~22:00
    PUBLISH_ANCHOR_HOURS = (9, 12, 19)
    DRAFT_BUFFER_TARGET = 6
    DEFAULT_GENERATOR_POLL_SECONDS = 30
    DEFAULT_PUBLISHER_POLL_SECONDS = 20

    def __init__(
        self,
        trend_service: Optional["TrendJobService"] = None,
        pipeline_service: Optional["PipelineService"] = None,
        metrics_collector: Optional["MetricsCollector"] = None,
        feedback_analyzer: Optional["FeedbackAnalyzer"] = None,
        job_store: Optional["JobStore"] = None,
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
    ):
        self.trend_service = trend_service
        self.pipeline_service = pipeline_service
        self.metrics_collector = metrics_collector
        self.feedback_analyzer = feedback_analyzer
        self.job_store = job_store
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

        logger.info("Scheduler setup complete")

    async def start(self) -> None:
        """스케줄러를 시작하고 시작 직후 catch-up을 수행한다."""
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
        try:
            while True:
                try:
                    await self._run_draft_prefetch()
                except Exception as exc:
                    logger.error("Generator worker error: %s", exc)
                await asyncio.sleep(self.generator_poll_seconds)
        except asyncio.CancelledError:
            logger.info("Generator worker stopped")

    async def _publisher_worker_loop(self) -> None:
        """시간 분포 기반 발행 워커 루프."""
        try:
            while True:
                try:
                    await self._run_daily_target_check()
                except Exception as exc:
                    logger.error("Publisher worker error: %s", exc)
                await asyncio.sleep(self.publisher_poll_seconds)
        except asyncio.CancelledError:
            logger.info("Publisher worker stopped")

    async def _run_startup_catchup(self) -> None:
        """시작 시점에 놓친 작업을 보정 실행한다."""
        logger.info("Running startup catch-up")
        await self._run_draft_prefetch()
        await self._run_daily_target_check()

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
        try:
            count = await self.metrics_collector.collect_all_pending()
            logger.info("Metrics collected: %d", count)
        except Exception as exc:
            logger.error("Metrics collection failed: %s", exc)

    async def _run_feedback_analysis(self) -> None:
        logger.info("Running feedback analysis")
        if not self.feedback_analyzer:
            return
        try:
            snapshot = await self.feedback_analyzer.run_analysis(
                platform="naver",
                trigger="scheduled",
                apply_updates=True,
            )
            if snapshot:
                logger.info("Feedback analysis complete")
        except Exception as exc:
            logger.error("Feedback analysis failed: %s", exc)

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

        try:
            sent = await self.notifier.notify_daily_summary(
                local_date=now_local.strftime("%Y-%m-%d"),
                target=self.daily_posts_target,
                completed=completed,
                failed=failed,
                ready_count=ready_count,
                queued_count=queued_count,
            )
            if sent:
                self._last_daily_summary_date = now_local.date()
        except Exception as exc:
            logger.warning("Daily summary notify failed: %s", exc)

    async def _run_daily_target_check(self) -> None:
        """일일 목표 기반으로 준비된 초안을 1건씩 천천히 발행한다."""
        now_local = self._get_now_local()
        self._ensure_daily_publish_slots(now_local.date())

        if not (self.ACTIVE_HOURS[0] <= now_local.hour < self.ACTIVE_HOURS[1]):
            logger.debug("Outside active hours: %s", now_local)
            return

        today_completed = self._get_today_post_count()
        remaining = self.daily_posts_target - today_completed
        if remaining <= 0:
            logger.debug("Daily target reached: %d/%d", today_completed, self.daily_posts_target)
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
                self.daily_posts_target,
            )
            return

        logger.info(
            "Daily target check passed (%d/%d), publishing prepared draft",
            today_completed,
            self.daily_posts_target,
        )
        try:
            published = await self._publish_next_available_job()
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
        """리소스 여유 시 오늘 목표치만큼 초안을 선생성한다."""
        if not self.pipeline_service:
            return

        ready_count = self._get_ready_draft_count()
        needed = max(0, self.DRAFT_BUFFER_TARGET - ready_count)
        if needed <= 0:
            logger.debug(
                "Draft buffer is enough (%d ready, target=%d)",
                ready_count,
                self.DRAFT_BUFFER_TARGET,
            )
            return

        if not self._has_resource_headroom():
            logger.info("Skip draft prefetch due to high resource usage or paused state")
            return

        logger.info(
            "Draft prefetch start (need=%d, ready=%d, target=%d)",
            needed,
            ready_count,
            self.DRAFT_BUFFER_TARGET,
        )
        prepared_count = 0
        for _ in range(needed):
            if not self._has_resource_headroom():
                break
            prepared = await self._prepare_next_available_job()
            if not prepared:
                break
            prepared_count += 1
            await asyncio.sleep(0.3)

        logger.info("Draft prefetch done (prepared=%d, needed=%d)", prepared_count, needed)

    async def _prepare_next_available_job(self) -> bool:
        """파이프라인이 지원하는 방식으로 다음 초안을 생성한다."""
        prepare_fn = getattr(self.pipeline_service, "prepare_next_pending_job", None)
        if prepare_fn and callable(prepare_fn):
            return bool(await prepare_fn())
        return False

    async def _publish_next_available_job(self) -> bool:
        """파이프라인이 지원하는 방식으로 다음 발행을 실행한다."""
        publish_fn = getattr(self.pipeline_service, "publish_next_ready_job", None)
        if publish_fn and callable(publish_fn):
            return bool(await publish_fn())
        run_fn = getattr(self.pipeline_service, "run_next_pending_job", None)
        if run_fn and callable(run_fn):
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

    def _ensure_daily_publish_slots(self, target_date: date) -> None:
        """당일 발행 슬롯을 준비한다."""
        if self._publish_slot_date == target_date and self._daily_publish_slots:
            return

        self._daily_publish_slots = self._build_daily_publish_slots(target_date)
        self._publish_slot_date = target_date
        self._publish_wait_until_utc = None
        logger.info(
            "Daily weighted publish slots generated",
            extra={
                "date": target_date.isoformat(),
                "slots": [slot.isoformat() for slot in self._daily_publish_slots],
            },
        )

    def _build_daily_publish_slots(self, target_date: date) -> List[datetime]:
        """출/점/퇴 중심 가중 분포로 하루 발행 슬롯을 만든다."""
        local_tz = self._get_now_local().tzinfo
        if local_tz is None:
            local_tz = timezone(timedelta(hours=9))

        rng = self._build_rng_for_date(target_date)
        candidates: List[datetime] = []

        for index in range(self.daily_posts_target):
            if index < len(self.PUBLISH_ANCHOR_HOURS):
                base_hour = self.PUBLISH_ANCHOR_HOURS[index]
            else:
                base_hour = rng.choices(
                    population=list(self.PUBLISH_ANCHOR_HOURS),
                    weights=[0.45, 0.35, 0.20],
                    k=1,
                )[0]

            base_time = datetime.combine(
                target_date,
                time_obj(hour=base_hour, minute=0),
                tzinfo=local_tz,
            )
            jitter_minutes = int(rng.gauss(0, 25))
            jitter_minutes = max(-45, min(55, jitter_minutes))
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
            return int(get_count())
        return 0

    def _get_now_local(self) -> datetime:
        """로컬 타임존 시각을 반환한다."""
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.timezone_name))
        except Exception:
            return datetime.now()

    def _get_today_post_count(self) -> int:
        if not self.job_store:
            return 0
        return self.job_store.get_today_completed_count()

    def _get_last_post_time(self) -> Optional[datetime]:
        if not self.job_store:
            return None
        return self.job_store.get_last_completed_time()

    def _get_today_failed_count(self) -> int:
        if not self.job_store:
            return 0
        get_count = getattr(self.job_store, "get_today_failed_count", None)
        if get_count and callable(get_count):
            return int(get_count())
        return 0


async def run_scheduler_forever(
    daily_posts_target: int = 3,
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
    from ..logging_config import setup_logging
    from ..uploaders.playwright_publisher import PlaywrightPublisher
    from .job_store import JobStore
    from .notifier import TelegramNotifier
    from .pipeline_service import PipelineService, stub_generate_fn
    from .trend_job_service import TrendJobService

    config = load_config()
    setup_logging(level=config.logging.level, log_format=config.logging.format)

    job_store = JobStore()
    trend_service = TrendJobService(job_store=job_store)
    metrics_collector = MetricsCollector(db_path=job_store.db_path)

    dry_run = False
    blog_id = ""
    import os

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    blog_id = os.getenv("NAVER_BLOG_ID", "")

    if dry_run and not blog_id:
        blog_id = "dry-run"

    notifier = TelegramNotifier.from_env()
    publisher = PlaywrightPublisher(blog_id=blog_id or "dry-run")
    pipeline_service = PipelineService(
        job_store=job_store,
        publisher=publisher,
        generate_fn=stub_generate_fn,
        notifier=notifier,
        internal_retry_attempts=1,
        queue_retry_limit=1,
        retry_max_attempts=config.retry.max_retries,
        retry_backoff_base_sec=config.retry.backoff_base_sec,
        retry_backoff_max_sec=config.retry.backoff_max_sec,
    )

    scheduler = SchedulerService(
        trend_service=trend_service,
        pipeline_service=pipeline_service,
        metrics_collector=metrics_collector,
        feedback_analyzer=None,
        job_store=job_store,
        timezone_name="Asia/Seoul",
        daily_posts_target=daily_posts_target,
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
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await scheduler.stop()
