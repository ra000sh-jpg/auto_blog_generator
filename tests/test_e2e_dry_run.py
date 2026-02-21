import asyncio
from pathlib import Path

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService, stub_generate_fn
from modules.uploaders.playwright_publisher import PlaywrightPublisher


def test_e2e_full_pipeline_dry_run(tmp_path: Path, monkeypatch):
    """DRY_RUN 환경에서 schedule -> claim -> run_job -> complete 흐름을 검증한다."""
    monkeypatch.setenv("DRY_RUN", "true")

    db_path = tmp_path / "e2e_dry_run.db"
    store = JobStore(str(db_path), config=JobConfig(max_llm_calls_per_job=15))

    scheduled_at = "2026-02-21T00:00:00Z"
    assert store.schedule_job(
        job_id="e2e-job-001",
        title="E2E 드라이런 테스트",
        seed_keywords=["e2e", "dry-run"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    claimed_jobs = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(claimed_jobs) == 1
    claimed_job = claimed_jobs[0]

    publisher = PlaywrightPublisher(blog_id="dry-run")
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=stub_generate_fn,
    )

    asyncio.run(pipeline.run_job(claimed_job))

    updated = store.get_job("e2e-job-001")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED
    assert updated.result_url == "https://blog.naver.com/dry-run/000000000000"
    assert updated.llm_call_count == 3
