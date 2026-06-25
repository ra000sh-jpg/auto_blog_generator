from __future__ import annotations

import asyncio
from typing import Any, Dict

from modules.automation.notifier import TelegramNotifier, resolve_manual_login_url


class CaptureNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__(bot_token="token", chat_id="chat")
        self.messages: list[str] = []

    async def send_message(
        self,
        text: str,
        *,
        disable_notification: bool = False,
        reply_markup: Dict[str, Any] | None = None,
    ) -> bool:
        del disable_notification, reply_markup
        self.messages.append(text)
        return True


class CaptureBackgroundNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__(bot_token="token", chat_id="chat")
        self.background_messages: list[dict[str, Any]] = []

    def send_message_background(
        self,
        text: str,
        *,
        disable_notification: bool = False,
        reply_markup: Dict[str, Any] | None = None,
    ) -> None:
        self.background_messages.append(
            {
                "text": text,
                "disable_notification": disable_notification,
                "reply_markup": reply_markup,
            }
        )


def test_manual_login_url_defaults_to_settings_focus(monkeypatch):
    monkeypatch.delenv("AUTOBLOG_MANUAL_LOGIN_URL", raising=False)
    monkeypatch.delenv("AUTOBLOG_WEB_BASE_URL", raising=False)

    assert resolve_manual_login_url() == "http://localhost:3000/settings?focus=naver"


def test_manual_login_url_uses_explicit_override(monkeypatch):
    monkeypatch.setenv("AUTOBLOG_MANUAL_LOGIN_URL", "https://example.test/naver-login")
    monkeypatch.setenv("AUTOBLOG_WEB_BASE_URL", "https://dashboard.example.test")

    assert resolve_manual_login_url() == "https://example.test/naver-login"


def test_auth_expired_critical_notification_includes_manual_login_button(monkeypatch):
    monkeypatch.delenv("AUTOBLOG_MANUAL_LOGIN_URL", raising=False)
    monkeypatch.setenv("AUTOBLOG_WEB_BASE_URL", "https://dashboard.example.test/")
    notifier = CaptureBackgroundNotifier()

    notifier.notify_critical_background(
        error_code="AUTH_EXPIRED",
        message="세션 만료. 수동 로그인 후 session state 갱신 필요.",
        job_id="job-1",
    )

    assert notifier.background_messages
    message = notifier.background_messages[0]
    assert "https://dashboard.example.test/settings?focus=naver" in message["text"]
    assert message["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "네이버 수동 로그인",
                    "url": "https://dashboard.example.test/settings?focus=naver",
                }
            ]
        ]
    }


def test_daily_summary_includes_idea_vault_alert_when_low_stock():
    notifier = CaptureNotifier()
    sent = asyncio.run(
        notifier.notify_daily_summary(
            local_date="2026-02-23",
            target=5,
            completed=3,
            failed=0,
            ready_count=1,
            queued_count=2,
            idea_pending_count=9,
            idea_daily_quota=2,
        )
    )
    assert sent is True
    assert notifier.messages
    assert notifier.messages[0].startswith("🚨")
    assert "idea_vault_pending: 9" in notifier.messages[0]


def test_daily_summary_without_idea_context_keeps_legacy_format():
    notifier = CaptureNotifier()
    sent = asyncio.run(
        notifier.notify_daily_summary(
            local_date="2026-02-23",
            target=3,
            completed=3,
            failed=0,
            ready_count=0,
            queued_count=0,
        )
    )
    assert sent is True
    assert notifier.messages
    assert "🚨" not in notifier.messages[0]


def test_send_document_uploads_txt_with_reply_markup(tmp_path, monkeypatch):
    """TXT 첨부는 sendDocument multipart 요청으로 전송되어야 한다."""

    captured = {}

    class FakeResponse:
        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

        async def post(self, url, *, data=None, files=None, json=None):
            del json
            captured["url"] = url
            captured["data"] = data
            captured["files"] = files
            return FakeResponse()

    monkeypatch.setattr("modules.automation.notifier.httpx.AsyncClient", FakeClient)
    draft_path = tmp_path / "draft.txt"
    draft_path.write_text("초안 본문", encoding="utf-8")

    notifier = TelegramNotifier(bot_token="token", chat_id="chat")
    sent = asyncio.run(
        notifier.send_document(
            file_path=str(draft_path),
            caption="초안 승인 요청",
            filename="draft_job.txt",
            reply_markup={"inline_keyboard": [[{"text": "승인", "callback_data": "x"}]]},
        )
    )

    assert sent is True
    assert captured["url"].endswith("/sendDocument")
    assert captured["data"]["caption"] == "초안 승인 요청"
    assert "inline_keyboard" in captured["data"]["reply_markup"]
    assert captured["files"]["document"][0] == "draft_job.txt"
