from __future__ import annotations

import asyncio

from modules.automation.notifier import TelegramNotifier


class CaptureNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__(bot_token="token", chat_id="chat")
        self.messages: list[str] = []

    async def send_message(
        self,
        text: str,
        *,
        disable_notification: bool = False,
    ) -> bool:
        del disable_notification
        self.messages.append(text)
        return True


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
