import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.time_utils import now_utc
from modules.automation.worker import Worker, WorkerConfig
from modules.uploaders.playwright_publisher import PlaywrightPublisher, PublishResult


def build_store(tmp_path: Path, db_name: str = "pipeline_test.db") -> JobStore:
    """테스트용 JobStore를 생성한다."""
    return JobStore(
        str(tmp_path / db_name),
        config=JobConfig(max_retries=3, max_llm_calls_per_job=15),
    )


class DummyPublisher:
    """파이프라인 테스트용 더미 발행기."""

    def __init__(self, success: bool = True):
        self.success = success
        self.called = 0

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del images, image_sources, image_points, tags, category
        self.called += 1
        if self.success:
            return PublishResult(success=True, url=f"https://blog.naver.com/test/{self.called}")
        return PublishResult(success=False, error_code="PUBLISH_FAILED", error_message="publish failed")


def schedule_and_claim(store: JobStore, job_id: str = "job-1"):
    """job을 등록 후 running 상태로 선점한다."""
    scheduled_at = now_utc()
    ok = store.schedule_job(
        job_id=job_id,
        title="테스트 포스트",
        seed_keywords=["테스트", "자동화"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )
    assert ok
    jobs = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(jobs) == 1
    return jobs[0]


def test_pipeline_quality_retry_mask(tmp_path: Path):
    """retry_mask가 2회 연속이면 QUALITY_FAILED로 전환되는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "retry-mask-job")

    async def retry_mask_generate(_job) -> Dict[str, Any]:
        return {
            "final_content": "content",
            "quality_gate": "retry_mask",
            "quality_snapshot": {},
            "seo_snapshot": {},
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=retry_mask_generate,
    )

    async def run_once():
        await asyncio.wait_for(pipeline.run_job(job), timeout=3)

    asyncio.run(run_once())

    updated = store.get_job("retry-mask-job")
    assert updated is not None
    assert updated.status in {store.STATUS_RETRY_WAIT, store.STATUS_FAILED}
    assert updated.error_code == "QUALITY_FAILED"
    assert updated.quality_snapshot.get("mask_retry_done") is True


def test_pipeline_quality_retry_all(tmp_path: Path):
    """retry_all 결과가 retry_wait으로 전환되는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "retry-all-job")

    async def retry_all_generate(_job) -> Dict[str, Any]:
        return {
            "final_content": "content",
            "quality_gate": "retry_all",
            "quality_snapshot": {},
            "seo_snapshot": {},
            "llm_calls_used": 2,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=retry_all_generate,
    )

    asyncio.run(pipeline.run_job(job))

    updated = store.get_job("retry-all-job")
    assert updated is not None
    assert updated.status == store.STATUS_RETRY_WAIT
    assert updated.error_code == "QUALITY_FAILED"
    assert updated.retry_count == 1


def test_pipeline_llm_budget_exceeded(tmp_path: Path):
    """LLM 예산 초과 시 BUDGET_EXCEEDED로 실패하는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "budget-exceeded-job")
    store.increment_llm_calls(job.job_id, 15)

    generate_called = {"count": 0}

    async def generate_never_called(_job) -> Dict[str, Any]:
        generate_called["count"] += 1
        return {"quality_gate": "pass", "final_content": "x", "llm_calls_used": 1}

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_never_called,
    )
    asyncio.run(pipeline.run_job(job))

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == store.STATUS_FAILED
    assert updated.error_code == "BUDGET_EXCEEDED"
    assert generate_called["count"] == 0


def test_pipeline_already_published_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """이미 발행된 job은 complete_job 없이 스킵되는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "already-published-job")

    with store.connection() as conn:
        conn.execute(
            "UPDATE jobs SET result_url = ? WHERE job_id = ?",
            ("https://blog.naver.com/existing/123", job.job_id),
        )

    complete_spy = MagicMock(wraps=store.complete_job)
    monkeypatch.setattr(store, "complete_job", complete_spy)

    publisher = DummyPublisher()
    pipeline = PipelineService(job_store=store, publisher=publisher, generate_fn=lambda _j: {})
    asyncio.run(pipeline.run_job(job))

    assert complete_spy.call_count == 0
    assert publisher.called == 0


def test_playwright_dry_run_returns_url(monkeypatch: pytest.MonkeyPatch):
    """DRY_RUN=true면 실제 브라우저 없이 URL을 반환하는지 검증."""
    monkeypatch.setenv("DRY_RUN", "true")
    publisher = PlaywrightPublisher(blog_id="dry-run")
    result = asyncio.run(publisher.publish(title="테스트", content="본문"))
    assert result.success is True
    assert result.url == "https://blog.naver.com/dry-run/000000000000"


def test_playwright_cleanup_order():
    """cleanup이 context -> browser -> playwright 순서로 호출되는지 검증."""
    publisher = PlaywrightPublisher(blog_id="cleanup-order")
    close_order = []

    class ContextMock:
        async def close(self):
            close_order.append("context")

    class BrowserMock:
        async def close(self):
            close_order.append("browser")

    class PlaywrightMock:
        async def stop(self):
            close_order.append("playwright")

    publisher._context = ContextMock()
    publisher._browser = BrowserMock()
    publisher._playwright = PlaywrightMock()

    asyncio.run(publisher._cleanup())

    assert close_order == ["context", "browser", "playwright"]
    assert publisher._context is None
    assert publisher._browser is None
    assert publisher._playwright is None


def test_playwright_error_classification():
    """에러 문자열에 따른 분류 코드가 기대값과 일치하는지 검증."""
    publisher = PlaywrightPublisher(blog_id="error-classify")
    assert publisher._classify_error(Exception("Timeout 30000ms exceeded")) == "NETWORK_TIMEOUT"
    assert publisher._classify_error(Exception("selector not found")) == "ELEMENT_NOT_FOUND"
    assert publisher._classify_error(Exception("HTTP 429 rate limited")) == "RATE_LIMITED"


def test_schedule_post_idempotency(tmp_path: Path):
    """동일 idempotency 키 입력 시 두 번째 등록이 거절되는지 검증."""
    store = build_store(tmp_path)
    scheduled_at = "2026-02-21T00:00:00Z"

    first = store.schedule_job(
        job_id="idem-first",
        title="중복 방지 테스트",
        seed_keywords=["중복", "검증"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )
    second = store.schedule_job(
        job_id="idem-second",
        title="중복 방지 테스트",
        seed_keywords=["중복", "다른키워드"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    assert first is True
    assert second is False


def test_worker_graceful_shutdown(tmp_path: Path):
    """shutdown 요청 시 실행 중 job이 timeout 내 완료되는지 검증."""
    store = build_store(tmp_path)
    scheduled_at = now_utc()

    assert store.schedule_job(
        job_id="graceful-job",
        title="Graceful Shutdown",
        seed_keywords=["워커", "종료"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    async def process_job(job):
        await asyncio.sleep(0.2)
        store.complete_job(job.job_id, "https://blog.naver.com/graceful/1")

    async def scenario():
        worker = Worker(
            job_store=store,
            process_job=process_job,
            config=WorkerConfig(
                poll_interval_sec=0.05,
                max_concurrent_jobs=1,
                heartbeat_interval_sec=1,
                reaper_interval_sec=1,
                graceful_shutdown_timeout_sec=2,
            ),
        )
        worker_task = asyncio.create_task(worker.run())
        try:
            for _ in range(200):
                if worker.active_job_count > 0:
                    break
                await asyncio.sleep(0.01)
            assert worker.active_job_count == 1
            await worker.shutdown()
            await asyncio.wait_for(worker_task, timeout=5)
        finally:
            if not worker_task.done():
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task

    asyncio.run(scenario())

    updated = store.get_job("graceful-job")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED
