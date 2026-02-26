"""Phase 21 Scheduler Daemon — E2E 수동 트리거 테스트.

FastAPI lifespan + scheduler.py 라우터 + SchedulerService 통합 흐름을 검증한다.

테스트 시나리오:
1. GET /api/scheduler/status → 정상 응답 스키마 확인
2. POST /api/scheduler/trigger/seed → 오늘 날짜 Job 생성 확인
3. POST /api/scheduler/trigger/draft → pipeline_service 없을 때 graceful 처리
4. POST /api/scheduler/trigger/publish → pipeline_service 없을 때 graceful 처리
5. SchedulerService.start() / stop() 생명주기 확인
6. 스케줄러 미실행 시 503 반환 확인
"""

from __future__ import annotations

import asyncio
import json as json_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.scheduler_service import SchedulerService

# FastAPI app 을 모듈 레벨에서 한 번만 import (naver_connect.py asyncio.Lock 문제 회피)
from server.main import app
from server.dependencies import get_app_config, get_job_store, get_llm_router
from server.routers.scheduler import set_scheduler_instance


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def build_store(tmp_path: Path, name: str = "daemon_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def build_scheduler(job_store: JobStore) -> SchedulerService:
    return SchedulerService(
        pipeline_service=None,  # API 서버 전용: pipeline 없이 큐 관리만
        job_store=job_store,
        timezone_name="Asia/Seoul",
        daily_posts_target=3,
        random_seed=42,  # 슬롯 재현성 확보
    )


def _today_kst() -> str:
    """현재 KST 날짜를 ISO 문자열로 반환한다."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def _run_in_loop(*coros) -> list:
    """여러 코루틴을 단일 이벤트 루프에서 순차 실행하고 결과를 반환한다."""
    async def _all():
        results = []
        for coro in coros:
            results.append(await coro)
        return results
    return asyncio.run(_all())


# ─────────────────────────────────────────────────────────────────────────────
# 1. 스케줄러 생명주기 테스트
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_lifecycle(tmp_path: Path):
    """start() / stop() 이 오류 없이 동작해야 한다."""
    store = build_store(tmp_path)
    scheduler = build_scheduler(store)

    async def _run():
        await scheduler.start()
        running_after_start = scheduler._scheduler.running
        # stop() 은 예외 없이 완료되어야 한다
        await scheduler.stop()
        scheduler_exists = scheduler._scheduler is not None
        return running_after_start, scheduler_exists

    running_after_start, scheduler_exists = asyncio.run(_run())
    assert scheduler_exists, "Scheduler instance should still exist after stop"
    assert running_after_start is True, "Scheduler should be running after start()"
    # APScheduler wait=False shutdown 후 running 상태는 구현마다 다를 수 있으므로
    # stop() 자체가 예외 없이 완료되는 것만 확인한다


# ─────────────────────────────────────────────────────────────────────────────
# 2. 큐 시드 트리거
# ─────────────────────────────────────────────────────────────────────────────

def test_manual_seed_trigger(tmp_path: Path):
    """수동 시드 트리거 후 오늘 날짜 Job 이 DB 에 생성되어야 한다."""
    store = build_store(tmp_path)

    # 온보딩 설정 모사: daily_target=2, 카테고리 1개, idea_vault_quota=0 (순수 카테고리 Job 생성)
    store.set_system_setting("scheduler_daily_posts_target", "2")
    store.set_system_setting("scheduler_idea_vault_daily_quota", "0")
    allocations = [{"category": "IT 기술", "topic_mode": "it", "count": 2}]
    store.set_system_setting("scheduler_category_allocations", json_mod.dumps(allocations))

    scheduler = build_scheduler(store)

    async def _run():
        await scheduler.start()
        # seed 날짜 초기화 → 강제 재실행
        store.set_system_setting("scheduler_last_seed_date", "")
        await scheduler._run_daily_quota_seed()
        await scheduler.stop()

    asyncio.run(_run())

    # DB Job 생성 확인 (get_queue_stats 는 존재하는 status key만 반환)
    stats = store.get_queue_stats()
    total_queued = sum(stats.values())
    assert total_queued >= 2, f"Expected ≥2 queued jobs, got stats={stats}"

    # seed 날짜가 기록되어야 함
    assert store.get_system_setting("scheduler_last_seed_date", "") == _today_kst()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 초안 선생성 — pipeline_service=None graceful 처리
# ─────────────────────────────────────────────────────────────────────────────

def test_draft_prefetch_no_pipeline(tmp_path: Path):
    """pipeline_service 가 없을 때 _run_draft_prefetch 가 오류 없이 조기 종료해야 한다."""
    store = build_store(tmp_path)
    scheduler = build_scheduler(store)

    async def _run():
        await scheduler.start()
        await scheduler._run_draft_prefetch()  # 오류 없이 완료되어야 함
        await scheduler.stop()

    asyncio.run(_run())  # 예외 없이 통과해야 함


# ─────────────────────────────────────────────────────────────────────────────
# 4. 발행 — pipeline_service=None graceful 처리
# ─────────────────────────────────────────────────────────────────────────────

def test_publish_no_pipeline_returns_false(tmp_path: Path):
    """pipeline_service 가 없을 때 _publish_next_available_job 이 False 를 반환해야 한다."""
    store = build_store(tmp_path)
    scheduler = build_scheduler(store)

    async def _run():
        await scheduler.start()
        result = await scheduler._publish_next_available_job()
        await scheduler.stop()
        return result

    result = asyncio.run(_run())
    assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# 5. FastAPI 라우터 — status 엔드포인트 스키마 검증
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_status_endpoint_schema(tmp_path: Path):
    """GET /api/scheduler/status 가 올바른 스키마를 반환해야 한다."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "api_test.db")
    store.set_system_setting("scheduler_daily_posts_target", "3")

    # 라우터에 스케줄러 인스턴스 주입 (setup만, start 없이)
    scheduler = build_scheduler(store)
    scheduler.setup_scheduler()
    set_scheduler_instance(scheduler)

    app.dependency_overrides[get_job_store] = lambda: store
    try:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/scheduler/status")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        data = response.json()
        required_keys = {
            "scheduler_running",
            "daemon_alive",
            "api_only_mode",
            "paused",
            "today_date",
            "daily_target",
            "today_completed",
            "today_failed",
            "ready_to_publish",
            "queued",
            "ready_master",
            "ready_sub",
            "queued_master",
            "queued_sub",
            "active_hours",
            "last_seed_date",
            "last_seed_count",
        }
        missing = required_keys - set(data.keys())
        assert not missing, f"Missing response keys: {missing}"
        assert isinstance(data["scheduler_running"], bool)
        assert isinstance(data["daemon_alive"], bool)
        assert isinstance(data["api_only_mode"], bool)
        assert isinstance(data["paused"], bool)
        assert data["paused"] is False
        assert isinstance(data["daily_target"], int)
        assert data["active_hours"] == "08:00~22:00"
        assert data["today_date"] == _today_kst()
    finally:
        app.dependency_overrides.clear()
        set_scheduler_instance(None)


def test_dashboard_stats_scheduler_fields(tmp_path: Path):
    """GET /api/stats/dashboard 의 scheduler 필드에 데몬 상태 키가 포함되어야 한다."""
    from fastapi.testclient import TestClient
    from server.routers import stats as stats_router

    store = build_store(tmp_path, "api_dashboard_test.db")
    store.set_system_setting(
        "scheduler_daemon_heartbeat_at",
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    store.set_system_setting("scheduler_paused", "1")

    scheduler = build_scheduler(store)
    scheduler.setup_scheduler()
    set_scheduler_instance(scheduler)

    # 외부 의존을 줄이기 위해 텔레그램/헬스 요약 빌더를 스텁 처리한다.
    async def _telegram_stub(_job_store):
        return stats_router.TelegramStatusData(
            configured=False,
            live_ok=False,
            bot_username=None,
            error="stub",
        )

    async def _health_stub(_app_config, _llm_router):
        return stats_router.HealthSummaryData(status="ok", ok=1, fail=0, total=1)

    original_telegram = stats_router._fetch_telegram_status
    original_health = stats_router._build_health_summary
    stats_router._fetch_telegram_status = _telegram_stub
    stats_router._build_health_summary = _health_stub

    app.dependency_overrides[get_job_store] = lambda: store
    app.dependency_overrides[get_app_config] = lambda: object()
    app.dependency_overrides[get_llm_router] = lambda: object()
    try:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/stats/dashboard")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        data = response.json()
        scheduler_data = data["scheduler"]
        required_keys = {
            "scheduler_running",
            "daemon_alive",
            "api_only_mode",
            "paused",
            "today_date",
            "daily_target",
            "today_completed",
            "today_failed",
            "ready_to_publish",
            "queued",
            "ready_master",
            "ready_sub",
            "queued_master",
            "queued_sub",
            "active_hours",
            "last_seed_date",
            "last_seed_count",
        }
        missing = required_keys - set(scheduler_data.keys())
        assert not missing, f"Missing scheduler keys: {missing}"
        assert isinstance(scheduler_data["scheduler_running"], bool)
        assert isinstance(scheduler_data["daemon_alive"], bool)
        assert isinstance(scheduler_data["api_only_mode"], bool)
        assert isinstance(scheduler_data["paused"], bool)
        assert scheduler_data["daemon_alive"] is True
        assert scheduler_data["paused"] is True
    finally:
        app.dependency_overrides.clear()
        stats_router._fetch_telegram_status = original_telegram
        stats_router._build_health_summary = original_health
        set_scheduler_instance(None)


def test_scheduler_pause_resume_endpoints(tmp_path: Path):
    """POST /api/scheduler/pause,resume 가 paused 상태를 토글해야 한다."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "api_pause_resume_test.db")
    scheduler = build_scheduler(store)
    scheduler.setup_scheduler()
    set_scheduler_instance(scheduler)

    app.dependency_overrides[get_job_store] = lambda: store
    try:
        client = TestClient(app, raise_server_exceptions=True)

        pause_response = client.post("/api/scheduler/pause")
        assert pause_response.status_code == 200
        pause_data = pause_response.json()
        assert pause_data["ok"] is True
        assert store.get_system_setting("scheduler_paused", "") == "1"

        status_after_pause = client.get("/api/scheduler/status")
        assert status_after_pause.status_code == 200
        assert status_after_pause.json()["paused"] is True

        resume_response = client.post("/api/scheduler/resume")
        assert resume_response.status_code == 200
        resume_data = resume_response.json()
        assert resume_data["ok"] is True
        assert store.get_system_setting("scheduler_paused", "") == ""

        status_after_resume = client.get("/api/scheduler/status")
        assert status_after_resume.status_code == 200
        assert status_after_resume.json()["paused"] is False
    finally:
        app.dependency_overrides.clear()
        set_scheduler_instance(None)


def test_scheduler_status_endpoint_reports_ready_to_publish(tmp_path: Path):
    """status 엔드포인트가 ready_to_publish 상태 건수를 정확히 반환해야 한다."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "api_ready_count_test.db")
    due_now = "2026-02-24T00:00:00Z"
    future_time = "2099-12-31T00:00:00Z"

    # queued 카운트 분리 검증용
    assert store.schedule_job(
        job_id="queued-master-status-job",
        title="Queued Master",
        seed_keywords=["queued", "master"],
        platform="naver",
        persona_id="P1",
        scheduled_at=future_time,
        job_kind=store.JOB_KIND_MASTER,
        status=store.STATUS_QUEUED,
    )
    assert store.schedule_job(
        job_id="queued-sub-status-job",
        title="Queued Sub",
        seed_keywords=["queued", "sub"],
        platform="naver",
        persona_id="P1",
        scheduled_at=future_time,
        job_kind=store.JOB_KIND_SUB,
        master_job_id="queued-master-status-job",
        channel_id="channel-sub-status",
        status=store.STATUS_QUEUED,
    )

    # ready 카운트 분리 검증용
    assert store.schedule_job(
        job_id="ready-master-status-job",
        title="Ready Master Status Job",
        seed_keywords=["ready", "master"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        job_kind=store.JOB_KIND_MASTER,
    )
    claimed_master = store.claim_due_jobs(limit=1, now_override=due_now, job_kind=store.JOB_KIND_MASTER)
    assert len(claimed_master) == 1
    assert store.save_prepared_payload(
        "ready-master-status-job",
        {"title": "ready", "content": "본문", "images": [], "image_points": []},
    )
    assert store.schedule_job(
        job_id="ready-sub-status-job",
        title="Ready Sub Status Job",
        seed_keywords=["ready", "sub"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        job_kind=store.JOB_KIND_SUB,
        master_job_id="ready-master-status-job",
        channel_id="channel-sub-status",
    )
    claimed_sub = store.claim_due_jobs(limit=1, now_override=due_now, job_kind=store.JOB_KIND_SUB)
    assert len(claimed_sub) == 1
    assert store.save_prepared_payload(
        "ready-sub-status-job",
        {"title": "ready-sub", "content": "본문", "images": [], "image_points": []},
    )

    scheduler = build_scheduler(store)
    scheduler.setup_scheduler()
    set_scheduler_instance(scheduler)

    app.dependency_overrides[get_job_store] = lambda: store
    try:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/scheduler/status")
        assert response.status_code == 200
        data = response.json()
        assert int(data["ready_to_publish"]) >= 1
        assert int(data["ready_master"]) == 1
        assert int(data["ready_sub"]) == 1
        assert int(data["queued_master"]) == 1
        assert int(data["queued_sub"]) == 1
    finally:
        app.dependency_overrides.clear()
        set_scheduler_instance(None)


# ─────────────────────────────────────────────────────────────────────────────
# 6. FastAPI 라우터 — seed 트리거 엔드포인트
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_seed_trigger_endpoint(tmp_path: Path):
    """POST /api/scheduler/trigger/seed 가 정상 응답을 반환해야 한다."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "api_seed_test.db")
    store.set_system_setting("scheduler_daily_posts_target", "1")
    store.set_system_setting("scheduler_idea_vault_daily_quota", "0")  # vault 비활성
    allocations = [{"category": "테스트카테", "topic_mode": "cafe", "count": 1}]
    store.set_system_setting("scheduler_category_allocations", json_mod.dumps(allocations))

    scheduler = build_scheduler(store)
    scheduler.setup_scheduler()
    set_scheduler_instance(scheduler)

    app.dependency_overrides[get_job_store] = lambda: store
    try:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.post("/api/scheduler/trigger/seed")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["ok"] is True
        assert "건" in data["message"]

        # DB 에 Job 이 생성되었는지 확인
        stats = store.get_queue_stats()
        total = sum(stats.values())
        assert total >= 1, f"Expected ≥1 job after seed trigger, stats={stats}"
    finally:
        app.dependency_overrides.clear()
        set_scheduler_instance(None)


# ─────────────────────────────────────────────────────────────────────────────
# 7. FastAPI 라우터 — 스케줄러 없을 때 503
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_trigger_503_when_not_running(tmp_path: Path):
    """스케줄러가 None 일 때 trigger 엔드포인트가 503 을 반환해야 한다."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "api_503_test.db")
    set_scheduler_instance(None)
    app.dependency_overrides[get_job_store] = lambda: store
    try:
        client = TestClient(app, raise_server_exceptions=False)
        for path in [
            "/api/scheduler/trigger/seed",
            "/api/scheduler/trigger/draft",
            "/api/scheduler/trigger/publish",
        ]:
            response = client.post(path)
            assert response.status_code == 503, (
                f"Expected 503 for {path}, got {response.status_code}: {response.text}"
            )
    finally:
        app.dependency_overrides.clear()
