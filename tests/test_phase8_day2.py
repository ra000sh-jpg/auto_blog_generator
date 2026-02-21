from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.scheduler_service import SchedulerService
from modules.automation.time_utils import now_utc
from modules.uploaders.playwright_publisher import PublishResult


def build_store(tmp_path: Path, name: str = "phase8_day2.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_retries=3, max_llm_calls_per_job=15))


def schedule_and_claim(store: JobStore, job_id: str) -> Any:
    scheduled_at = now_utc()
    assert store.schedule_job(
        job_id=job_id,
        title="Day2 테스트",
        seed_keywords=["자동화", "테스트"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )
    jobs = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(jobs) == 1
    return jobs[0]


class AlwaysNetworkTimeoutPublisher:
    RETRYABLE_ERRORS = frozenset({"NETWORK_TIMEOUT"})

    def __init__(self):
        self.calls = 0

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        self.calls += 1
        return PublishResult(
            success=False,
            error_code="NETWORK_TIMEOUT",
            error_message="timeout",
        )


class CaptchaPublisher:
    RETRYABLE_ERRORS = frozenset()

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        return PublishResult(
            success=False,
            error_code="CAPTCHA_REQUIRED",
            error_message="captcha detected",
        )


class DummyNotifier:
    def __init__(self):
        self.critical_calls: List[Dict[str, str]] = []
        self.enabled = True
        self.daily_calls = 0

    def notify_critical_background(
        self,
        *,
        error_code: str,
        message: str,
        job_id: str = "",
    ) -> None:
        self.critical_calls.append(
            {"error_code": error_code, "message": message, "job_id": job_id}
        )

    async def notify_daily_summary(
        self,
        *,
        local_date: str,
        target: int,
        completed: int,
        failed: int,
        ready_count: int,
        queued_count: int,
        idea_pending_count: int = -1,
        idea_daily_quota: int = 0,
    ) -> bool:
        del local_date, target, completed, failed, ready_count, queued_count
        del idea_pending_count, idea_daily_quota
        self.daily_calls += 1
        return True


async def long_content_generate(_job) -> Dict[str, Any]:
    content = ("자동화 테스트 본문입니다. " * 80).strip()
    return {
        "final_content": content,
        "quality_gate": "pass",
        "quality_snapshot": {"score": 90, "issues": []},
        "seo_snapshot": {"topic_mode": "cafe"},
        "image_prompts": [],
        "llm_calls_used": 1,
    }


async def short_content_generate(_job) -> Dict[str, Any]:
    return {
        "final_content": "짧은 글",
        "quality_gate": "pass",
        "quality_snapshot": {"score": 90, "issues": []},
        "seo_snapshot": {"topic_mode": "cafe"},
        "image_prompts": [],
        "llm_calls_used": 1,
    }


def test_job_metrics_table_and_insert(tmp_path: Path):
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "metric-job")

    store.record_job_metric(
        job_id=job.job_id,
        metric_type="quality_gate",
        status="failed",
        duration_ms=12.5,
        error_code="QUALITY_FAILED",
        input_tokens=11,
        output_tokens=22,
        provider="qwen",
        detail={"reason": "too_short"},
    )

    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                MAX(input_tokens) AS max_input_tokens,
                MAX(output_tokens) AS max_output_tokens,
                MAX(provider) AS provider
            FROM job_metrics
            WHERE job_id = ?
            """,
            (job.job_id,),
        ).fetchone()
    assert row is not None
    assert int(row["total"]) == 1
    assert int(row["max_input_tokens"]) == 11
    assert int(row["max_output_tokens"]) == 22
    assert str(row["provider"]) == "qwen"


def test_fail_job_force_final_skips_retry_wait(tmp_path: Path):
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "force-final-job")

    ok = store.fail_job(
        job_id=job.job_id,
        error_code="NETWORK_TIMEOUT",
        error_message="timeout",
        force_final=True,
    )
    assert ok is True

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == store.STATUS_FAILED


def test_pipeline_quality_failure_limited_to_one_queue_retry(tmp_path: Path):
    store = build_store(tmp_path)
    pipeline = PipelineService(
        job_store=store,
        publisher=CaptchaPublisher(),
        generate_fn=short_content_generate,
        internal_retry_attempts=1,
        queue_retry_limit=1,
    )

    first = schedule_and_claim(store, "quality-retry-job")
    asyncio.run(pipeline.run_job(first))

    after_first = store.get_job("quality-retry-job")
    assert after_first is not None
    assert after_first.status == store.STATUS_RETRY_WAIT
    assert after_first.retry_count == 1

    retried = store.claim_due_jobs(limit=1, now_override="2099-01-01T00:00:00Z")
    assert len(retried) == 1
    asyncio.run(pipeline.run_job(retried[0]))

    after_second = store.get_job("quality-retry-job")
    assert after_second is not None
    assert after_second.status == store.STATUS_FAILED
    assert after_second.error_code == "QUALITY_FAILED"


def test_pipeline_network_timeout_uses_internal_and_queue_retry_limits(tmp_path: Path):
    store = build_store(tmp_path)
    publisher = AlwaysNetworkTimeoutPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=long_content_generate,
        internal_retry_attempts=1,
        queue_retry_limit=1,
    )

    first = schedule_and_claim(store, "network-retry-job")
    asyncio.run(pipeline.run_job(first))

    mid = store.get_job("network-retry-job")
    assert mid is not None
    assert mid.status == store.STATUS_RETRY_WAIT
    assert publisher.calls == 2  # 초기 + 내부 재시도 1회

    retried = store.claim_due_jobs(limit=1, now_override="2099-01-01T00:00:00Z")
    assert len(retried) == 1
    asyncio.run(pipeline.run_job(retried[0]))

    final = store.get_job("network-retry-job")
    assert final is not None
    assert final.status == store.STATUS_FAILED
    assert final.error_code == "NETWORK_TIMEOUT"
    assert publisher.calls == 4


def test_pipeline_notifies_critical_error(tmp_path: Path):
    store = build_store(tmp_path)
    notifier = DummyNotifier()
    pipeline = PipelineService(
        job_store=store,
        publisher=CaptchaPublisher(),
        generate_fn=long_content_generate,
        notifier=notifier,
        internal_retry_attempts=1,
        queue_retry_limit=1,
    )

    job = schedule_and_claim(store, "critical-notify-job")
    asyncio.run(pipeline.run_job(job))

    assert len(notifier.critical_calls) == 1
    assert notifier.critical_calls[0]["error_code"] == "CAPTCHA_REQUIRED"


def test_scheduler_registers_daily_summary_job_when_notifier_enabled():
    notifier = DummyNotifier()
    scheduler = SchedulerService(notifier=notifier)
    scheduler.setup_scheduler()

    if hasattr(scheduler._scheduler, "jobs"):
        job = scheduler._scheduler.jobs.get("daily_summary_notification")
        assert job is not None
    else:
        jobs = scheduler._scheduler.get_jobs()
        assert any(job.id == "daily_summary_notification" for job in jobs)


def test_scheduler_daily_summary_sends_once_per_day(tmp_path: Path):
    store = build_store(tmp_path)
    notifier = DummyNotifier()
    scheduler = SchedulerService(job_store=store, notifier=notifier)
    scheduler._get_now_local = lambda: datetime(2026, 2, 20, 22, 30, 0)  # type: ignore[assignment]

    asyncio.run(scheduler._run_daily_summary_notification())
    asyncio.run(scheduler._run_daily_summary_notification())

    assert notifier.daily_calls == 1
