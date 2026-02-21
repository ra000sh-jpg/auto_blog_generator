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
