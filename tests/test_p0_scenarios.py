import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from modules.automation import JobStore, Worker, now_utc
from modules.automation.job_store import JobConfig
from modules.automation.time_utils import add_seconds, parse_iso


UTC_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def assert_utc_iso(value: str) -> None:
    """UTC ISO 형식 검증 헬퍼."""
    assert value, "timestamp should not be empty"
    assert UTC_ISO_PATTERN.match(value), f"invalid UTC format: {value}"
    parsed = parse_iso(value)
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


@pytest.fixture
def store(tmp_path):
    """테스트용 JobStore 생성."""
    db_path = tmp_path / "automation_test.db"
    config = JobConfig(
        max_retries=3,
        lease_timeout_sec=300,
        heartbeat_interval_sec=60,
        max_llm_calls_per_job=15,
    )
    return JobStore(str(db_path), config=config)


def test_claim_retry_wait_timing(store: JobStore):
    """retry_wait는 next_retry_at 이후에만 선점되는지 검증."""
    base_now = "2026-02-19T10:00:00Z"
    future_retry = add_seconds(base_now, 60)

    assert store.schedule_job(
        job_id="queued-job",
        title="Queued Job",
        seed_keywords=["queued"],
        platform="naver",
        persona_id="P1",
        scheduled_at=base_now,
    )
    assert store.schedule_job(
        job_id="retry-job",
        title="Retry Job",
        seed_keywords=["retry"],
        platform="naver",
        persona_id="P2",
        scheduled_at=add_seconds(base_now, -3600),
    )

    with store.connection() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, next_retry_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (store.STATUS_RETRY_WAIT, future_retry, base_now, "retry-job"),
        )

    claimed_before_retry = store.claim_due_jobs(limit=10, now_override=base_now)
    claimed_before_retry_ids = {job.job_id for job in claimed_before_retry}
    assert "queued-job" in claimed_before_retry_ids
    assert "retry-job" not in claimed_before_retry_ids

    claimed_after_retry = store.claim_due_jobs(
        limit=10,
        now_override=add_seconds(base_now, 61),
    )
    claimed_after_retry_ids = {job.job_id for job in claimed_after_retry}
    assert "retry-job" in claimed_after_retry_ids


def test_duplicate_claim_prevention(store: JobStore):
    """동시 선점 시 동일 job이 중복 claim되지 않는지 검증."""
    due_now = now_utc()
    assert store.schedule_job(
        job_id="duplicate-claim-job",
        title="Duplicate Claim Job",
        seed_keywords=["duplicate"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )

    barrier = threading.Barrier(2)

    def claim_once():
        barrier.wait(timeout=3)
        claimed_jobs = store.claim_due_jobs(limit=1, now_override=due_now)
        return [job.job_id for job in claimed_jobs]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim_once(), range(2)))

    claimed_ids = [job_id for batch in results for job_id in batch]
    assert claimed_ids.count("duplicate-claim-job") == 1

    job = store.get_job("duplicate-claim-job")
    assert job is not None
    assert job.status == store.STATUS_RUNNING


def test_running_stale_recovery(store: JobStore):
    """stale running 재큐잉과 heartbeat 보호 동작 검증."""
    stale_start = add_seconds(now_utc(), -700)

    assert store.schedule_job(
        job_id="stale-job",
        title="Stale Job",
        seed_keywords=["stale"],
        platform="naver",
        persona_id="P1",
        scheduled_at=stale_start,
    )
    stale_claimed = store.claim_due_jobs(limit=1, now_override=stale_start)
    assert len(stale_claimed) == 1

    async def noop_process(_job):
        return None

    worker = Worker(store, noop_process)
    asyncio.run(worker._reap_stale_jobs())  # noqa: SLF001

    requeued_job = store.get_job("stale-job")
    assert requeued_job is not None
    assert requeued_job.status == store.STATUS_RETRY_WAIT
    assert requeued_job.retry_count == 1

    fresh_now = now_utc()
    assert store.schedule_job(
        job_id="fresh-job",
        title="Fresh Job",
        seed_keywords=["fresh"],
        platform="naver",
        persona_id="P2",
        scheduled_at=fresh_now,
    )
    fresh_claimed = store.claim_due_jobs(limit=1, now_override=fresh_now)
    assert len(fresh_claimed) == 1
    assert fresh_claimed[0].job_id == "fresh-job"
    assert store.heartbeat("fresh-job")

    asyncio.run(worker._reap_stale_jobs())  # noqa: SLF001

    fresh_job = store.get_job("fresh-job")
    assert fresh_job is not None
    assert fresh_job.status == store.STATUS_RUNNING


def test_publish_idempotency(store: JobStore):
    """재발행 방지와 idempotency key 중복 등록 방지 검증."""
    scheduled = "2026-02-19T12:00:00Z"

    assert store.schedule_job(
        job_id="publish-job",
        title="Publish Once",
        seed_keywords=["publish"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=scheduled)
    assert len(claimed) == 1

    post_url = "https://blog.naver.com/sample/1"
    assert store.complete_job("publish-job", post_url)
    assert store.check_already_published("publish-job") == post_url

    assert store.schedule_job(
        job_id="idem-1",
        title="Same Identity",
        seed_keywords=["same", "identity"],
        platform="naver",
        persona_id="P9",
        scheduled_at="2026-02-20T09:00:00Z",
    )
    assert not store.schedule_job(
        job_id="idem-2",
        title="Same Identity",
        seed_keywords=["same", "identity", "dup"],
        platform="naver",
        persona_id="P9",
        scheduled_at="2026-02-20T09:00:00Z",
    )


def test_llm_budget_persistence(store: JobStore, tmp_path):
    """LLM 호출량이 재시작 후에도 유지되고 예산 초과가 처리되는지 검증."""
    db_path = tmp_path / "automation_test.db"
    scheduled = "2026-02-19T15:00:00Z"

    assert store.schedule_job(
        job_id="budget-job",
        title="Budget Job",
        seed_keywords=["budget"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=scheduled)
    assert len(claimed) == 1

    assert store.increment_llm_calls("budget-job", count=5) == 5

    reloaded_store = JobStore(str(db_path), config=JobConfig(max_llm_calls_per_job=15))
    reloaded_job = reloaded_store.get_job("budget-job")
    assert reloaded_job is not None
    assert reloaded_job.llm_call_count == 5

    assert reloaded_store.increment_llm_calls("budget-job", count=10) == 15
    assert not reloaded_store.check_llm_budget("budget-job")
    assert reloaded_store.fail_job(
        "budget-job",
        "BUDGET_EXCEEDED",
        "LLM call limit exceeded",
    )

    failed_job = reloaded_store.get_job("budget-job")
    assert failed_job is not None
    assert failed_job.status == reloaded_store.STATUS_FAILED
    assert failed_job.error_code == "BUDGET_EXCEEDED"


def test_timestamp_utc_consistency(store: JobStore):
    """timestamp 컬럼과 now_utc 출력 형식이 UTC ISO로 일관적인지 검증."""
    now_value = now_utc()
    assert_utc_iso(now_value)

    assert store.schedule_job(
        job_id="time-job",
        title="Time Job",
        seed_keywords=["time"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_value,
    )

    claimed = store.claim_due_jobs(limit=1, now_override=now_value)
    assert len(claimed) == 1
    claimed_job = store.get_job("time-job")
    assert claimed_job is not None

    assert_utc_iso(claimed_job.created_at)
    assert_utc_iso(claimed_job.updated_at)
    assert_utc_iso(claimed_job.scheduled_at)
    assert claimed_job.claimed_at is not None
    assert claimed_job.heartbeat_at is not None
    assert_utc_iso(claimed_job.claimed_at)
    assert_utc_iso(claimed_job.heartbeat_at)

    assert store.fail_job("time-job", "NETWORK_TIMEOUT", "retryable failure")
    retry_job = store.get_job("time-job")
    assert retry_job is not None
    assert retry_job.status == store.STATUS_RETRY_WAIT
    assert_utc_iso(retry_job.updated_at)
    assert retry_job.next_retry_at is not None
    assert_utc_iso(retry_job.next_retry_at)

    for event in store.get_job_events("time-job", limit=20):
        assert_utc_iso(event["created_at"])


def test_router_competition_tables_initialized(store: JobStore):
    """스마트 라우터 P0 테이블이 초기화되는지 검증."""
    with store.connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert "model_performance_log" in tables
    assert "weekly_competition_state" in tables
    assert "champion_history" in tables


def test_legacy_published_status_is_migrated(tmp_path):
    """legacy published 상태가 completed로 자동 변환되는지 검증."""
    db_path = tmp_path / "legacy_status.db"
    store = JobStore(str(db_path), config=JobConfig())

    scheduled_at = now_utc()
    assert store.schedule_job(
        job_id="legacy-status-job",
        title="Legacy Status",
        seed_keywords=["legacy"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'published' WHERE job_id = ?",
            ("legacy-status-job",),
        )

    reloaded = JobStore(str(db_path), config=JobConfig())
    migrated = reloaded.get_job("legacy-status-job")
    assert migrated is not None
    assert migrated.status == reloaded.STATUS_COMPLETED
