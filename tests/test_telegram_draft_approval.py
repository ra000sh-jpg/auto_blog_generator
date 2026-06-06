from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import pytest
from fastapi.testclient import TestClient

from modules.automation.draft_approval import (
    DraftApprovalRequest,
    build_draft_compact_message,
    build_draft_text_attachment,
    build_inline_keyboard,
    expire_pending_draft_revision_sessions,
    parse_draft_callback_data,
    start_draft_revision_session,
)
from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.uploaders.playwright_publisher import PublishResult
from server.dependencies import get_job_store
from server.main import app


class DummyPublisher:
    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[list[str]] = None,
        image_sources: Optional[dict[str, dict[str, str]]] = None,
        image_points: Optional[list[Any]] = None,
        tags: Optional[list[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/approved")


class CaptureNotifier:
    def __init__(self) -> None:
        self.enabled = True
        self.messages: list[str] = []
        self.documents: list[Dict[str, Any]] = []
        self.reply_markups: list[Dict[str, Any]] = []

    def send_message_background(
        self,
        text: str,
        *,
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        del disable_notification
        self.messages.append(text)
        if reply_markup:
            self.reply_markups.append(reply_markup)

    def send_document_background(
        self,
        *,
        file_path: str,
        caption: str = "",
        filename: str = "",
        disable_notification: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        del disable_notification
        self.documents.append(
            {
                "file_path": file_path,
                "caption": caption,
                "filename": filename,
            }
        )
        if reply_markup:
            self.reply_markups.append(reply_markup)


def test_draft_compact_message_includes_writing_strategy_summary():
    """승인 메시지는 본문 대신 전략 요약과 TXT 첨부 안내를 보여준다."""

    payload = {
        "content": "본문",
        "tags": ["경제공부"],
        "quality_snapshot": {
            "writing_strategy": {
                "label": "국장전 시나리오 브리핑형",
                "intent_label": "뉴스 해설",
                "axis_summary": "근거 35% + 체크리스트 25% + 리스크 25%",
            }
        },
    }

    message = build_draft_compact_message(
        job_id="strategy-message-job",
        title="국장전 브리핑",
        payload=payload,
        expires_at="2026-06-08T00:00:00Z",
    )
    attachment = build_draft_text_attachment(
        job_id="strategy-message-job",
        title="국장전 브리핑",
        payload=payload,
        expires_at="2026-06-08T00:00:00Z",
    )

    assert "추천 전략: 국장전 시나리오 브리핑형" in message
    assert "검색 의도: 뉴스 해설" in message
    assert "전략 비율: 근거 35% + 체크리스트 25% + 리스크 25%" in message
    assert "본문 TXT: 첨부됨" in message
    assert "writing_strategy: 국장전 시나리오 브리핑형" in attachment


def build_store(tmp_path: Path, name: str = "draft_approval.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """초안 승인 웹훅 테스트용 FastAPI 클라이언트."""
    store = build_store(tmp_path, "draft_approval_api.db")
    app.dependency_overrides[get_job_store] = lambda: store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _generate_content(_job: Any) -> Dict[str, Any]:
    title = str(getattr(_job, "title", "국장 시작 전 마음이 먼저 흔들리는 이유"))
    content = (
        f"## {title}\n\n"
        f"{title}를 생각할 때 국장 초심자에게 가장 중요한 판단은 방향을 맞히는 능력보다 "
        "자기 감정이 먼저 움직이는 순간을 알아차리는 일입니다. 시장은 늘 불완전한 정보 위에서 "
        "움직이고, 투자자는 그 불완전함을 견디는 방식으로 자신의 원칙을 확인합니다.\n\n"
        "## 브리핑을 읽는 태도\n\n"
        "국장 개장 전 브리핑은 오늘 무엇이 오를지 단정하는 문서가 아니라, 초심자에게 필요한 "
        "판단의 순서를 정돈하는 문서여야 합니다. 전일 미장 흐름, 환율, 금리, 위험 선호의 변화를 "
        "한 줄로 묶어보면 시장을 보는 마음도 조금 차분해집니다.\n\n"
        "## 적용 관점\n\n"
        "판단이 흔들릴수록 더 많은 정보를 모으려 하기 쉽지만, 실제로 필요한 것은 핵심 변수의 "
        "우선순위를 정하는 일입니다. 그래서 좋은 자동 블로그 초안은 데이터와 통찰을 함께 보여주되 "
        "읽는 사람이 자신의 매매 태도를 돌아볼 여백을 남겨야 합니다.\n\n"
        "참고 자료: Stooq 시장 데이터 (https://stooq.com)\n"
    )
    return {
        "final_content": content,
        "quality_gate": "pass",
        "quality_snapshot": {"score": 92, "issues": []},
        "seo_snapshot": {"provider_used": "stub", "provider_model": "stub", "topic_mode": "economy"},
        "image_prompts": [],
        "llm_token_usage": {},
    }


def test_pipeline_generation_waits_for_telegram_draft_approval(tmp_path: Path):
    """승인 모드에서는 생성 결과가 ready가 아니라 awaiting_approval로 보관되어야 한다."""
    store = build_store(tmp_path, "draft_approval_pipeline.db")
    store.set_system_setting("telegram_draft_approval_enabled", "true")
    due_now = "2026-06-05T00:00:00Z"
    assert store.schedule_job(
        job_id="draft-approval-job",
        title="국장 시작 전 마음이 먼저 흔들리는 이유",
        seed_keywords=["국장", "초심자", "판단"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1

    notifier = CaptureNotifier()
    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=_generate_content,
        notifier=notifier,  # type: ignore[arg-type]
    )

    assert asyncio.run(pipeline.process_generation(claimed[0])) is True
    updated = store.get_job("draft-approval-job")
    assert updated is not None
    assert updated.status == store.STATUS_AWAITING_APPROVAL
    assert bool(updated.prepared_payload)
    assert notifier.documents
    assert "AutoBlog 초안 승인 요청" in notifier.documents[0]["caption"]
    draft_file = Path(notifier.documents[0]["file_path"])
    assert draft_file.exists()
    draft_text = draft_file.read_text(encoding="utf-8")
    assert "job_id: draft-approval-job" in draft_text
    assert "--- 본문 시작 ---" in draft_text
    assert "브리핑을 읽는 태도" in draft_text

    callback_data = notifier.reply_markups[0]["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_draft_callback_data(callback_data)
    assert parsed is not None

    from modules.automation.draft_approval import apply_draft_callback_action

    result = apply_draft_callback_action(
        store,
        approval_id=parsed["approval_id"],
        token=parsed["token"],
        action=parsed["action"],
    )
    assert result["ok"] is True
    approved = store.get_job("draft-approval-job")
    assert approved is not None
    assert approved.status == store.STATUS_READY


def test_draft_preview_marks_revision_needed():
    """통찰 품질이 낮은 초안은 텔레그램 미리보기에서 수정필요로 표시해야 한다."""
    from modules.automation.draft_approval import build_draft_preview_message

    message = build_draft_preview_message(
        job_id="draft-preview-job",
        title="국장 시작 전 기준을 세우는 법",
        payload={
            "content": "본문 미리보기입니다.",
            "quality_snapshot": {
                "insight_quality": {
                    "overall_score": 82,
                    "needs_rewrite": True,
                }
            },
        },
        expires_at="2026-06-06T00:00:00Z",
    )

    assert "수정필요" in message
    assert "통찰 품질 82/100" in message
    assert "수정본입력" in str(build_inline_keyboard(DraftApprovalRequest("a1", "t1", "draft-preview-job", "2026-06-06T00:00:00Z")))
    assert "/draft_update draft-preview-job" in message


def test_telegram_webhook_approves_draft_to_ready_queue(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """텔레그램 초안 승인 버튼이 ready_to_publish 승격을 수행해야 한다."""
    import server.routers.telegram_webhook as telegram_router
    from modules.automation.draft_approval import build_callback_data, create_draft_approval_request

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")

    assert store.schedule_job(
        job_id="draft-webhook-job",
        title="미장 시작 전 숫자보다 중요한 것",
        seed_keywords=["미장", "브리핑", "초심자"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-05T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    payload = {
        "title": "미장 시작 전 숫자보다 중요한 것",
        "content": "본문 초안입니다. 판단을 돕는 텍스트입니다.",
        "tags": ["미장", "투자초심자"],
        "category": "economy",
    }
    assert store.save_prepared_payload("draft-webhook-job", payload, mark_ready=False) is True
    assert store.update_job_status("draft-webhook-job", store.STATUS_AWAITING_APPROVAL) is True
    approval = create_draft_approval_request(
        store,
        job_id="draft-webhook-job",
        title="미장 시작 전 숫자보다 중요한 것",
    )

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
                "id": "cbq_draft_001",
                "data": build_callback_data(
                    action="approve",
                    approval_id=approval.approval_id,
                    token=approval.token,
                ),
                "message": {"chat": {"id": 777001, "type": "private"}},
            }
        },
    )
    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload["callback_handled"] is True
    assert response_payload["callback_action"] == "approve"
    assert any("발행 대기열" in text for text in answered)
    assert any("ready_to_publish" in text for text in replied)

    updated = store.get_job("draft-webhook-job")
    assert updated is not None
    assert updated.status == store.STATUS_READY


def test_telegram_webhook_updates_awaiting_draft_payload(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """텔레그램 수정본 명령이 승인 대기 초안 payload를 교체해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")

    assert store.schedule_job(
        job_id="draft-update-job",
        title="기존 제목",
        seed_keywords=["국장", "수정본"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-05T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    payload = {
        "title": "기존 제목",
        "content": "기존 본문입니다.",
        "tags": ["국장"],
        "quality_snapshot": {
            "insight_quality": {
                "overall_score": 81,
                "needs_rewrite": True,
            }
        },
    }
    assert store.save_prepared_payload("draft-update-job", payload, mark_ready=False) is True
    assert store.update_job_status("draft-update-job", store.STATUS_AWAITING_APPROVAL) is True

    replied: list[str] = []

    async def _fake_send_reply(bot_token: str, chat_id: int | str, text: str) -> None:
        del bot_token, chat_id
        replied.append(text)

    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    revised_body = (
        "/draft_update draft-update-job\n"
        "제목: 수정된 제목\n"
        "## 오늘 같이 확인할 기준\n\n"
        "저도 국장 흐름을 완벽하게 맞히지는 못합니다. 그래서 오늘은 방향보다 기준을 먼저 보려 합니다. "
        "환율과 외국인 수급을 같이 확인하면서, 내가 줄여야 할 리스크가 무엇인지 정리해보겠습니다."
    )
    response = client.post(
        "/api/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 777001, "type": "private"},
                "text": revised_body,
            }
        },
    )
    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload["stored"] is False
    assert response_payload["reason"] == "draft_update_applied"

    loaded = store.load_prepared_payload("draft-update-job")
    assert loaded["title"] == "수정된 제목"
    assert "오늘 같이 확인할 기준" in loaded["content"]
    assert loaded["quality_snapshot"]["manual_revision_applied"] is True
    assert loaded["quality_snapshot"]["insight_quality"]["needs_rewrite"] is False
    assert any("수정본 반영 완료" in text for text in replied)


def test_telegram_webhook_updates_draft_from_txt_document(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """TXT 파일 업로드로 승인 대기 초안 payload를 교체해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")

    assert store.schedule_job(
        job_id="draft-txt-update-job",
        title="기존 TXT 제목",
        seed_keywords=["국장", "TXT"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-05T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    payload = {
        "title": "기존 TXT 제목",
        "content": "기존 본문입니다.",
        "tags": ["국장"],
        "quality_snapshot": {"insight_quality": {"overall_score": 82, "needs_rewrite": True}},
    }
    assert store.save_prepared_payload("draft-txt-update-job", payload, mark_ready=False) is True
    assert store.update_job_status("draft-txt-update-job", store.STATUS_AWAITING_APPROVAL) is True

    replied: list[str] = []

    async def _fake_send_reply(bot_token: str, chat_id: int | str, text: str) -> None:
        del bot_token, chat_id
        replied.append(text)

    async def _fake_download_document_text(*, bot_token: str, document: Dict[str, Any], max_bytes: int = 512_000) -> Dict[str, str]:
        del bot_token, document, max_bytes
        return {
            "ok": "1",
            "reason": "",
            "text": (
                "job_id: draft-txt-update-job\n"
                "title: TXT로 수정된 제목\n\n"
                "--- 본문 시작 ---\n"
                "## TXT로 다시 정리한 기준\n\n"
                "오늘 국장 흐름을 확인할 때는 예측보다 기준을 먼저 세웁니다. "
                "환율과 금리, 외국인 수급을 함께 보면서 내가 줄여야 할 위험을 차분히 확인합니다.\n"
                "--- 본문 끝 ---\n"
            ),
        }

    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)
    monkeypatch.setattr(telegram_router, "_download_telegram_document_text", _fake_download_document_text)

    response = client.post(
        "/api/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 777001, "type": "private"},
                "document": {
                    "file_id": "doc-file-1",
                    "file_name": "draft_txt_update.txt",
                    "mime_type": "text/plain",
                    "file_size": 400,
                },
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "draft_update_applied"

    loaded = store.load_prepared_payload("draft-txt-update-job")
    assert loaded["title"] == "TXT로 수정된 제목"
    assert "TXT로 다시 정리한 기준" in loaded["content"]
    assert loaded["quality_snapshot"]["manual_revision_applied"] is True
    assert any("TXT 수정본 반영 완료" in text for text in replied)


def test_telegram_webhook_revision_button_accepts_next_plain_text(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """수정본입력 버튼을 누르면 다음 평문이 해당 초안의 수정본으로 반영되어야 한다."""
    import server.routers.telegram_webhook as telegram_router
    from modules.automation.draft_approval import build_callback_data, create_draft_approval_request

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")
    store.set_system_setting("telegram_draft_revision_timeout_minutes", "30")

    assert store.schedule_job(
        job_id="draft-revision-button-job",
        title="수정 전 제목",
        seed_keywords=["국장", "수정본"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-05T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    payload = {
        "title": "수정 전 제목",
        "content": "기존 본문입니다.",
        "tags": ["국장"],
        "quality_snapshot": {
            "insight_quality": {
                "overall_score": 79,
                "needs_rewrite": True,
            }
        },
    }
    assert store.save_prepared_payload("draft-revision-button-job", payload, mark_ready=False) is True
    assert store.update_job_status("draft-revision-button-job", store.STATUS_AWAITING_APPROVAL) is True
    approval = create_draft_approval_request(
        store,
        job_id="draft-revision-button-job",
        title="수정 전 제목",
    )

    replied: list[str] = []
    reply_markups: list[Dict[str, Any]] = []

    async def _fake_send_reply(
        bot_token: str,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        del bot_token, chat_id
        replied.append(text)
        if reply_markup:
            reply_markups.append(reply_markup)

    async def _fake_answer_callback_query(
        bot_token: str,
        callback_query_id: str,
        text: str,
        *,
        show_alert: bool = False,
    ) -> None:
        del bot_token, callback_query_id, text, show_alert

    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)
    monkeypatch.setattr(telegram_router, "_answer_callback_query", _fake_answer_callback_query)

    response = client.post(
        "/api/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq_revision_001",
                "data": build_callback_data(
                    action="revise",
                    approval_id=approval.approval_id,
                    token=approval.token,
                ),
                "message": {"chat": {"id": 777001, "type": "private"}},
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["callback_action"] == "revise"
    assert any("수정본 입력 모드" in text for text in replied)

    revised_text = (
        "제목: 버튼으로 수정된 제목\n"
        "## 오늘 같이 다시 본 기준\n\n"
        "저도 시장을 정확히 맞힌다고 말할 수는 없습니다. 그래서 오늘은 예측보다 기준을 먼저 세워봅니다. "
        "환율과 금리, 외국인 수급을 함께 보면서 내가 줄여야 할 위험이 무엇인지 차분히 확인하겠습니다."
    )
    response = client.post(
        "/api/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 777001, "type": "private"},
                "text": revised_text,
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "draft_update_applied"

    loaded = store.load_prepared_payload("draft-revision-button-job")
    assert loaded["title"] == "버튼으로 수정된 제목"
    assert "오늘 같이 다시 본 기준" in loaded["content"]
    assert loaded["quality_snapshot"]["manual_revision_applied"] is True
    assert reply_markups
    assert "수정본입력" in str(reply_markups[-1])


def test_expired_revision_session_cancels_job_but_keeps_payload(tmp_path: Path):
    """수정본 입력 제한시간이 지나면 업로드는 취소하되 초안 백업은 남겨야 한다."""
    store = build_store(tmp_path, "draft_revision_timeout.db")
    assert store.schedule_job(
        job_id="draft-timeout-job",
        title="시간 초과 초안",
        seed_keywords=["국장"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-05T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    payload = {"title": "시간 초과 초안", "content": "백업으로 남아야 하는 초안 본문입니다."}
    assert store.save_prepared_payload("draft-timeout-job", payload, mark_ready=False) is True
    assert store.update_job_status("draft-timeout-job", store.STATUS_AWAITING_APPROVAL) is True

    result = start_draft_revision_session(
        store,
        chat_id="777001",
        job_id="draft-timeout-job",
        approval_id="approval-timeout",
        token="token-timeout",
        timeout_minutes=1,
    )
    assert result["ok"] is True
    settings = store.get_system_settings()
    raw_session = settings["telegram_draft_revision_session:777001"]
    session = json.loads(raw_session)
    expires_at = datetime.strptime(session["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    expired = expire_pending_draft_revision_sessions(
        store,
        now_override=expires_at + timedelta(seconds=1),
    )
    assert expired == 1

    job = store.get_job("draft-timeout-job")
    assert job is not None
    assert job.status == store.STATUS_CANCELLED
    assert store.load_prepared_payload("draft-timeout-job")["content"] == "백업으로 남아야 하는 초안 본문입니다."


def test_publication_archives_text_and_sends_draft_review_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """임시저장 성공 시 최종 텍스트를 보존하고 텔레그램 확인 링크를 보내야 한다."""
    store = build_store(tmp_path, "draft_saved_archive.db")
    monkeypatch.setenv("NAVER_PUBLISH_MODE", "draft")

    assert store.schedule_job(
        job_id="draft-saved-link-job",
        title="임시저장 확인 링크 테스트",
        seed_keywords=["국장", "임시저장"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-05T00:00:00Z",
        status=store.STATUS_RUNNING,
    )
    payload = {
        "title": "임시저장 확인 링크 테스트",
        "content": "수정본이 반영된 최종 본문입니다. 텍스트 아카이브에 반드시 남아야 합니다.",
        "tags": ["국장", "수정본"],
        "category": "economy",
        "images": ["data/images/content.png"],
        "image_sources": {"data/images/content.png": {"kind": "stock", "provider": "pexels"}},
        "quality_snapshot": {
            "score": 91,
            "manual_revision_applied": True,
            "insight_quality": {"overall_score": 88, "needs_rewrite": False},
        },
    }
    assert store.save_prepared_payload("draft-saved-link-job", payload, mark_ready=False) is True
    job = store.get_job("draft-saved-link-job")
    assert job is not None

    notifier = CaptureNotifier()
    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=_generate_content,
        notifier=notifier,  # type: ignore[arg-type]
    )

    assert asyncio.run(pipeline.process_publication(job)) is True
    archive = store.get_post_text_archive("draft-saved-link-job")
    assert archive is not None
    assert archive["final_content"] == payload["content"]
    assert archive["source_type"] == "published_draft"
    assert archive["manual_revision_applied"] == 1
    assert archive["insight_score"] == 88
    assert archive["review_status"] == "pending"

    assert notifier.messages
    assert any("네이버 임시저장 완료" in message for message in notifier.messages)
    assert any("https://blog.naver.com/test/approved" in message for message in notifier.messages)
    assert notifier.reply_markups
    assert "확인완료" in str(notifier.reply_markups[-1])
    assert "보류" in str(notifier.reply_markups[-1])


def test_draft_saved_review_callback_updates_archive_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """임시저장 확인/보류 버튼은 텍스트 아카이브의 review_status를 갱신해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    store = app.dependency_overrides[get_job_store]()
    store.set_system_setting("telegram_bot_token", "123456789:ABCdef_token")
    store.set_system_setting("telegram_chat_id", "777001")
    assert store.archive_post_text(
        job_id="draft-saved-review-job",
        title="확인 버튼 테스트",
        final_content="확인 버튼 테스트 본문",
        tags=["국장"],
        category="economy",
        source_type="published_draft",
        result_url="https://blog.naver.com/test/postwrite",
    )

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
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        del bot_token, chat_id, reply_markup
        replied.append(text)

    monkeypatch.setattr(telegram_router, "_answer_callback_query", _fake_answer_callback_query)
    monkeypatch.setattr(telegram_router, "_send_telegram_reply", _fake_send_reply)

    response = client.post(
        "/api/telegram/webhook",
        json={
            "callback_query": {
                "id": "cbq_draft_saved_001",
                "data": "ads:v1:h:draft-saved-review-job",
                "message": {"chat": {"id": 777001, "type": "private"}},
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["callback_handled"] is True
    assert body["callback_action"] == "held"
    assert any("보류" in text for text in answered)
    assert any("보류 기록" in text for text in replied)

    archive = store.get_post_text_archive("draft-saved-review-job")
    assert archive is not None
    assert archive["review_status"] == "held"
