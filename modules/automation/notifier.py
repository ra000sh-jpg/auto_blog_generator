"""비동기 알림 모듈."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import httpx

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
    def from_env(cls, db_path: str = "data/automation.db") -> "TelegramNotifier":
        """환경변수/DB 기반 인스턴스를 생성한다."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

        if bot_token and chat_id:
            return cls(bot_token=bot_token, chat_id=chat_id)

        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT setting_key, setting_value
                    FROM system_settings
                    WHERE setting_key IN ('telegram_bot_token', 'telegram_chat_id')
                    """
                ).fetchall()
            finally:
                conn.close()
            mapped = {str(row[0]): str(row[1]) for row in rows}
            bot_token = bot_token or mapped.get("telegram_bot_token", "").strip()
            chat_id = chat_id or mapped.get("telegram_chat_id", "").strip()
        except Exception:
            pass

        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
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
        reply_markup: Dict[str, Any] | None = None,
    ) -> bool:
        """텔레그램 메시지를 비동기로 전송한다."""
        if not self.enabled:
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_notification": bool(disable_notification),
        }
        if isinstance(reply_markup, dict) and reply_markup:
            payload["reply_markup"] = reply_markup
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        timeout = self.connect_timeout_sec + self.read_timeout_sec
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
            try:
                response_payload: Dict[str, Any] = response.json()
            except json.JSONDecodeError:
                response_payload = {"ok": False, "status_code": response.status_code}
            ok = bool(response_payload.get("ok", False))
            if not ok:
                logger.warning("Telegram API returned not-ok: %s", response_payload)
            return ok
        except Exception as exc:
            logger.warning("Telegram notify failed: %s", exc)
            return False

    async def send_document(
        self,
        *,
        file_path: str,
        caption: str = "",
        filename: str = "",
        disable_notification: bool = False,
        reply_markup: Dict[str, Any] | None = None,
    ) -> bool:
        """텔레그램 문서 파일을 비동기로 전송한다."""
        if not self.enabled:
            return False

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            logger.warning("Telegram document missing: %s", file_path)
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "caption": str(caption or "")[:1024],
            "disable_notification": bool(disable_notification),
        }
        if isinstance(reply_markup, dict) and reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        upload_name = filename or path.name
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        timeout = self.connect_timeout_sec + self.read_timeout_sec
        try:
            with path.open("rb") as file_obj:
                files = {"document": (upload_name, file_obj, "text/plain")}
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, data=payload, files=files)
            try:
                response_payload: Dict[str, Any] = response.json()
            except json.JSONDecodeError:
                response_payload = {"ok": False, "status_code": response.status_code}
            ok = bool(response_payload.get("ok", False))
            if not ok:
                logger.warning("Telegram sendDocument returned not-ok: %s", response_payload)
            return ok
        except Exception as exc:
            logger.warning("Telegram document notify failed: %s", exc)
            return False

    def send_message_background(
        self,
        text: str,
        *,
        disable_notification: bool = False,
        reply_markup: Dict[str, Any] | None = None,
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
            self.send_message(
                text,
                disable_notification=disable_notification,
                reply_markup=reply_markup,
            ),
            name="telegram-send-message",
        )

    def send_document_background(
        self,
        *,
        file_path: str,
        caption: str = "",
        filename: str = "",
        disable_notification: bool = False,
        reply_markup: Dict[str, Any] | None = None,
    ) -> None:
        """문서 전송을 fire-and-forget으로 실행한다."""
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop for async document notifier")
            return
        loop.create_task(
            self.send_document(
                file_path=file_path,
                caption=caption,
                filename=filename,
                disable_notification=disable_notification,
                reply_markup=reply_markup,
            ),
            name="telegram-send-document",
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
        idea_pending_count: int = -1,
        idea_daily_quota: int = 0,
    ) -> bool:
        """일일 목표 요약 메시지를 전송한다."""
        status = "달성" if completed >= target else "미달"
        lines = [
            "AutoBlog 일일 요약 (22:30 KST)",
            f"- date: {local_date}",
            f"- target: {target}",
            f"- completed: {completed}",
            f"- failed: {failed}",
            f"- ready_to_publish: {ready_count}",
            f"- queued: {queued_count}",
            f"- result: {status}",
        ]

        if idea_pending_count >= 0:
            lines.append(f"- idea_vault_pending: {idea_pending_count}")
            threshold = max(0, int(idea_daily_quota)) * 5
            if threshold > 0 and idea_pending_count <= threshold:
                lines.insert(0, "🚨 원자재 확충 요망: 아이디어 창고 재고가 5일 치 이하입니다.")

        text = "\n".join(lines)
        return await self.send_message(text, disable_notification=False)
