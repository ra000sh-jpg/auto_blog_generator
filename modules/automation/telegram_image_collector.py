"""Telegram 반자동 이미지 수집기."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .job_store import JobStore
    from .notifier import TelegramNotifier

logger = logging.getLogger(__name__)

_MODE_KEY = "telegram_image_mode"
_LAST_UPDATE_KEY = "telegram_last_update_id"
_SLOT_KEY_PREFIX = "img_slot_"


def is_semi_auto_mode(job_store: "JobStore") -> bool:
    """semi_auto 모드 활성화 여부를 반환한다."""
    return job_store.get_system_setting(_MODE_KEY, "auto") == "semi_auto"


class TelegramImageCollector:
    """잡별 이미지 슬롯을 관리하고 텔레그램 수신 이미지를 수집한다."""

    def __init__(
        self,
        job_store: "JobStore",
        notifier: "TelegramNotifier",
        image_output_dir: str = "data/images",
    ) -> None:
        self._job_store = job_store
        self._notifier = notifier
        self._output_dir = Path(image_output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def init_slots(self, job_id: str, image_slots: list[dict]) -> None:
        """image_slots(LLM 출력)로 슬롯을 초기화한다."""
        slots = [
            {
                "slot_id": slot.get("slot_id", f"slot_{index}"),
                "prompt": slot.get("render_prompt") or slot.get("prompt", ""),
                "slot_role": slot.get("slot_role", "content"),
                "status": "pending",
                "received_path": None,
                "sent_at": None,
            }
            for index, slot in enumerate(image_slots)
        ]
        self._job_store.set_system_setting(
            f"{_SLOT_KEY_PREFIX}{job_id}",
            json.dumps(slots, ensure_ascii=False),
        )
        logger.info("[ImageCollector] %s: %d slots initialized", job_id, len(slots))

    def get_slots(self, job_id: str) -> list[dict]:
        """잡의 슬롯 목록을 반환한다."""
        raw = self._job_store.get_system_setting(f"{_SLOT_KEY_PREFIX}{job_id}", "")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []

    def clear_slots(self, job_id: str) -> None:
        """잡 슬롯 저장값을 비운다."""
        self._job_store.set_system_setting(f"{_SLOT_KEY_PREFIX}{job_id}", "")

    def _save_slots(self, job_id: str, slots: list[dict]) -> None:
        self._job_store.set_system_setting(
            f"{_SLOT_KEY_PREFIX}{job_id}",
            json.dumps(slots, ensure_ascii=False),
        )

    def all_slots_received(self, job_id: str) -> bool:
        """모든 슬롯이 수집 완료인지 확인한다."""
        slots = self.get_slots(job_id)
        return bool(slots) and all(slot.get("status") == "received" for slot in slots)

    def get_received_paths(self, job_id: str) -> dict[str, str]:
        """수집 완료된 슬롯 경로를 반환한다."""
        return {
            str(slot.get("slot_id", "")): str(slot.get("received_path", ""))
            for slot in self.get_slots(job_id)
            if slot.get("status") == "received" and slot.get("received_path")
        }

    async def send_next_prompt(self, job_id: str, job_title: str) -> bool:
        """다음 pending 슬롯 프롬프트를 전송한다."""
        slots = self.get_slots(job_id)
        pending = [slot for slot in slots if slot.get("status") == "pending"]
        if not pending:
            return False

        slot = pending[0]
        current = next(
            index + 1
            for index, item in enumerate(slots)
            if item.get("slot_id") == slot.get("slot_id")
        )
        total = len(slots)
        role_label = "썸네일" if slot.get("slot_role") == "thumbnail" else "본문 이미지"

        message = (
            f"🖼 [{current}/{total}] {role_label}\n"
            f"📝 글 제목: {job_title}\n\n"
            f"아래 프롬프트로 Grok 또는 Gemini에서 이미지를 생성 후 "
            f"이 봇에 사진을 전송해 주세요.\n\n"
            f"```\n{slot.get('prompt', '')}\n```"
        )
        sent = await self._notifier.send_message(message)
        if sent:
            slot["status"] = "sent"
            slot["sent_at"] = datetime.now(timezone.utc).isoformat()
            self._save_slots(job_id, slots)
            logger.info(
                "[ImageCollector] %s: slot %s sent (%d/%d)",
                job_id,
                slot.get("slot_id"),
                current,
                total,
            )
        return sent

    async def poll_and_collect(self, job_id: str) -> bool:
        """getUpdates 폴링으로 수신 이미지를 sent 슬롯에 저장한다."""
        if not self._notifier.enabled:
            return False

        updates = await self._fetch_updates()
        if not updates:
            return False

        slots = self.get_slots(job_id)
        sent_slot = next((slot for slot in slots if slot.get("status") == "sent"), None)
        if not sent_slot:
            return False

        # 오배정 방지를 위해 등록된 chat_id와 일치하는 메시지만 사용한다.
        registered_chat_id = str(
            self._job_store.get_system_setting("telegram_chat_id", "") or self._notifier.chat_id
        ).strip()

        for update in updates:
            message = update.get("message", {})
            msg_chat_id = str(message.get("chat", {}).get("id", "")).strip()
            if registered_chat_id and msg_chat_id != registered_chat_id:
                continue

            photos = message.get("photo")
            if not photos:
                continue

            best_photo = max(photos, key=lambda item: item.get("file_size", 0))
            file_id = str(best_photo.get("file_id", "")).strip()
            if not file_id:
                continue

            save_path = await self._download_file(
                file_id=file_id,
                job_id=job_id,
                slot_id=str(sent_slot.get("slot_id", "")),
            )
            if not save_path:
                continue

            sent_slot["status"] = "received"
            sent_slot["received_path"] = str(save_path)
            self._save_slots(job_id, slots)
            logger.info(
                "[ImageCollector] %s: slot %s received -> %s",
                job_id,
                sent_slot.get("slot_id"),
                save_path,
            )
            return True

        return False

    async def _fetch_updates(self) -> list[dict]:
        """Telegram getUpdates를 호출한다."""
        bot_token = self._notifier.bot_token
        last_id_raw = self._job_store.get_system_setting(_LAST_UPDATE_KEY, "0")
        try:
            offset = int(last_id_raw) + 1
        except ValueError:
            offset = 0

        url = (
            f"https://api.telegram.org/bot{bot_token}/getUpdates"
            f"?offset={offset}&timeout=5&allowed_updates=%5B%22message%22%5D"
        )
        try:
            data = await asyncio.to_thread(self._get_json, url)
        except Exception as exc:
            logger.warning("[ImageCollector] getUpdates failed: %s", exc)
            return []

        if not data.get("ok"):
            return []

        updates: list[dict] = data.get("result", [])
        if updates:
            last_update_id = max(int(item.get("update_id", 0)) for item in updates)
            self._job_store.set_system_setting(_LAST_UPDATE_KEY, str(last_update_id))
        return updates

    async def _download_file(
        self,
        file_id: str,
        job_id: str,
        slot_id: str,
    ) -> Optional[Path]:
        """Telegram 파일을 내려받아 로컬 경로를 반환한다."""
        bot_token = self._notifier.bot_token
        url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        try:
            data = await asyncio.to_thread(self._get_json, url)
        except Exception as exc:
            logger.warning("[ImageCollector] getFile failed: %s", exc)
            return None

        if not data.get("ok"):
            return None

        file_path_remote = str(data.get("result", {}).get("file_path", "")).strip()
        if not file_path_remote:
            return None

        download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path_remote}"
        extension = Path(file_path_remote).suffix or ".jpg"
        save_path = self._output_dir / f"tg_{job_id}_{slot_id}{extension}"
        try:
            await asyncio.to_thread(self._download, download_url, save_path)
            return save_path
        except Exception as exc:
            logger.warning("[ImageCollector] download failed: %s", exc)
            return None

    @staticmethod
    def _get_json(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=10) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _download(url: str, save_path: Path) -> None:
        with urllib.request.urlopen(url, timeout=30) as response:  # nosec B310
            save_path.write_bytes(response.read())
