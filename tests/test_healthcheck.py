import json
from pathlib import Path
from typing import Any, Dict, Optional

from modules.automation.job_store import JobStore
from modules.automation.time_utils import now_utc
from scripts.healthcheck import run_healthcheck


def create_job(store: JobStore, job_id: str, status: str) -> None:
    """헬스체크 테스트용 job 상태를 생성한다."""
    scheduled_at = now_utc()
    assert store.schedule_job(
        job_id=job_id,
        title=f"title-{job_id}",
        seed_keywords=["health", "check"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    if status == "queued":
        return

    claimed = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert claimed, f"failed to claim job: {job_id}"
    claimed_job = claimed[0]

    if status == "completed":
        store.complete_job(claimed_job.job_id, f"https://blog.naver.com/{job_id}")
    elif status == "failed":
        store.fail_job(claimed_job.job_id, "PIPELINE_ERROR", "test failure")


def write_session_state(path: Path, expires_in_hours: Optional[int]) -> None:
    """세션 상태 파일을 작성한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any]
    if expires_in_hours is None:
        payload = {"cookies": [], "origins": []}
    else:
        expires_epoch = 1_700_000_000
        if expires_in_hours > 0:
            # now 기준 계산이 단순하도록 충분히 큰 미래 epoch를 사용한다.
            expires_epoch = 4_000_000_000
        payload = {
            "cookies": [{"name": "NID_SES", "expires": expires_epoch}],
            "origins": [],
        }
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file)


def test_healthcheck_healthy(tmp_path: Path):
    """정상 상태일 때 healthy를 반환하는지 검증한다."""
    db_path = tmp_path / "automation.db"
    store = JobStore(str(db_path))

    for index in range(8):
        create_job(store, f"done-{index}", "completed")
    for index in range(2):
        create_job(store, f"fail-{index}", "failed")
    create_job(store, "queued-1", "queued")

    session_file = tmp_path / "data" / "sessions" / "naver" / "state.json"
    write_session_state(session_file, expires_in_hours=24)

    result = run_healthcheck(str(db_path), str(session_file))
    assert result["status"] == "healthy"
    assert result["checks"]["database"]["pending_jobs"] >= 1
    assert result["checks"]["success_rate"]["rate"] >= 0.7
    assert result["checks"]["session"]["status"] == "ok"


def test_healthcheck_unhealthy_when_session_missing(tmp_path: Path):
    """세션 파일이 없으면 unhealthy를 반환하는지 검증한다."""
    db_path = tmp_path / "automation.db"
    store = JobStore(str(db_path))
    create_job(store, "done-1", "completed")

    missing_session_file = tmp_path / "data" / "sessions" / "naver" / "state.json"
    result = run_healthcheck(str(db_path), str(missing_session_file))

    assert result["status"] == "unhealthy"
    assert result["checks"]["session"]["reason"] == "session_file_missing"
