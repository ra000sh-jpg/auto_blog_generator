from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.time_utils import now_utc
from server.dependencies import get_job_store
from server.main import app


def build_store(tmp_path: Path, name: str = "multichannel_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _override_store(store: JobStore) -> None:
    app.dependency_overrides[get_job_store] = lambda: store


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_first_active_channel_must_be_master(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "master_required.db")
    _override_store(store)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/channels",
            json={
                "platform": "naver",
                "label": "서브 채널",
                "blog_url": "https://blog.naver.com/sub",
                "is_master": False,
                "active": True,
                "auth_json": {"session_dir": "data/sessions/naver_sub"},
            },
        )
        assert response.status_code == 409
    finally:
        _clear_overrides()


def test_channel_settings_toggle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "channel_settings.db")
    _override_store(store)
    try:
        client = TestClient(app, raise_server_exceptions=True)
        initial = client.get("/api/channels/settings")
        assert initial.status_code == 200
        assert initial.json()["multichannel_enabled"] is False

        updated = client.post("/api/channels/settings", json={"multichannel_enabled": True})
        assert updated.status_code == 200
        assert updated.json()["multichannel_enabled"] is True

        check = client.get("/api/channels/settings")
        assert check.status_code == 200
        assert check.json()["multichannel_enabled"] is True
    finally:
        _clear_overrides()


def test_deactivate_sub_channel_cancels_queued_and_ready_jobs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "deactivate_cancel.db")
    _override_store(store)

    master_channel_id = str(uuid.uuid4())
    sub_channel_id = str(uuid.uuid4())
    assert store.insert_channel(
        {
            "channel_id": master_channel_id,
            "platform": "naver",
            "label": "메인",
            "blog_url": "https://blog.naver.com/main",
            "persona_id": "P1",
            "is_master": True,
            "auth_json": "{}",
            "active": True,
        }
    )
    assert store.insert_channel(
        {
            "channel_id": sub_channel_id,
            "platform": "naver",
            "label": "서브",
            "blog_url": "https://blog.naver.com/sub",
            "persona_id": "P1",
            "is_master": False,
            "auth_json": "{}",
            "active": True,
        }
    )

    queued_job_id = str(uuid.uuid4())
    ready_job_id = str(uuid.uuid4())
    assert store.schedule_job(
        job_id=queued_job_id,
        title="queued sub",
        seed_keywords=["a"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_utc(),
        job_kind=store.JOB_KIND_SUB,
        master_job_id="master-1",
        channel_id=sub_channel_id,
        status=store.STATUS_QUEUED,
    )
    assert store.schedule_job(
        job_id=ready_job_id,
        title="ready sub",
        seed_keywords=["b"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_utc(),
        job_kind=store.JOB_KIND_SUB,
        master_job_id="master-2",
        channel_id=sub_channel_id,
        status=store.STATUS_READY,
    )

    try:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.delete(f"/api/channels/{sub_channel_id}")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ok"] is True
        assert int(data["cancelled_jobs"]) == 2

        queued_job = store.get_job(queued_job_id)
        ready_job = store.get_job(ready_job_id)
        assert queued_job is not None
        assert ready_job is not None
        assert queued_job.status == store.STATUS_CANCELLED
        assert ready_job.status == store.STATUS_CANCELLED
    finally:
        _clear_overrides()


def test_distribute_requires_master_completed_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "distribute_precondition.db")
    _override_store(store)
    try:
        master_job_id = str(uuid.uuid4())
        assert store.schedule_job(
            job_id=master_job_id,
            title="master pending",
            seed_keywords=["seed"],
            platform="naver",
            persona_id="P1",
            scheduled_at=now_utc(),
            status=store.STATUS_QUEUED,
            job_kind=store.JOB_KIND_MASTER,
        )

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(f"/api/jobs/{master_job_id}/distribute")
        assert response.status_code == 409
        assert "master_not_completed" in response.text
    finally:
        _clear_overrides()


def test_cannot_deactivate_only_active_master_via_update(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "master_update_guard.db")
    _override_store(store)
    try:
        master_channel_id = str(uuid.uuid4())
        assert store.insert_channel(
            {
                "channel_id": master_channel_id,
                "platform": "naver",
                "label": "메인",
                "blog_url": "https://blog.naver.com/main",
                "persona_id": "P1",
                "is_master": True,
                "auth_json": "{}",
                "active": True,
            }
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.put(
            f"/api/channels/{master_channel_id}",
            json={"active": False},
        )
        assert response.status_code == 409
    finally:
        _clear_overrides()


def test_complete_job_sets_completed_at(tmp_path: Path) -> None:
    store = build_store(tmp_path, "completed_at.db")
    job_id = str(uuid.uuid4())
    assert store.schedule_job(
        job_id=job_id,
        title="complete test",
        seed_keywords=["x"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_utc(),
    )
    claimed = store.claim_due_jobs(limit=1)
    assert len(claimed) == 1
    assert claimed[0].job_id == job_id

    assert store.complete_job(job_id=job_id, result_url="https://blog.naver.com/test/1")
    updated = store.get_job(job_id)
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED
    assert str(updated.completed_at).strip() != ""


def test_channel_test_endpoint_uses_publisher_connection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "channel_test_connection.db")
    _override_store(store)
    try:
        session_dir = tmp_path / "sessions" / "naver_sub"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "state.json").write_text("{}", encoding="utf-8")

        channel_id = str(uuid.uuid4())
        assert store.insert_channel(
            {
                "channel_id": channel_id,
                "platform": "naver",
                "label": "테스트 채널",
                "blog_url": "https://blog.naver.com/testsub",
                "persona_id": "P1",
                "is_master": False,
                "auth_json": f'{{"session_dir":"{session_dir}"}}',
                "active": True,
            }
        )

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(f"/api/channels/{channel_id}/test")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data.get("reason_code") is None
    finally:
        _clear_overrides()


def test_channel_test_endpoint_returns_false_without_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "channel_test_connection_fail.db")
    _override_store(store)
    try:
        missing_session_dir = tmp_path / "sessions" / "missing_naver"
        channel_id = str(uuid.uuid4())
        assert store.insert_channel(
            {
                "channel_id": channel_id,
                "platform": "naver",
                "label": "테스트 채널 실패",
                "blog_url": "https://blog.naver.com/testsub2",
                "persona_id": "P1",
                "is_master": False,
                "auth_json": f'{{"session_dir":"{missing_session_dir}"}}',
                "active": True,
            }
        )

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(f"/api/channels/{channel_id}/test")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["reason_code"] == "missing_state_file"
        assert "code=missing_state_file" in data["message"]
    finally:
        _clear_overrides()


def test_channel_test_endpoint_returns_tistory_auth_reason_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "channel_test_tistory_auth_reason.db")
    _override_store(store)
    try:
        channel_id = str(uuid.uuid4())
        assert store.insert_channel(
            {
                "channel_id": channel_id,
                "platform": "tistory",
                "label": "티스토리 테스트 채널",
                "blog_url": "https://sample-blog.tistory.com",
                "persona_id": "P1",
                "is_master": False,
                "auth_json": '{"blog_name":"sample-blog"}',
                "active": True,
            }
        )

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(f"/api/channels/{channel_id}/test")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["reason_code"] == "missing_access_token"
        assert "code=missing_access_token" in data["message"]
    finally:
        _clear_overrides()


def test_channel_test_endpoint_returns_publisher_not_implemented_reason_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_DISABLED", "true")
    store = build_store(tmp_path, "channel_test_wordpress_reason.db")
    _override_store(store)
    try:
        channel_id = str(uuid.uuid4())
        assert store.insert_channel(
            {
                "channel_id": channel_id,
                "platform": "wordpress",
                "label": "워드프레스 테스트 채널",
                "blog_url": "https://example.com/blog",
                "persona_id": "P1",
                "is_master": False,
                "auth_json": "{}",
                "active": True,
            }
        )

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(f"/api/channels/{channel_id}/test")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["reason_code"] == "publisher_not_implemented"
        assert "code=publisher_not_implemented" in data["message"]
    finally:
        _clear_overrides()
