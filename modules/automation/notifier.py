"""비동기 알림 모듈."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict

logger = logging.getLogger(__name__)


@dataclass
class TelegramNotifier:
    """텔레그램 알림 전송기."""

    bot_token: str = ""
    chat_id: str = ""
    connect_timeout_sec: float = 4.0
    read_timeout_sec: float = 8.0

    CRITICAL_ERROR_CODES = frozenset({"CAPTCHA_REQUIRED", "AUTH_EXPIRED"})

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        """환경변수 기반 인스턴스를 생성한다."""
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        )

    @property
    def enabled(self) -> bool:
        """알림 설정 유효 여부."""
        return bool(self.bot_token and self.chat_id)

    async def send_message(
        self,
        text: str,
        *,
        disable_notification: bool = False,
    ) -> bool:
        """텔레그램 메시지를 비동기로 전송한다."""
        if not self.enabled:
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_notification": "true" if disable_notification else "false",
        }
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        return await asyncio.to_thread(self._send_blocking, url, encoded)

    def send_message_background(
        self,
        text: str,
        *,
        disable_notification: bool = False,
    ) -> None:
        """메시지 전송을 fire-and-forget으로 실행한다."""
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop for async notifier")
            return
        loop.create_task(
            self.send_message(text, disable_notification=disable_notification),
            name="telegram-send-message",
        )

    def notify_critical_background(
        self,
        *,
        error_code: str,
        message: str,
        job_id: str = "",
    ) -> None:
        """치명 에러 알림을 비차단으로 전송한다."""
        if error_code not in self.CRITICAL_ERROR_CODES:
            return
        headline = "[CRITICAL] AutoBlog 치명 오류 감지"
        body = (
            f"{headline}\n"
            f"- code: {error_code}\n"
            f"- job_id: {job_id or '-'}\n"
            f"- detail: {message[:300]}"
        )
        self.send_message_background(body, disable_notification=False)

    async def notify_daily_summary(
        self,
        *,
        local_date: str,
        target: int,
        completed: int,
        failed: int,
        ready_count: int,
        queued_count: int,
    ) -> bool:
        """일일 목표 요약 메시지를 전송한다."""
        status = "달성" if completed >= target else "미달"
        text = (
            "AutoBlog 일일 요약 (22:30 KST)\n"
            f"- date: {local_date}\n"
            f"- target: {target}\n"
            f"- completed: {completed}\n"
            f"- failed: {failed}\n"
            f"- ready_to_publish: {ready_count}\n"
            f"- queued: {queued_count}\n"
            f"- result: {status}"
        )
        return await self.send_message(text, disable_notification=False)

    def _send_blocking(self, url: str, encoded_payload: bytes) -> bool:
        """블로킹 HTTP 요청을 실행한다."""
        request = urllib.request.Request(  # nosec B310
            url=url,
            data=encoded_payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        timeout = self.connect_timeout_sec + self.read_timeout_sec
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
                raw = response.read().decode("utf-8", errors="ignore")
            payload: Dict[str, Any] = json.loads(raw or "{}")
            ok = bool(payload.get("ok", False))
            if not ok:
                logger.warning("Telegram API returned not-ok: %s", payload)
            return ok
        except Exception as exc:
            logger.warning("Telegram notify failed: %s", exc)
            return False
