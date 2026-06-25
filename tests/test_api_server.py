from __future__ import annotations

import uuid
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from modules.automation.job_store import JobStore
from modules.llm.idea_vault_parser import IdeaVaultParseResult, IdeaVaultParsedItem
from modules.llm.llm_router import LLMRouter
from modules.llm.magic_input_parser import MagicInputParseResult
from server.dependencies import (
    get_idea_vault_parser,
    get_job_store,
    get_llm_router,
    get_magic_input_parser,
)
from server.main import app


@pytest.fixture
def client(tmp_path) -> Generator[TestClient, None, None]:
    """테스트용 FastAPI 클라이언트."""
    db_path = tmp_path / "api_test.db"
    store = JobStore(db_path=str(db_path))

    app.dependency_overrides[get_job_store] = lambda: store
    app.dependency_overrides[get_llm_router] = lambda: LLMRouter(job_store=store)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_degraded_when_api_key_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """API 키 누락 실패는 500이 아닌 degraded로 반환해야 한다."""
    import server.routers.health as health_router

    async def fake_check_all_providers(skip_expensive=True, llm_config=None, api_keys=None):
        del skip_expensive, llm_config, api_keys
        return [
            {
                "provider": "qwen",
                "model": "qwen-plus",
                "status": "FAIL",
                "message": "DASHSCOPE_API_KEY 환경변수가 필요합니다.",
            },
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "status": "FAIL",
                "message": "DEEPSEEK_API_KEY 환경변수가 필요합니다.",
            },
        ]

    monkeypatch.setattr(health_router, "check_all_providers", fake_check_all_providers)

    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["summary"]["fail"] == 2


def test_jobs_post_and_list(client: TestClient):
    """작업 등록 후 목록 조회가 가능해야 한다."""
    create_response = client.post(
        "/api/jobs",
        json={
            "title": "FastAPI 작업 생성 테스트",
            "seed_keywords": ["fastapi", "자동화", "테스트"],
            "platform": "naver",
            "persona_id": "P1",
            "scheduled_at": "2026-02-20T12:00:00+09:00",
            "topic_mode": "economy",
        },
    )
    assert create_response.status_code == 201
    create_payload = create_response.json()
    assert create_payload["status"] == "queued"
    assert create_payload["topic_mode"] == "finance"

    list_response = client.get("/api/jobs?page=1&size=20")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 1
    assert len(list_payload["items"]) == 1
    assert list_payload["items"][0]["title"] == "FastAPI 작업 생성 테스트"


def test_ops_check_and_text_archive_index(client: TestClient):
    """운영 점검과 텍스트 백업 인덱스를 조회할 수 있어야 한다."""
    store = app.dependency_overrides[get_job_store]()
    assert store.archive_post_text(
        job_id="archive-test-1",
        title="수정본 반영 테스트",
        final_content="본문입니다.\n\n| 구분 | 값 |\n| --- | --- |\n| 이미지 | 1 |",
        tags=["market_daily", "market_slot:kr_preopen"],
        category="경제 공부와 투자 기록",
        quality_snapshot={
            "score": 87,
            "manual_revision_applied": True,
            "insight_quality": {"overall_score": 82},
        },
        image_manifest={"summary": "summary-card.png"},
    )

    check_response = client.get("/api/ops/check")
    assert check_response.status_code == 200
    check_payload = check_response.json()
    assert "checks" in check_payload
    assert any(item["key"] == "database" for item in check_payload["checks"])

    backups_response = client.get("/api/ops/backups?limit=5")
    assert backups_response.status_code == 200
    backups_payload = backups_response.json()
    assert backups_payload["items"][0]["title"] == "수정본 반영 테스트"
    assert backups_payload["items"][0]["manual_revision_applied"] is True
    assert backups_payload["items"][0]["image_count"] == 1

    revisions_response = client.get("/api/ops/revisions?limit=5")
    assert revisions_response.status_code == 200
    revisions_payload = revisions_response.json()
    assert len(revisions_payload["items"]) == 1


def test_jobs_cancel_queued_success_without_idea_lock(client: TestClient):
    """아이디어 연계가 없는 queued 작업도 정상 취소되어야 한다."""
    create_response = client.post(
        "/api/jobs",
        json={
            "title": "취소 테스트 queued",
            "seed_keywords": ["cancel", "queued"],
            "platform": "naver",
            "persona_id": "P1",
        },
    )
    assert create_response.status_code == 201
    job_id = create_response.json()["job_id"]

    cancel_response = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 200
    cancel_payload = cancel_response.json()
    assert cancel_payload["ok"] is True
    assert cancel_payload["status"] == "cancelled"
    assert int(cancel_payload["released_idea_locks"]) == 0

    detail_response = client.get(f"/api/jobs/{job_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["status"] == "cancelled"
    assert detail_payload["error_code"] == "USER_CANCELLED"


def test_jobs_cancel_retry_wait_and_ready_to_publish(client: TestClient):
    """retry_wait/ready_to_publish 상태는 취소 가능해야 한다."""
    store = app.dependency_overrides[get_job_store]()

    retry_job_id = str(uuid.uuid4())
    ready_job_id = str(uuid.uuid4())
    assert store.schedule_job(
        job_id=retry_job_id,
        title="retry cancel",
        seed_keywords=["retry"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-20T00:00:00Z",
        status=store.STATUS_RETRY_WAIT,
    )
    assert store.schedule_job(
        job_id=ready_job_id,
        title="ready cancel",
        seed_keywords=["ready"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-20T00:00:00Z",
        status=store.STATUS_READY,
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE jobs SET next_retry_at = ? WHERE job_id = ?",
            ("2026-02-20T00:30:00Z", retry_job_id),
        )

    retry_cancel = client.post(f"/api/jobs/{retry_job_id}/cancel")
    assert retry_cancel.status_code == 200
    ready_cancel = client.post(f"/api/jobs/{ready_job_id}/cancel")
    assert ready_cancel.status_code == 200

    retry_detail = client.get(f"/api/jobs/{retry_job_id}").json()
    ready_detail = client.get(f"/api/jobs/{ready_job_id}").json()
    assert retry_detail["status"] == "cancelled"
    assert retry_detail["next_retry_at"] is None
    assert ready_detail["status"] == "cancelled"


def test_jobs_cancel_rejects_running_and_recancel(client: TestClient):
    """취소 불가 상태와 재취소 요청은 409를 반환해야 한다."""
    store = app.dependency_overrides[get_job_store]()

    running_job_id = str(uuid.uuid4())
    recancel_job_id = str(uuid.uuid4())
    assert store.schedule_job(
        job_id=running_job_id,
        title="running job",
        seed_keywords=["running"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-20T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    assert store.schedule_job(
        job_id=recancel_job_id,
        title="recancel job",
        seed_keywords=["queued"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-20T00:00:00Z",
        status=store.STATUS_QUEUED,
    )

    running_cancel = client.post(f"/api/jobs/{running_job_id}/cancel")
    assert running_cancel.status_code == 409
    assert "cancelable=queued,retry_wait,ready_to_publish" in running_cancel.text

    first_cancel = client.post(f"/api/jobs/{recancel_job_id}/cancel")
    assert first_cancel.status_code == 200
    second_cancel = client.post(f"/api/jobs/{recancel_job_id}/cancel")
    assert second_cancel.status_code == 409
    assert "status=cancelled" in second_cancel.text


def test_jobs_cancel_releases_idea_vault_lock(client: TestClient):
    """아이디어 창고에 연결된 queued 잡 취소 시 잠금이 해제되어야 한다."""
    store = app.dependency_overrides[get_job_store]()
    job_id = str(uuid.uuid4())
    assert store.schedule_job(
        job_id=job_id,
        title="idea vault cancel",
        seed_keywords=["idea"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-20T00:00:00Z",
        status=store.STATUS_QUEUED,
    )
    inserted = store.add_idea_vault_items(
        [
            {
                "raw_text": "취소 시 잠금 해제 테스트",
                "mapped_category": "다양한 생각",
                "topic_mode": "cafe",
                "parser_used": "test",
            }
        ]
    )
    assert inserted == 1
    claimed = store.claim_random_idea_vault_items([job_id])
    assert len(claimed) == 1
    claimed_id = int(claimed[0]["id"])

    cancel_response = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 200
    cancel_payload = cancel_response.json()
    assert int(cancel_payload["released_idea_locks"]) == 1

    with store.connection() as conn:
        row = conn.execute(
            "SELECT status, queued_job_id FROM idea_vault WHERE id = ?",
            (claimed_id,),
        ).fetchone()
    assert row is not None
    assert str(row["status"]) == store.IDEA_STATUS_PENDING
    assert str(row["queued_job_id"]) == ""


def test_jobs_cancel_returns_404_when_missing(client: TestClient):
    """존재하지 않는 job_id 취소 요청은 404여야 한다."""
    response = client.post("/api/jobs/not-found-job/cancel")
    assert response.status_code == 404


def test_metrics_returns_recent_rows(client: TestClient):
    """metrics 엔드포인트가 최근 post_metrics 데이터를 반환해야 한다."""
    create_response = client.post(
        "/api/jobs",
        json={
            "title": "메트릭 테스트 작업",
            "seed_keywords": ["메트릭"],
            "platform": "naver",
            "persona_id": "P1",
        },
    )
    job_id = create_response.json()["job_id"]

    store = app.dependency_overrides[get_job_store]()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO post_metrics (
                post_id, job_id, title, url, published_at, views, likes, comments, snapshot_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "post-1",
                job_id,
                "메트릭 테스트 작업",
                "https://blog.naver.com/test/1",
                "2026-02-20T01:00:00Z",
                123,
                4,
                1,
                "2026-02-20T02:00:00Z",
            ),
        )

    response = client.get("/api/metrics?page=1&size=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["summary"]["total_views"] == 123
    assert payload["items"][0]["job_id"] == job_id


def test_llm_metrics_includes_token_averages(client: TestClient):
    """LLM 메트릭 엔드포인트는 평균 입력/출력 토큰을 함께 반환해야 한다."""
    create_response = client.post(
        "/api/jobs",
        json={
            "title": "LLM 메트릭 테스트",
            "seed_keywords": ["토큰", "메트릭"],
            "platform": "naver",
            "persona_id": "P1",
        },
    )
    job_id = create_response.json()["job_id"]

    store = app.dependency_overrides[get_job_store]()
    store.record_job_metric(
        job_id=job_id,
        metric_type="quality_step",
        status="ok",
        duration_ms=250.0,
        input_tokens=1200,
        output_tokens=800,
        provider="qwen",
        detail={"model": "qwen-plus", "calls": 2},
    )

    response = client.get("/api/metrics/llm?hours=24")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_llm_calls"] >= 1

    target = next((item for item in payload["by_type"] if item["metric_type"] == "quality_step"), None)
    assert target is not None
    assert target["avg_input_tokens"] >= 1200
    assert target["avg_output_tokens"] >= 800


def test_config_readonly_masked_keys(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """설정 조회는 키 원문 대신 마스킹 문자열만 반환해야 한다."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    response = client.get("/api/config")
    assert response.status_code == 200
    payload = response.json()

    key_map = {item["provider"]: item for item in payload["api_keys"]}
    assert key_map["openai"]["configured"] is True
    assert key_map["openai"]["masked"].startswith("sk-****")
    assert key_map["deepseek"]["configured"] is True
    assert "deepseek-secret" not in response.text
    assert key_map["dashscope"]["configured"] is False

    persona_values = [item["value"] for item in payload["personas"]]
    assert persona_values == ["P1", "P2", "P3", "P4"]

    topic_values = [item["value"] for item in payload["topic_modes"]]
    assert topic_values == ["cafe", "parenting", "it", "finance", "health", "economy"]


def test_onboarding_wizard_roundtrip(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """온보딩 Step1~3 저장과 완료 플래그를 검증한다."""
    import server.routers.onboarding as onboarding_router

    initial = client.get("/api/onboarding")
    assert initial.status_code == 200
    assert initial.json()["completed"] is False

    persona_response = client.post(
        "/api/onboarding/persona",
        json={
            "persona_id": "P2",
            "identity": "IT 직장인",
            "target_audience": "20대 개발 입문자",
            "tone_hint": "친근하지만 논리적",
            "interests": ["AI 자동화", "생산성 앱"],
            "structure_score": 82,
            "evidence_score": 77,
            "distance_score": 55,
            "criticism_score": 40,
            "density_score": 68,
            "style_strength": 45,
        },
    )
    assert persona_response.status_code == 200
    persona_payload = persona_response.json()
    assert persona_payload["persona_id"] == "P2"
    assert persona_payload["voice_profile"]["structure"] == "top_down"

    category_response = client.post(
        "/api/onboarding/categories",
        json={
            "categories": ["AI 자동화", "생산성 팁"],
            "fallback_category": "다양한 생각",
        },
    )
    assert category_response.status_code == 200
    category_payload = category_response.json()
    assert "다양한 생각" in category_payload["categories"]

    schedule_response = client.post(
        "/api/onboarding/schedule",
        json={
            "daily_posts_target": 4,
            "idea_vault_daily_quota": 1,
            "allocations": [
                {"category": "AI 자동화", "topic_mode": "it", "count": 2},
                {"category": "생산성 팁", "topic_mode": "it", "count": 1},
                {"category": "다양한 생각", "topic_mode": "cafe", "count": 1},
            ],
        },
    )
    assert schedule_response.status_code == 200
    schedule_payload = schedule_response.json()
    assert schedule_payload["daily_posts_target"] == 4
    assert schedule_payload["idea_vault_daily_quota"] == 1
    assert sum(item["count"] for item in schedule_payload["allocations"]) == 3

    async def fake_send_message(self, text: str, disable_notification: bool = False) -> bool:
        del self, text, disable_notification
        return True

    monkeypatch.setattr(onboarding_router.TelegramNotifier, "send_message", fake_send_message)
    telegram_response = client.post(
        "/api/onboarding/telegram/test",
        json={
            "bot_token": "test-bot-token",
            "chat_id": "123456789",
            "save": True,
        },
    )
    assert telegram_response.status_code == 200
    assert telegram_response.json()["success"] is True

    complete_response = client.post("/api/onboarding/complete")
    assert complete_response.status_code == 200
    assert complete_response.json()["completed"] is True

    final_status = client.get("/api/onboarding")
    assert final_status.status_code == 200
    final_payload = final_status.json()
    assert final_payload["completed"] is True
    assert final_payload["persona_id"] == "P2"
    assert final_payload["daily_posts_target"] == 4
    assert final_payload["idea_vault_daily_quota"] == 1
    assert sum(item["count"] for item in final_payload["category_allocations"]) == 3
    assert final_payload["telegram_configured"] is True


def test_onboarding_persona_question_bank_endpoint(client: TestClient):
    """온보딩 페르소나 질문지 API가 기본 스키마를 반환해야 한다."""
    response = client.get("/api/onboarding/persona/questions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "v1"
    assert payload["required_count"] >= 5
    assert len(payload["questions"]) >= 7
    first_question = payload["questions"][0]
    assert first_question["question_id"] != ""
    assert len(first_question["options"]) >= 3


def test_telegram_verify_token_and_webhook_auth_flow(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """토큰 검증 + 웹훅 인증코드로 chat_id 자동 저장이 동작해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    telegram_router._PENDING_AUTH_CODES.clear()

    class _MockResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

        async def get(self, url: str, params: dict | None = None):
            del params
            if "getMe" in url:
                return _MockResponse({"ok": True, "result": {"username": "autoblog_test_bot"}})
            return _MockResponse({"ok": True, "result": []})

        async def post(self, url: str, json: dict | None = None):
            del url, json
            return _MockResponse({"ok": True})

    async def _fake_send_reply(bot_token: str, chat_id: int | str, text: str) -> None:
        del bot_token, chat_id, text
        return None

    monkeypatch.setattr(telegram_router.httpx, "AsyncClient", lambda *args, **kwargs: _MockAsyncClient())
    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    token_response = client.post(
        "/api/telegram/verify-token",
        json={"bot_token": "123456789:ABCdef_token"},
    )
    assert token_response.status_code == 200
    token_payload = token_response.json()
    assert token_payload["success"] is True
    assert token_payload["bot_username"] == "autoblog_test_bot"
    auth_code = token_payload["auth_code"]
    assert auth_code

    webhook_response = client.post(
        "/api/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 777001, "type": "private"},
                "text": f"/start autoblog_{auth_code}",
            }
        },
    )
    assert webhook_response.status_code == 200
    assert webhook_response.json()["auth_verified"] is True

    verify_response = client.post(
        "/api/telegram/verify",
        json={"auth_code": auth_code},
    )
    assert verify_response.status_code == 200
    verify_payload = verify_response.json()
    assert verify_payload["success"] is True
    assert verify_payload["chat_id"] == "777001"
    assert verify_payload["used_fallback"] is False

    onboarding_response = client.get("/api/onboarding")
    assert onboarding_response.status_code == 200
    onboarding_payload = onboarding_response.json()
    assert onboarding_payload["telegram_configured"] is True
    assert onboarding_payload["telegram_chat_id"] == "777001"


def test_telegram_verify_uses_getupdates_fallback(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """웹훅 수신이 없을 때 getUpdates 폴백으로 인증을 완료해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    telegram_router._PENDING_AUTH_CODES.clear()

    update_cache: list[dict] = []

    class _MockResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

        async def get(self, url: str, params: dict | None = None):
            del params
            if "getMe" in url:
                return _MockResponse({"ok": True, "result": {"username": "autoblog_fallback_bot"}})
            if "getUpdates" in url:
                return _MockResponse({"ok": True, "result": update_cache})
            return _MockResponse({"ok": True, "result": []})

        async def post(self, url: str, json: dict | None = None):
            del url, json
            return _MockResponse({"ok": True})

    async def _fake_send_reply(bot_token: str, chat_id: int | str, text: str) -> None:
        del bot_token, chat_id, text
        return None

    monkeypatch.setattr(telegram_router.httpx, "AsyncClient", lambda *args, **kwargs: _MockAsyncClient())
    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    token_response = client.post(
        "/api/telegram/verify-token",
        json={"bot_token": "123456789:ABCdef_token"},
    )
    assert token_response.status_code == 200
    auth_code = token_response.json()["auth_code"]
    assert auth_code

    update_cache.append(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 991122, "type": "private"},
                "text": f"/start autoblog_{auth_code}",
            },
        }
    )

    verify_response = client.post(
        "/api/telegram/verify",
        json={"auth_code": auth_code},
    )
    assert verify_response.status_code == 200
    verify_payload = verify_response.json()
    assert verify_payload["success"] is True
    assert verify_payload["chat_id"] == "991122"
    assert verify_payload["used_fallback"] is True


def test_telegram_verify_requires_private_chat(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """인증코드 메시지는 private 채팅에서만 승인되어야 한다."""
    import server.routers.telegram_webhook as telegram_router

    telegram_router._PENDING_AUTH_CODES.clear()

    class _MockResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

        async def get(self, url: str, params: dict | None = None):
            del params
            if "getMe" in url:
                return _MockResponse({"ok": True, "result": {"username": "autoblog_group_bot"}})
            return _MockResponse({"ok": True, "result": []})

        async def post(self, url: str, json: dict | None = None):
            del url, json
            return _MockResponse({"ok": True})

    async def _fake_send_reply(bot_token: str, chat_id: int | str, text: str) -> None:
        del bot_token, chat_id, text
        return None

    monkeypatch.setattr(telegram_router.httpx, "AsyncClient", lambda *args, **kwargs: _MockAsyncClient())
    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    token_response = client.post(
        "/api/telegram/verify-token",
        json={"bot_token": "123456789:ABCdef_token"},
    )
    assert token_response.status_code == 200
    auth_code = token_response.json()["auth_code"]
    assert auth_code

    webhook_response = client.post(
        "/api/telegram/webhook",
        json={
            "message": {
                "chat": {"id": -1002211, "type": "group"},
                "text": f"/start autoblog_{auth_code}",
            }
        },
    )
    assert webhook_response.status_code == 200
    assert webhook_response.json()["reason"] == "auth_requires_private_chat"

    verify_response = client.post(
        "/api/telegram/verify",
        json={"auth_code": auth_code},
    )
    assert verify_response.status_code == 409


def test_telegram_webhook_callback_query_approve_feedback_candidate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """텔레그램 callback_query로 피드백 후보 승인 처리가 동작해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")

    candidate = None
    for _ in range(5):
        candidate = store.record_feedback_suggestion_observation(
            suggestion_text="이미지와 본문 단락 사이 간격을 조금 더 넓히세요",
            visual_score=79.0,
        )
    assert candidate is not None
    assert candidate["status"] == "pending_approval"

    prepared = store.prepare_feedback_candidate_notification(candidate["id"], callback_ttl_hours=24)
    assert prepared is not None
    callback_token = prepared["callback_token"]

    answered: list[str] = []
    replied: list[str] = []

    async def _fake_answer_callback_query(
        bot_token: str,
        callback_query_id: str,
        text: str,
        *,
        show_alert: bool = False,
    ) -> None:
        del bot_token, callback_query_id, show_alert
        answered.append(text)

    async def _fake_send_reply(bot_token: str, chat_id: int | str, text: str) -> None:
        del bot_token, chat_id
        replied.append(text)

    monkeypatch.setattr(telegram_router, "_answer_callback_query", _fake_answer_callback_query)
    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    response = client.post(
        "/api/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq_001",
                "data": f"afl:v1:a:{candidate['id']}:{callback_token}",
                "message": {"chat": {"id": 777001, "type": "private"}},
            }
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["callback_handled"] is True
    assert payload["callback_action"] == "approve"
    assert any("자동 반영" in text for text in answered)
    assert any("다음 포스트부터 자동 반영" in text for text in replied)

    active_rules = store.list_active_feedback_rules(limit=3)
    assert len(active_rules) == 1

    duplicate = client.post(
        "/api/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq_002",
                "data": f"afl:v1:a:{candidate['id']}:{callback_token}",
                "message": {"chat": {"id": 777001, "type": "private"}},
            }
        },
    )
    assert duplicate.status_code == 200
    duplicate_payload = duplicate.json()
    assert duplicate_payload["callback_handled"] is False
    assert duplicate_payload["reason"] == "already_handled"


def test_telegram_webhook_callback_query_promotes_macro_candidate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """텔레그램 callback_query로 매크로 후보를 초안 생성 큐에 올릴 수 있어야 한다."""
    import server.routers.telegram_webhook as telegram_router
    from modules.macro.telegram_approval import build_callback_data

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")

    document = store.upsert_macro_document(
        {
            "source": "MOTIE",
            "title": "2026년 5월 수출입 동향",
            "published_at": "2026-06-01",
            "url": "https://example.test/motie",
            "file_url": "",
            "file_type": "html",
            "attachments_json": [],
            "status": "analyzed",
            "hash": "macro-api-test-hash",
        }
    )
    candidate = store.replace_macro_blog_candidates(
        document["id"],
        [
            {
                "title": "대미 수출 증가는 한국 경제에 어떤 의미를 줄까",
                "angle": "미국 연결",
                "target_reader": "미국 매크로와 한국 수출을 함께 보는 독자",
                "outline_json": {"sections": ["대미 수출", "한국 ETF 관점"]},
                "status": "needs_review",
            }
        ],
    )[0]

    answered: list[str] = []
    replied: list[str] = []

    async def _fake_answer_callback_query(
        bot_token: str,
        callback_query_id: str,
        text: str,
        *,
        show_alert: bool = False,
    ) -> None:
        del bot_token, callback_query_id, show_alert
        answered.append(text)

    async def _fake_send_reply(
        bot_token: str,
        chat_id: int | str,
        text: str,
        *,
        reply_markup=None,
    ) -> None:
        del bot_token, chat_id, reply_markup
        replied.append(text)

    monkeypatch.setattr(telegram_router, "_answer_callback_query", _fake_answer_callback_query)
    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    response = client.post(
        "/api/telegram/webhook",
        json={
            "callback_query": {
                "id": "macro_cbq_001",
                "data": build_callback_data(action="promote", candidate_id=candidate["id"]),
                "message": {"chat": {"id": 777001, "type": "private"}},
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["callback_handled"] is True
    assert payload["callback_action"] == "promote"
    assert any("초안 생성 큐" in text for text in answered)
    assert any("job_id:" in text for text in replied)

    updated_candidate = store.get_macro_blog_candidate(candidate["id"])
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT title, tags FROM jobs WHERE title = ?",
            (candidate["title"],),
        ).fetchall()
    assert updated_candidate["status"] == "approved"
    assert len(rows) == 1
    assert f"macro_candidate:{candidate['id']}" in rows[0]["tags"]


def test_onboarding_persona_questionnaire_answers_apply_scores(client: TestClient):
    """질문지 응답이 있으면 슬라이더 대신 질문지 점수를 우선 반영해야 한다."""
    response = client.post(
        "/api/onboarding/persona",
        json={
            "persona_id": "P2",
            "identity": "테스터",
            "target_audience": "일반 독자",
            "tone_hint": "명확한 설명",
            "interests": ["AI"],
            "mbti": "",
            "mbti_enabled": False,
            "mbti_confidence": 0,
            "questionnaire_version": "v1",
            "questionnaire_answers": [
                {"question_id": "q1_opening_flow", "option_id": "a_scan_then_map"},
                {"question_id": "q2_evidence_conflict", "option_id": "a_add_sources"},
                {"question_id": "q3_reader_distance", "option_id": "a_calm_data_reply"},
                {"question_id": "q5_density_tradeoff", "option_id": "a_checklist_numbers"},
                {"question_id": "q7_uncertain_fact", "option_id": "a_mark_unknown"},
            ],
            "age_group": "30대",
            "gender": "비공개",
            "structure_score": 10,
            "evidence_score": 10,
            "distance_score": 10,
            "criticism_score": 10,
            "density_score": 10,
            "style_strength": 40,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    voice_profile = payload["voice_profile"]
    assert voice_profile["scores"]["structure"] > 50
    assert voice_profile["scores"]["evidence"] > 50
    assert voice_profile["questionnaire_meta"]["source"] == "questionnaire"
    assert voice_profile["questionnaire_meta"]["answered_count"] == 5
    assert voice_profile["blending"]["mbti_applied"] is False


def test_onboarding_persona_mbti_blending_optional(client: TestClient):
    """MBTI 미입력 시 질문지만 사용하고, 입력 시 보조 혼합되어야 한다."""
    # 1) MBTI 비활성: 질문지 점수 그대로 반영
    disabled_response = client.post(
        "/api/onboarding/persona",
        json={
            "persona_id": "P1",
            "identity": "테스터",
            "target_audience": "일반 독자",
            "tone_hint": "담백한 설명체",
            "interests": ["커피"],
            "mbti": "ENFP",
            "mbti_enabled": False,
            "mbti_confidence": 80,
            "age_group": "30대",
            "gender": "비공개",
            "structure_score": 61,
            "evidence_score": 48,
            "distance_score": 44,
            "criticism_score": 53,
            "density_score": 52,
            "style_strength": 40,
        },
    )
    assert disabled_response.status_code == 200
    disabled_payload = disabled_response.json()
    disabled_voice = disabled_payload["voice_profile"]
    assert disabled_voice["mbti_enabled"] is False
    assert disabled_voice["mbti"] == ""
    assert disabled_voice["scores"]["structure"] == 61
    assert disabled_voice["scores"]["evidence"] == 48
    assert disabled_voice["blending"]["mbti_weight"] == 0.0

    # 2) MBTI 활성: 질문지 + MBTI prior 혼합
    enabled_response = client.post(
        "/api/onboarding/persona",
        json={
            "persona_id": "P1",
            "identity": "테스터",
            "target_audience": "일반 독자",
            "tone_hint": "담백한 설명체",
            "interests": ["커피"],
            "mbti": "ENTJ",
            "mbti_enabled": True,
            "mbti_confidence": 100,
            "age_group": "30대",
            "gender": "비공개",
            "structure_score": 50,
            "evidence_score": 50,
            "distance_score": 50,
            "criticism_score": 50,
            "density_score": 50,
            "style_strength": 40,
        },
    )
    assert enabled_response.status_code == 200
    enabled_payload = enabled_response.json()
    enabled_voice = enabled_payload["voice_profile"]
    assert enabled_voice["mbti_enabled"] is True
    assert enabled_voice["mbti"] == "ENTJ"
    assert enabled_voice["blending"]["mbti_applied"] is True
    assert enabled_voice["blending"]["mbti_weight"] >= 0.1
    assert enabled_voice["scores"]["structure"] > 50
    assert enabled_voice["scores"]["criticism"] > 50


def test_onboarding_schedule_clamps_idea_vault_quota(client: TestClient):
    """Idea Vault 할당량은 0~daily_posts_target 범위로 안전 보정해야 한다."""
    response = client.post(
        "/api/onboarding/schedule",
        json={
            "daily_posts_target": 3,
            "idea_vault_daily_quota": 20,
            "allocations": [
                {"category": "다양한 생각", "topic_mode": "cafe", "count": 1},
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["daily_posts_target"] == 3
    assert payload["idea_vault_daily_quota"] == 3
    assert sum(item["count"] for item in payload["allocations"]) == 0


def test_magic_input_parsing_and_job_creation(client: TestClient):
    """매직 인풋이 파싱되고 Job 큐 등록까지 이어져야 한다."""
    parse_response = client.post(
        "/api/magic-input/parse",
        json={
            "instruction": "이번 주 스벅 신메뉴 리뷰 글 하나 써줘. P2 스타일로 위트있게.",
        },
    )
    assert parse_response.status_code == 200
    parse_payload = parse_response.json()
    assert parse_payload["title"] != ""
    assert len(parse_payload["seed_keywords"]) >= 1
    assert parse_payload["persona_id"] in {"P1", "P2", "P3", "P4"}
    assert "schedule_time" in parse_payload

    create_response = client.post(
        "/api/magic-input/jobs",
        json={
            "instruction": "이번 주 스벅 신메뉴 리뷰 글 하나 써줘. P2 스타일로 위트있게.",
            "platform": "naver",
        },
    )
    assert create_response.status_code == 201
    created_payload = create_response.json()
    assert created_payload["job_id"] != ""
    assert created_payload["status"] == "queued"
    assert created_payload["parser_used"] in {"heuristic", "qwen", "deepseek", "zai", "gemini", "groq", "cerebras"}

    list_response = client.get("/api/jobs?page=1&size=20")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1


def test_magic_input_job_uses_parser_schedule_when_request_missing(client: TestClient):
    """요청에 시간이 없으면 파서가 추출한 schedule_time을 사용해야 한다."""

    class _ParserStub:
        async def parse(self, instruction: str) -> MagicInputParseResult:
            del instruction
            return MagicInputParseResult(
                title="스케줄 파서 테스트",
                seed_keywords=["스케줄", "테스트"],
                persona_id="P2",
                topic_mode="it",
                schedule_time="2026-02-21T00:00:00Z",
                confidence=0.9,
                parser_used="stub",
                raw={"source": "test"},
            )

    app.dependency_overrides[get_magic_input_parser] = lambda: _ParserStub()
    try:
        response = client.post(
            "/api/magic-input/jobs",
            json={
                "instruction": "내일 아침 9시에 올려줘",
                "platform": "naver",
            },
        )
    finally:
        app.dependency_overrides.pop(get_magic_input_parser, None)

    assert response.status_code == 201
    payload = response.json()
    assert payload["scheduled_at"] == "2026-02-21T00:00:00Z"


def test_idea_vault_ingest_and_stats(client: TestClient):
    """아이디어 창고 적재 후 통계가 반영되어야 한다."""

    class _IdeaParserStub:
        async def parse_bulk(self, raw_text: str, *, categories: list[str], batch_size: int = 20):
            del raw_text, categories, batch_size
            return IdeaVaultParseResult(
                total_lines=3,
                accepted_items=[
                    IdeaVaultParsedItem(
                        raw_text="카페 오픈 루틴 점검법",
                        mapped_category="다양한 생각",
                        topic_mode="cafe",
                        parser_used="stub",
                    ),
                    IdeaVaultParsedItem(
                        raw_text="IT 자동화로 업무시간 절약한 실제 사례",
                        mapped_category="다양한 생각",
                        topic_mode="it",
                        parser_used="stub",
                    ),
                ],
                rejected_lines=[{"line": "!!!", "reason": "품질 미달"}],
                parser_used="stub",
            )

    app.dependency_overrides[get_idea_vault_parser] = lambda: _IdeaParserStub()
    try:
        response = client.post(
            "/api/idea-vault/ingest",
            json={
                "raw_text": "라인1\n라인2\n라인3",
                "batch_size": 20,
            },
        )
    finally:
        app.dependency_overrides.pop(get_idea_vault_parser, None)

    assert response.status_code == 201
    payload = response.json()
    assert payload["total_lines"] == 3
    assert payload["accepted_count"] == 2
    assert payload["rejected_count"] == 1
    assert payload["pending_count"] == 2

    stats_response = client.get("/api/idea-vault/stats")
    assert stats_response.status_code == 200
    stats_payload = stats_response.json()
    assert stats_payload["total"] == 2
    assert stats_payload["pending"] == 2


def test_router_settings_quote_and_save(client: TestClient):
    """제로-설정 라우터 견적/저장 API가 동작해야 한다."""
    initial = client.get("/api/router-settings")
    assert initial.status_code == 200
    initial_payload = initial.json()
    assert "matrix" in initial_payload
    assert "competition" in initial_payload
    assert len(initial_payload["matrix"]["text_models"]) >= 1
    assert len(initial_payload["matrix"]["vlm_models"]) >= 1
    first_vlm = initial_payload["matrix"]["vlm_models"][0]
    assert "model" in first_vlm
    assert "status" in first_vlm
    assert "quality_score" in first_vlm
    assert "estimated_cost_krw" in first_vlm

    quote = client.post(
        "/api/router-settings/quote",
        json={
            "strategy_mode": "cost",
            "text_api_keys": {"deepseek": "ds-test-key"},
            "image_api_keys": {"pexels": "pex-test-key"},
            "image_engine": "pexels",
            "image_enabled": True,
            "images_per_post": 1,
        },
    )
    assert quote.status_code == 200
    quote_payload = quote.json()
    assert quote_payload["estimate"]["total_cost_krw"] >= 0
    assert quote_payload["estimate"]["quality_score"] >= 0
    assert quote_payload["strategy_mode"] == "cost"

    saved = client.post(
        "/api/router-settings/save",
        json={
            "strategy_mode": "quality",
            "text_api_keys": {"gemini": "gm-test-key"},
            "image_api_keys": {"fal": "fal-test-key"},
            "image_engine": "fal_flux",
            "image_enabled": True,
            "images_per_post": 2,
        },
    )
    assert saved.status_code == 200
    saved_payload = saved.json()
    assert saved_payload["settings"]["strategy_mode"] == "quality"
    assert saved_payload["settings"]["image_engine"] == "fal_flux"
    assert "phase" in saved_payload["competition"]


def test_router_settings_exposes_default_topic_quota_overrides(client: TestClient):
    """초기 상태에서도 토픽별 기본 quota override가 노출되어야 한다."""
    response = client.get("/api/router-settings")
    assert response.status_code == 200
    payload = response.json()
    overrides = payload["settings"]["image_topic_quota_overrides"]
    assert payload["settings"]["traffic_feedback_strong_mode"] is False
    assert overrides["cafe"] == "0"
    assert overrides["it"] == "1"
    assert overrides["finance"] == "1"
    assert overrides["parenting"] == "0"


def test_router_settings_save_supports_image_ai_fields(client: TestClient):
    """router_image_ai_quota/engine/topic_overrides 저장이 가능해야 한다."""
    response = client.post(
        "/api/router-settings/save",
        json={
            "strategy_mode": "cost",
            "text_api_keys": {"qwen": "qwen-test-key"},
            "image_api_keys": {"together": "together-test-key"},
            "image_engine": "together_flux",
            "image_ai_engine": "together_flux",
            "image_ai_quota": "1",
            "image_topic_quota_overrides": {"it": "1", "cafe": "0"},
            "traffic_feedback_strong_mode": True,
            "image_enabled": True,
            "images_per_post": 4,
            "images_per_post_min": 0,
            "images_per_post_max": 4,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    settings = payload["settings"]
    assert settings["image_ai_engine"] == "together_flux"
    assert settings["image_ai_quota"] == "1"
    assert settings["image_topic_quota_overrides"]["it"] == "1"
    assert settings["image_topic_quota_overrides"]["cafe"] == "0"
    assert settings["traffic_feedback_strong_mode"] is True


def test_router_settings_save_supports_vlm_fields(client: TestClient):
    """VLM 토글/모델/전략 설정이 저장되어야 한다."""
    response = client.post(
        "/api/router-settings/save",
        json={
            "strategy_mode": "balanced",
            "text_api_keys": {"nvidia": "nv-test-key"},
            "image_api_keys": {"pexels": "pex-test-key"},
            "image_engine": "pexels",
            "image_enabled": True,
            "images_per_post": 1,
            "vlm_enabled": True,
            "vlm_model": "meta/llama-3.2-90b-vision-instruct",
            "vlm_strategy_mode": "inherit",
            "vlm_eval_sampling_rate": 0.45,
            "vlm_quality_floor": 70.0,
            "vlm_max_cost_guard_krw": 25.0,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["vlm_enabled"] is True
    assert payload["settings"]["vlm_model"] == "meta/llama-3.2-90b-vision-instruct"
    assert payload["settings"]["vlm_strategy_mode"] == "inherit"
    assert payload["settings"]["vlm_eval_sampling_rate"] == 0.45
    assert payload["settings"]["vlm_quality_floor"] == 70.0
    assert payload["settings"]["vlm_max_cost_guard_krw"] == 25.0


def test_router_quote_includes_ai_stock_image_count_split(client: TestClient):
    """견적 응답은 AI/스톡 이미지 수 분리 값을 포함해야 한다."""
    response = client.post(
        "/api/router-settings/quote",
        json={
            "strategy_mode": "cost",
            "text_api_keys": {"qwen": "qwen-test-key"},
            "image_api_keys": {"fal": "fal-test-key", "pexels": "pex-test-key"},
            "image_engine": "pexels",
            "image_ai_engine": "fal_flux",
            "image_ai_quota": "1",
            "image_enabled": True,
            "images_per_post": 4,
            "images_per_post_min": 0,
            "images_per_post_max": 4,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    estimate = payload["estimate"]
    assert estimate["ai_image_count"] == 1
    assert estimate["stock_image_count"] == 3
    assert estimate["ai_image_count_min"] == 0


def test_naver_connect_status_endpoint(client: TestClient):
    """네이버 연동 상태 조회 API가 기본 필드를 반환해야 한다."""
    response = client.get("/api/naver/connect/status")
    assert response.status_code == 200
    payload = response.json()
    assert "connected" in payload
    assert "state_path" in payload
