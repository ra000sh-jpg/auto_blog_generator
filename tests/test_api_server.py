from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from modules.automation.job_store import JobStore
from modules.llm.idea_vault_parser import IdeaVaultParseResult, IdeaVaultParsedItem
from modules.llm.magic_input_parser import MagicInputParseResult
from server.dependencies import get_idea_vault_parser, get_job_store, get_magic_input_parser
from server.main import app


@pytest.fixture
def client(tmp_path) -> Generator[TestClient, None, None]:
    """테스트용 FastAPI 클라이언트."""
    db_path = tmp_path / "api_test.db"
    store = JobStore(db_path=str(db_path))

    app.dependency_overrides[get_job_store] = lambda: store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_degraded_when_api_key_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """API 키 누락 실패는 500이 아닌 degraded로 반환해야 한다."""
    import server.routers.health as health_router

    async def fake_check_all_providers(skip_expensive=True, llm_config=None):
        del skip_expensive, llm_config
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
    assert topic_values == ["cafe", "parenting", "it", "finance", "economy"]


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
    assert created_payload["parser_used"] in {"heuristic", "qwen", "deepseek", "gemini"}

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
