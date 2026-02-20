import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.scheduler_service import SchedulerService
from modules.automation.trend_job_service import CATEGORY_TO_TOPIC, TrendJobService
from modules.automation.time_utils import now_utc
from modules.uploaders.playwright_publisher import PublishResult


def build_store(tmp_path: Path, name: str = "scheduler_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


class DummyPublisher:
    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/1")


def test_trend_job_service_creates_jobs(tmp_path: Path):
    store = build_store(tmp_path)
    service = TrendJobService(job_store=store, max_jobs_per_run=2)

    class MockCollector:
        def fetch_trending_keywords(self, category_name: str, count: int) -> List[str]:
            del category_name, count
            return ["테스트키워드1", "테스트키워드2"]

    service.collector = MockCollector()  # type: ignore[assignment]
    job_ids = service.fetch_and_create_jobs(categories=["디지털/가전"])
    assert len(job_ids) == 2
    assert all(store.get_job(job_id) is not None for job_id in job_ids)


def test_category_to_topic_mapping():
    assert CATEGORY_TO_TOPIC["출산/육아"] == "parenting"
    assert CATEGORY_TO_TOPIC["디지털/가전"] == "it"
    assert CATEGORY_TO_TOPIC["생활/건강"] == "cafe"


def test_scheduler_service_setup():
    scheduler = SchedulerService(daily_posts_target=3, min_post_interval_minutes=60)
    scheduler.setup_scheduler()
    assert scheduler._scheduler is not None
    assert scheduler.daily_posts_target == 3
    assert scheduler.min_post_interval_minutes == 60


def test_scheduler_misfire_grace_time():
    scheduler = SchedulerService()
    assert scheduler.MISFIRE_GRACE_TIME == 86400


def test_daily_target_check_outside_active_hours():
    calls: Dict[str, int] = {"count": 0}

    @dataclass
    class PipelineStub:
        async def run_next_pending_job(self) -> bool:
            calls["count"] += 1
            return True

    scheduler = SchedulerService(
        pipeline_service=PipelineStub(),  # type: ignore[arg-type]
        daily_posts_target=3,
    )
    scheduler._get_now_local = lambda: datetime(2026, 2, 21, 3, 0, 0)  # type: ignore[assignment]

    asyncio.run(scheduler._run_daily_target_check())
    assert calls["count"] == 0


def test_post_interval_check():
    calls: Dict[str, int] = {"count": 0}

    @dataclass
    class PipelineStub:
        async def run_next_pending_job(self) -> bool:
            calls["count"] += 1
            return True

    @dataclass
    class JobStoreStub:
        def get_today_completed_count(self) -> int:
            return 0

        def get_last_completed_time(self) -> Optional[datetime]:
            return datetime.now(timezone.utc) - timedelta(minutes=30)

    scheduler = SchedulerService(
        pipeline_service=PipelineStub(),  # type: ignore[arg-type]
        job_store=JobStoreStub(),  # type: ignore[arg-type]
        min_post_interval_minutes=60,
    )
    scheduler._get_now_local = lambda: datetime(2026, 2, 21, 10, 0, 0)  # type: ignore[assignment]

    asyncio.run(scheduler._run_daily_target_check())
    assert calls["count"] == 0


def test_pipeline_run_next_pending_job(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-pending-job",
        title="Scheduler Pending Job",
        seed_keywords=["scheduler", "pending"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )

    async def generate_fn(_job) -> Dict[str, Any]:
        long_body = ("scheduler pending 스케줄러 테스트 본문입니다. " * 60).strip()
        return {
            "final_content": long_body,
            "quality_gate": "pass",
            "quality_snapshot": {"score": 90, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
            "image_prompts": [],
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
    )

    executed = asyncio.run(pipeline.run_next_pending_job())
    assert executed is True

    updated = store.get_job("scheduler-pending-job")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED


def test_jobstore_today_count_and_last_completed_time(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-completed-job",
        title="Completed Job",
        seed_keywords=["completed"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.complete_job(
        job_id="scheduler-completed-job",
        result_url="https://blog.naver.com/test/completed",
    )

    assert store.get_today_completed_count() >= 1
    assert store.get_last_completed_time() is not None


def test_jobstore_ready_claim_flow(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-ready-job",
        title="Ready Job",
        seed_keywords=["ready"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.save_prepared_payload(
        "scheduler-ready-job",
        {"title": "Ready Job", "content": "초안 본문", "images": [], "image_points": []},
    )
    assert store.get_ready_to_publish_count() == 1

    publish_claimed = store.claim_ready_jobs(limit=1, now_override=due_now)
    assert len(publish_claimed) == 1
    assert publish_claimed[0].job_id == "scheduler-ready-job"
    assert publish_claimed[0].prepared_payload.get("content") == "초안 본문"


def test_pipeline_prepare_then_publish_ready_job(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-prepare-publish-job",
        title="Prepare Publish Job",
        seed_keywords=["prepare", "publish"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )

    async def generate_fn(_job) -> Dict[str, Any]:
        long_body = ("prepare publish 준비 발행 테스트 본문입니다. " * 60).strip()
        return {
            "final_content": long_body,
            "quality_gate": "pass",
            "quality_snapshot": {"score": 88, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
            "image_prompts": [],
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
    )

    prepared = asyncio.run(pipeline.prepare_next_pending_job())
    assert prepared is True
    ready_job = store.get_job("scheduler-prepare-publish-job")
    assert ready_job is not None
    assert ready_job.status == store.STATUS_READY
    assert ready_job.prepared_payload.get("content")

    published = asyncio.run(pipeline.publish_next_ready_job())
    assert published is True

    completed_job = store.get_job("scheduler-prepare-publish-job")
    assert completed_job is not None
    assert completed_job.status == store.STATUS_COMPLETED
    assert completed_job.prepared_payload == {}


def test_weighted_publish_slots_are_deterministic_with_seed():
    """가중 분포 발행 슬롯은 seed가 같으면 항상 동일해야 한다."""
    scheduler_a = SchedulerService(
        daily_posts_target=3,
        min_post_interval_minutes=60,
        random_seed=20260220,
    )
    scheduler_b = SchedulerService(
        daily_posts_target=3,
        min_post_interval_minutes=60,
        random_seed=20260220,
    )

    target_date = date(2026, 2, 20)
    slots_a = scheduler_a._build_daily_publish_slots(target_date)
    slots_b = scheduler_b._build_daily_publish_slots(target_date)

    assert [slot.isoformat() for slot in slots_a] == [slot.isoformat() for slot in slots_b]
    assert len(slots_a) == 3
    assert slots_a == sorted(slots_a)

    for index in range(1, len(slots_a)):
        gap_minutes = (slots_a[index] - slots_a[index - 1]).total_seconds() / 60.0
        assert gap_minutes >= 60.0
