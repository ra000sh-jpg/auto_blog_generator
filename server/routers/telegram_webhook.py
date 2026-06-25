"""텔레그램 봇 Webhook 라우터 (Track B — 아이디어 금고 자동 수집 + 연동 인증).

지원 기능:
1. POST /api/telegram/verify-token — 토큰 2차 검증(getMe) + 1회용 인증코드 발급
2. POST /api/telegram/verify       — 인증코드 승인 확인 후 chat_id 자동 저장
3. POST /api/telegram/webhook      — 웹훅 수신(아이디어 금고 적재 + 인증코드 승인)
4. collect_pending_updates()       — FastAPI 시작 시 getUpdates 폴백 수집

보안:
- TELEGRAM_WEBHOOK_SECRET 환경변수 (또는 DB system_settings) 를
  X-Telegram-Bot-Api-Secret-Token 헤더와 비교해 검증한다.
- 헤더 누락 또는 불일치 시 403 반환.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from modules.automation.draft_approval import (
    DraftApprovalRequest,
    apply_draft_callback_action,
    apply_draft_manual_revision,
    apply_pending_draft_revision,
    build_inline_keyboard,
    expire_pending_draft_revision_sessions,
    get_revision_timeout_minutes,
    is_draft_callback_data,
    parse_draft_text_attachment,
    parse_draft_update_message,
    parse_draft_callback_data,
    start_draft_revision_session,
)
from modules.macro.telegram_approval import (
    apply_macro_candidate_callback,
    is_macro_callback_data,
    parse_macro_callback_data,
)
from server.dependencies import get_job_store

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# 내부 상수
# ---------------------------------------------------------------------------

_LAST_UPDATE_ID_KEY = "telegram_last_processed_update_id"
_GETUP_LIMIT = 100  # getUpdates 한 번에 가져올 최대 건수
_AUTH_CODE_PREFIX = "autoblog_"
_AUTH_CODE_TTL_SEC = 300
_BOT_TOKEN_PATTERN = re.compile(r"^[0-9]+:[a-zA-Z0-9_-]+$")
_FEEDBACK_CALLBACK_PREFIX = "afl:v1:"
_FEEDBACK_ACTION_MAP = {"a": "approve", "i": "ignore", "s": "snooze"}
_DRAFT_SAVED_CALLBACK_PREFIX = "ads:v1:"
_DRAFT_SAVED_ACTION_MAP = {"c": "confirmed", "h": "held"}


@dataclass
class PendingTelegramAuth:
    """텔레그램 연동 대기 상태."""

    bot_token: str
    bot_username: str
    expires_at: float
    verified_chat_id: str = ""


_PENDING_AUTH_CODES: Dict[str, PendingTelegramAuth] = {}
_PENDING_AUTH_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# 헬퍼: 봇 토큰 및 시크릿 조회
# ---------------------------------------------------------------------------


def _get_bot_token(job_store: Any) -> Optional[str]:
    """DB 또는 환경변수에서 Telegram Bot 토큰을 조회한다."""
    token = ""
    try:
        token = str(job_store.get_system_setting("telegram_bot_token", "")).strip()
    except Exception:
        pass
    if not token:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return token or None


def _get_webhook_secret(job_store: Any) -> Optional[str]:
    """DB 또는 환경변수에서 Webhook Secret 토큰을 조회한다."""
    secret = ""
    try:
        secret = str(job_store.get_system_setting("telegram_webhook_secret", "")).strip()
    except Exception:
        pass
    if not secret:
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    return secret or None


def _get_allowed_chat_id(job_store: Any) -> Optional[str]:
    """DB에 저장된 허용 Chat ID 를 조회한다 (빈 값 = 모두 허용)."""
    chat_id = ""
    try:
        chat_id = str(job_store.get_system_setting("telegram_chat_id", "")).strip()
    except Exception:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return chat_id or None


def _resolve_draft_destination_label(job_store: Any, job_id: str) -> str:
    """승인 후 이동할 목적지를 사용자에게 보여줄 문구로 반환한다."""
    publish_mode = ""
    try:
        payload = job_store.load_prepared_payload(job_id)
        if isinstance(payload, dict):
            publish_mode = str(payload.get("publish_mode", "") or "").strip().lower()
    except Exception:
        publish_mode = ""

    if not publish_mode:
        try:
            job = job_store.get_job(job_id)
        except Exception:
            job = None
        tags = getattr(job, "tags", []) if job is not None else []
        if isinstance(tags, list):
            for tag in tags:
                tag_text = str(tag or "").strip().lower()
                if tag_text.startswith("publish_mode:"):
                    publish_mode = tag_text.split(":", 1)[1].strip()
                    break

    if not publish_mode:
        publish_mode = os.getenv("NAVER_PUBLISH_MODE", "publish").strip().lower()
    return "네이버 임시저장" if publish_mode == "draft" else "네이버 발행"


def _normalize_auth_code(raw_value: str) -> str:
    """인증코드를 표준화한다."""
    return str(raw_value or "").strip().lower()


def _extract_auth_code_from_text(text: str) -> str:
    """`/start autoblog_xxxxxx` 형태에서 인증코드를 추출한다."""
    normalized = str(text or "").strip()
    if not normalized.startswith("/start"):
        return ""
    tokens = normalized.split(maxsplit=1)
    if len(tokens) < 2:
        return ""
    payload = tokens[1].strip()
    if not payload.startswith(_AUTH_CODE_PREFIX):
        return ""
    return _normalize_auth_code(payload.replace(_AUTH_CODE_PREFIX, "", 1))


async def _cleanup_expired_auth_codes() -> None:
    """만료된 인증코드를 정리한다."""
    now = time.time()
    expired_codes = [code for code, state in _PENDING_AUTH_CODES.items() if state.expires_at <= now]
    for code in expired_codes:
        _PENDING_AUTH_CODES.pop(code, None)


async def _register_pending_auth_code(
    auth_code: str,
    *,
    bot_token: str,
    bot_username: str,
) -> None:
    """인증코드 상태를 TTL과 함께 저장한다."""
    async with _PENDING_AUTH_LOCK:
        await _cleanup_expired_auth_codes()
        _PENDING_AUTH_CODES[_normalize_auth_code(auth_code)] = PendingTelegramAuth(
            bot_token=bot_token,
            bot_username=bot_username,
            expires_at=time.time() + _AUTH_CODE_TTL_SEC,
            verified_chat_id="",
        )


async def _mark_auth_code_verified(auth_code: str, chat_id: str) -> bool:
    """웹훅에서 인증코드 일치 시 승인 상태로 전환한다."""
    async with _PENDING_AUTH_LOCK:
        await _cleanup_expired_auth_codes()
        state = _PENDING_AUTH_CODES.get(_normalize_auth_code(auth_code))
        if not state:
            return False
        state.verified_chat_id = str(chat_id)
        return True


async def _get_pending_auth_state(auth_code: str) -> Optional[PendingTelegramAuth]:
    """현재 인증코드 상태를 조회한다."""
    async with _PENDING_AUTH_LOCK:
        await _cleanup_expired_auth_codes()
        return _PENDING_AUTH_CODES.get(_normalize_auth_code(auth_code))


async def _pop_pending_auth_state(auth_code: str) -> Optional[PendingTelegramAuth]:
    """인증 완료된 코드를 소비(삭제)한다."""
    async with _PENDING_AUTH_LOCK:
        return _PENDING_AUTH_CODES.pop(_normalize_auth_code(auth_code), None)


async def _try_collect_auth_code_from_updates(bot_token: str, auth_code: str) -> str:
    """웹훅 유실 대비 getUpdates 폴백으로 인증코드 메시지를 탐색한다."""
    target = _normalize_auth_code(auth_code)
    if not bot_token or not target:
        return ""

    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, params={"limit": 50, "timeout": 0})
            payload = response.json()
    except Exception as exc:
        logger.warning("Telegram verify fallback(getUpdates) failed: %s", exc)
        return ""

    if not payload.get("ok"):
        return ""

    updates: List[Dict[str, Any]] = payload.get("result", [])
    for update in reversed(updates):
        message: Dict[str, Any] = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat", {})
        chat_type = str(chat.get("type", "")).lower()
        if chat_type != "private":
            continue
        code = _extract_auth_code_from_text(str(message.get("text", "")))
        if code and code == target:
            return str(chat.get("id", "")).strip()
    return ""


# ---------------------------------------------------------------------------
# 메시지 처리 핵심 로직
# ---------------------------------------------------------------------------


async def _parse_and_store_text(
    text: str,
    job_store: Any,
) -> Optional[str]:
    """텔레그램 메시지 텍스트를 아이디어 금고에 적재하고 키워드 요약을 반환한다.

    Returns:
        저장된 아이템의 mapped_category (실패 시 None).
    """
    if not text or not text.strip():
        return None

    # 허용 카테고리 조회 (job_store 에서 직접)
    import json as json_mod
    from modules.constants import DEFAULT_FALLBACK_CATEGORY

    categories: List[str] = []
    try:
        raw = job_store.get_system_setting("custom_categories", "[]")
        decoded = json_mod.loads(raw)
        categories = [str(c).strip() for c in decoded if str(c).strip()]
    except Exception:
        pass
    if not categories:
        fallback = job_store.get_system_setting("fallback_category", "").strip()
        categories = [fallback if fallback else DEFAULT_FALLBACK_CATEGORY]

    # LLM 파싱 시도
    try:
        from modules.llm.idea_vault_parser import IdeaVaultBatchParser
        from modules.config import LLMConfig

        parser = IdeaVaultBatchParser(llm_config=LLMConfig())
        result = await parser.parse_bulk(
            text.strip(),
            categories=categories,
            batch_size=5,
        )
        items_to_save = [
            {
                "raw_text": item.raw_text,
                "mapped_category": item.mapped_category,
                "topic_mode": item.topic_mode,
                "parser_used": result.parser_used,
                "source_url": "",
            }
            for item in result.accepted_items
        ]
        if items_to_save:
            job_store.add_idea_vault_items(items_to_save)
            return items_to_save[0]["mapped_category"]
    except Exception as exc:
        logger.warning("Telegram webhook: LLM parse failed, using heuristic fallback: %s", exc)

    # Heuristic fallback
    job_store.add_idea_vault_items([
        {
            "raw_text": text.strip()[:500],
            "mapped_category": categories[0],
            "topic_mode": "cafe",
            "parser_used": "heuristic_fallback",
            "source_url": "",
        }
    ])
    return categories[0]


async def _send_telegram_reply(
    bot_token: str,
    chat_id: int | str,
    text: str,
    *,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> None:
    """Telegram sendMessage API 로 답장을 보낸다 (비차단, 오류 무시)."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if isinstance(reply_markup, dict) and reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.debug("Telegram reply failed (ignored): %s", exc)


async def _download_telegram_document_text(
    *,
    bot_token: str,
    document: Dict[str, Any],
    max_bytes: int = 512_000,
) -> Dict[str, str]:
    """텔레그램 TXT 문서를 내려받아 문자열로 반환한다."""

    file_name = str(document.get("file_name", "") or "").strip()
    mime_type = str(document.get("mime_type", "") or "").strip().lower()
    if file_name and not file_name.lower().endswith(".txt"):
        return {"ok": "", "reason": "unsupported_document_type", "text": ""}
    if mime_type and mime_type not in {"text/plain", "application/octet-stream"}:
        return {"ok": "", "reason": "unsupported_document_type", "text": ""}

    try:
        file_size = int(document.get("file_size", 0) or 0)
    except (TypeError, ValueError):
        file_size = 0
    if file_size > max_bytes:
        return {"ok": "", "reason": "document_too_large", "text": ""}

    file_id = str(document.get("file_id", "") or "").strip()
    if not file_id:
        return {"ok": "", "reason": "file_id_missing", "text": ""}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            file_response = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={"file_id": file_id},
            )
            file_payload = file_response.json()
            if not bool(file_payload.get("ok", False)):
                return {"ok": "", "reason": "get_file_failed", "text": ""}
            file_path = str((file_payload.get("result") or {}).get("file_path", "") or "").strip()
            if not file_path:
                return {"ok": "", "reason": "file_path_missing", "text": ""}
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            content_response = await client.get(download_url)
            content_response.raise_for_status()
            raw_bytes = content_response.content
    except Exception as exc:
        logger.debug("Telegram document download failed: %s", exc)
        return {"ok": "", "reason": "download_failed", "text": ""}

    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return {"ok": "1", "reason": "", "text": raw_bytes.decode(encoding)}
        except UnicodeDecodeError:
            continue
    return {"ok": "", "reason": "decode_failed", "text": ""}


async def _answer_callback_query(
    bot_token: str,
    callback_query_id: str,
    text: str,
    *,
    show_alert: bool = False,
) -> None:
    """inline button 콜백에 응답해 로딩 상태를 해제한다."""
    if not bot_token or not callback_query_id:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                url,
                json={
                    "callback_query_id": callback_query_id,
                    "text": text[:180],
                    "show_alert": bool(show_alert),
                },
            )
    except Exception as exc:
        logger.debug("Telegram callback answer failed (ignored): %s", exc)


async def _edit_message_reply_markup(
    bot_token: str,
    *,
    chat_id: int | str | None,
    message_id: int | str | None,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> None:
    """처리 완료된 inline button을 제거하거나 교체한다."""

    if not bot_token or chat_id is None or message_id is None:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.debug("Telegram message reply_markup edit failed (ignored): %s", exc)


async def _append_callback_status_to_message(
    bot_token: str,
    *,
    message: Dict[str, Any],
    status_text: str,
) -> None:
    """기존 승인 메시지 하단에 처리 결과를 남긴다."""

    if not bot_token or not message:
        return
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return

    base_text = str(message.get("text") or message.get("caption") or "").strip()
    if not base_text:
        await _edit_message_reply_markup(
            bot_token,
            chat_id=chat_id,
            message_id=message_id,
        )
        return

    edited_text = f"{base_text}\n\n처리 결과: {status_text}"
    if len(edited_text) > 3900:
        edited_text = f"{base_text[:3600].rstrip()}\n\n...(기존 메시지 일부 생략)\n\n처리 결과: {status_text}"

    method = "editMessageCaption" if message.get("caption") else "editMessageText"
    payload_key = "caption" if method == "editMessageCaption" else "text"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        payload_key: edited_text,
        "reply_markup": {"inline_keyboard": []},
    }
    try:
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.debug("Telegram message status edit failed; falling back to markup removal: %s", exc)
        await _edit_message_reply_markup(
            bot_token,
            chat_id=chat_id,
            message_id=message_id,
        )


def _parse_feedback_callback_data(raw_data: str) -> Optional[Dict[str, str]]:
    """afl 버튼 callback_data를 파싱한다."""
    normalized = str(raw_data or "").strip()
    if not normalized.startswith(_FEEDBACK_CALLBACK_PREFIX):
        return None
    parts = normalized.split(":")
    if len(parts) != 5:
        return None
    _, version, action_code, candidate_id, callback_token = parts
    if version != "v1":
        return None
    action = _FEEDBACK_ACTION_MAP.get(action_code)
    if not action:
        return None
    if not candidate_id or not callback_token:
        return None
    return {
        "action": action,
        "candidate_id": candidate_id,
        "callback_token": callback_token,
    }


def _parse_draft_saved_callback_data(raw_data: str) -> Optional[Dict[str, str]]:
    """임시저장 확인 버튼 callback_data를 파싱한다."""
    normalized = str(raw_data or "").strip()
    if not normalized.startswith(_DRAFT_SAVED_CALLBACK_PREFIX):
        return None
    parts = normalized.split(":", 3)
    if len(parts) != 4:
        return None
    _, version, action_code, job_id = parts
    if version != "v1":
        return None
    action = _DRAFT_SAVED_ACTION_MAP.get(action_code)
    if not action or not job_id:
        return None
    return {
        "action": action,
        "job_id": job_id,
    }


async def _handle_draft_saved_callback_query(
    callback_query: Dict[str, Any],
    job_store: Any,
) -> Dict[str, Any]:
    """임시저장 확인/보류 버튼을 처리한다."""
    callback_query_id = str(callback_query.get("id", "")).strip()
    callback_data = str(callback_query.get("data", "")).strip()
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    parsed = _parse_draft_saved_callback_data(callback_data)
    if not parsed:
        return {
            "handled": False,
            "reason": "not_draft_saved_callback",
        }

    allowed_chat_id = _get_allowed_chat_id(job_store)
    if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
        return {
            "handled": False,
            "reason": "chat_id_not_allowed",
        }

    update_fn = getattr(job_store, "update_post_archive_review_status", None)
    ok = bool(callable(update_fn) and update_fn(parsed["job_id"], parsed["action"]))
    bot_token = _get_bot_token(job_store) or ""
    if ok:
        if parsed["action"] == "confirmed":
            callback_text = "확인완료로 기록했습니다."
            followup_text = f"확인완료 기록: {parsed['job_id']}"
        else:
            callback_text = "보류로 기록했습니다."
            followup_text = f"보류 기록: {parsed['job_id']}"
        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text=callback_text,
                show_alert=True,
            )
        if bot_token:
            await _append_callback_status_to_message(
                bot_token,
                message=message,
                status_text=callback_text,
            )
        if bot_token and chat_id is not None:
            await _send_telegram_reply(bot_token, chat_id, followup_text)
        return {
            "handled": True,
            "reason": "draft_saved_callback_applied",
            "action": parsed["action"],
        }

    if callback_query_id and bot_token:
        await _answer_callback_query(
            bot_token=bot_token,
            callback_query_id=callback_query_id,
            text="기록할 아카이브를 찾지 못했습니다.",
            show_alert=True,
        )
    return {
        "handled": False,
        "reason": "archive_not_found",
    }


async def _handle_draft_approval_callback_query(
    callback_query: Dict[str, Any],
    job_store: Any,
) -> Dict[str, Any]:
    """초안 승인 inline button callback을 처리한다."""
    callback_query_id = str(callback_query.get("id", "")).strip()
    callback_data = str(callback_query.get("data", "")).strip()
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if not is_draft_callback_data(callback_data):
        return {
            "handled": False,
            "reason": "not_draft_callback",
        }

    allowed_chat_id = _get_allowed_chat_id(job_store)
    if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
        return {
            "handled": False,
            "reason": "chat_id_not_allowed",
        }

    parsed = parse_draft_callback_data(callback_data)
    bot_token = _get_bot_token(job_store) or ""
    if not parsed:
        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text="알 수 없는 초안 승인 요청입니다.",
                show_alert=True,
            )
        return {
            "handled": False,
            "reason": "invalid_callback_data",
        }

    result = apply_draft_callback_action(
        job_store,
        approval_id=parsed["approval_id"],
        token=parsed["token"],
        action=parsed["action"],
    )
    ok = bool(isinstance(result, dict) and result.get("ok"))
    reason = str(result.get("reason", "unknown")) if isinstance(result, dict) else "invalid_result"

    if ok:
        action = str(result.get("action", ""))
        job_id = str(result.get("job_id", "")).strip()
        status_text = ""
        if action == "approve":
            destination = _resolve_draft_destination_label(job_store, job_id)
            callback_text = f"승인 완료: {destination} 대기 중입니다."
            status_text = f"승인 완료 - 1/3 승인 완료, 2/3 {destination} 대기 중"
            followup_text = (
                "승인 완료\n"
                "진행상황: 1/3 텔레그램 승인 완료\n"
                f"다음 단계: 2/3 {destination} 처리 대기\n"
                "완료 알림: 3/3 확인 링크 전송 예정"
            )
        elif action == "revise":
            revision_result = start_draft_revision_session(
                job_store,
                chat_id=chat_id,
                job_id=job_id,
                approval_id=parsed["approval_id"],
                token=parsed["token"],
                timeout_minutes=get_revision_timeout_minutes(job_store),
            )
            if revision_result.get("ok"):
                callback_text = "수정본 입력 대기 상태로 전환했습니다."
                status_text = "수정본 입력 대기 - 다음 메시지로 완성본 전체를 보내주세요."
                followup_text = (
                    "수정본 입력 모드로 전환했습니다.\n"
                    "이 메시지 다음에 수정한 완성본 전체를 그대로 붙여넣어 주세요.\n"
                    f"{revision_result.get('timeout_minutes')}분 안에 입력하지 않으면 이 초안의 오늘 업로드는 취소됩니다.\n\n"
                    "제목까지 바꾸려면 첫 줄에 `제목: 새 제목`을 넣어주세요."
                )
            else:
                callback_text = "수정본 입력 상태 전환에 실패했습니다."
                status_text = "수정본 입력 준비 실패"
                followup_text = f"수정본 입력 준비 실패: {revision_result.get('reason', 'unknown')}"
        else:
            callback_text = "취소 완료: 초안을 중단했습니다."
            status_text = "취소 완료 - 이 초안은 업로드하지 않습니다."
            followup_text = "취소 완료: 이 초안은 발행 대기열에 올리지 않았습니다."
        if job_id:
            followup_text += f"\njob_id: {job_id}"

        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text=callback_text,
                show_alert=False,
            )
        if bot_token and status_text:
            await _append_callback_status_to_message(
                bot_token,
                message=message,
                status_text=status_text,
            )
        if bot_token and chat_id is not None:
            await _send_telegram_reply(bot_token, chat_id, followup_text)

        return {
            "handled": True,
            "reason": "draft_callback_applied",
            "action": action,
        }

    fail_message = "이미 처리되었거나 만료된 초안 요청입니다."
    if reason == "invalid_token":
        fail_message = "버튼 토큰이 유효하지 않습니다."
    elif reason == "token_expired":
        fail_message = "초안 승인 유효기간이 만료되었습니다."
    elif reason == "already_handled":
        fail_message = "이미 처리된 초안 요청입니다."
    elif reason == "job_not_found":
        fail_message = "초안 작업을 찾지 못했습니다."
    elif reason == "missing_payload":
        fail_message = "저장된 초안 본문을 찾지 못했습니다."
    elif reason == "invalid_job_status":
        fail_message = "현재 처리할 수 없는 초안 상태입니다."

    if callback_query_id and bot_token:
        await _answer_callback_query(
            bot_token=bot_token,
            callback_query_id=callback_query_id,
            text=fail_message,
            show_alert=True,
        )
    return {
        "handled": False,
        "reason": reason,
    }


async def _handle_pending_draft_revision_message(
    *,
    text: str,
    chat_id: int | str | None,
    job_store: Any,
) -> Dict[str, Any]:
    """수정본 입력 대기 상태의 다음 텍스트를 초안에 반영한다."""
    if chat_id is None:
        return {
            "handled": False,
            "reason": "chat_id_missing",
        }

    result = apply_pending_draft_revision(
        job_store,
        chat_id=chat_id,
        revised_text=text,
    )
    if not bool(result.get("handled", False)):
        return {
            "handled": False,
            "reason": result.get("reason", "revision_session_not_found"),
        }

    bot_token = _get_bot_token(job_store) or ""
    ok = bool(result.get("ok", False))
    reason = str(result.get("reason", "unknown"))

    if ok:
        job_id = str(result.get("job_id", "")).strip()
        request = DraftApprovalRequest(
            approval_id=str(result.get("approval_id", "")),
            token=str(result.get("token", "")),
            job_id=job_id,
            expires_at=str(result.get("expires_at", "")),
        )
        reply = (
            "수정본 반영 완료.\n"
            f"job_id: {job_id}\n"
            f"본문 길이: {result.get('content_length')}자\n"
            "승인 버튼을 누르면 발행 대기열로 이동합니다."
        )
        if bot_token:
            await _send_telegram_reply(
                bot_token,
                chat_id,
                reply,
                reply_markup=build_inline_keyboard(request),
            )
    elif reason == "revision_timeout":
        if bot_token:
            await _send_telegram_reply(
                bot_token,
                chat_id,
                "수정본 입력 제한시간이 지나 이 초안의 오늘 업로드를 취소했습니다. 초안 내용은 백업으로 남겨두었습니다.",
            )
    elif reason == "content_too_short":
        if bot_token:
            await _send_telegram_reply(
                bot_token,
                chat_id,
                f"수정본이 너무 짧습니다. 최소 {result.get('min_content_chars', 80)}자 이상으로 다시 보내주세요.",
            )
    else:
        if bot_token:
            await _send_telegram_reply(bot_token, chat_id, f"수정본 반영 실패: {reason}")

    return {
        "handled": True,
        "reason": reason,
        "ok": ok,
    }


async def _handle_draft_document_message(
    *,
    message: Dict[str, Any],
    chat_id: int | str | None,
    job_store: Any,
) -> Dict[str, Any]:
    """텔레그램 TXT 문서 업로드를 초안 수정본으로 반영한다."""

    document = message.get("document") or {}
    if not isinstance(document, dict) or not document:
        return {
            "handled": False,
            "reason": "not_document",
        }

    bot_token = _get_bot_token(job_store) or ""
    if not bot_token:
        return {
            "handled": True,
            "reason": "bot_token_missing",
            "ok": False,
        }

    downloaded = await _download_telegram_document_text(
        bot_token=bot_token,
        document=document,
    )
    if not bool(downloaded.get("ok")):
        reason = str(downloaded.get("reason", "document_download_failed"))
        if chat_id is not None:
            await _send_telegram_reply(bot_token, chat_id, f"TXT 수정본을 읽지 못했습니다. reason={reason}")
        return {
            "handled": True,
            "reason": reason,
            "ok": False,
        }

    parsed = parse_draft_text_attachment(str(downloaded.get("text", "")))
    if parsed.get("error"):
        if chat_id is not None:
            await _send_telegram_reply(bot_token, chat_id, "TXT 수정본 형식이 비어 있거나 본문을 찾지 못했습니다.")
        return {
            "handled": True,
            "reason": parsed["error"],
            "ok": False,
        }

    job_id = str(parsed.get("job_id", "") or "").strip()
    revised_text = str(parsed.get("content", "") or "").strip()
    if job_id:
        result = apply_draft_manual_revision(
            job_store,
            job_id=job_id,
            revised_text=revised_text,
        )
        ok = bool(isinstance(result, dict) and result.get("ok"))
        reason = str(result.get("reason", "unknown")) if isinstance(result, dict) else "invalid_result"
    elif chat_id is not None:
        result = apply_pending_draft_revision(
            job_store,
            chat_id=chat_id,
            revised_text=revised_text,
        )
        ok = bool(isinstance(result, dict) and result.get("ok"))
        reason = str(result.get("reason", "unknown")) if isinstance(result, dict) else "invalid_result"
    else:
        result = {"ok": False, "reason": "job_id_missing"}
        ok = False
        reason = "job_id_missing"

    if ok:
        reply_job_id = str(result.get("job_id", job_id)).strip()
        reply = (
            "TXT 수정본 반영 완료.\n"
            f"job_id: {reply_job_id}\n"
            f"본문 길이: {result.get('content_length')}자\n"
            "승인 버튼을 누르면 발행 대기열로 이동합니다."
        )
    elif reason == "content_too_short":
        reply = f"TXT 수정본이 너무 짧습니다. 최소 {result.get('min_content_chars', 80)}자 이상으로 보내주세요."
    elif reason == "invalid_job_status":
        reply = f"현재 수정할 수 없는 초안 상태입니다. current_status={result.get('current_status', '-')}"
    elif reason in {"revision_session_not_found", "job_id_missing"}:
        reply = "수정할 초안을 찾지 못했습니다. TXT 안의 job_id를 확인하거나 수정본입력 버튼을 먼저 눌러주세요."
    elif reason == "job_not_found":
        reply = "job_id에 해당하는 초안을 찾지 못했습니다."
    elif reason == "missing_payload":
        reply = "저장된 초안 본문을 찾지 못했습니다."
    else:
        reply = f"TXT 수정본 반영 실패: {reason}"

    if chat_id is not None:
        await _send_telegram_reply(bot_token, chat_id, reply)

    return {
        "handled": True,
        "reason": reason,
        "ok": ok,
    }


async def _handle_feedback_callback_query(
    callback_query: Dict[str, Any],
    job_store: Any,
) -> Dict[str, Any]:
    """피드백 루프 inline button callback을 처리한다."""
    callback_query_id = str(callback_query.get("id", "")).strip()
    callback_data = str(callback_query.get("data", "")).strip()
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    allowed_chat_id = _get_allowed_chat_id(job_store)
    if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
        return {
            "handled": False,
            "reason": "chat_id_not_allowed",
        }

    parsed = _parse_feedback_callback_data(callback_data)
    if not parsed:
        bot_token = _get_bot_token(job_store) or ""
        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text="알 수 없는 버튼 요청입니다.",
                show_alert=True,
            )
        return {
            "handled": False,
            "reason": "invalid_callback_data",
        }

    apply_action = getattr(job_store, "apply_feedback_candidate_action", None)
    if not callable(apply_action):
        bot_token = _get_bot_token(job_store) or ""
        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text="서버가 아직 준비되지 않았습니다.",
                show_alert=True,
            )
        return {
            "handled": False,
            "reason": "feedback_store_unavailable",
        }

    result = apply_action(
        candidate_id=parsed["candidate_id"],
        action=parsed["action"],
        callback_token=parsed["callback_token"],
    )
    ok = bool(isinstance(result, dict) and result.get("ok"))
    reason = str(result.get("reason", "unknown")) if isinstance(result, dict) else "invalid_result"
    bot_token = _get_bot_token(job_store) or ""

    if ok:
        action = str(result.get("action", ""))
        callback_text = "처리되었습니다."
        followup_text = "✅ 요청이 반영되었습니다."
        if action == "approve":
            callback_text = "자동 반영으로 적용했습니다."
            followup_text = "✅ 승인 완료: 다음 포스트부터 자동 반영됩니다."
        elif action == "ignore":
            callback_text = "무시 처리했습니다."
            followup_text = "❌ 무시 완료: 이 제안은 자동 반영 후보에서 제외됩니다."
        elif action == "snooze":
            callback_text = "나중에 다시 물어볼게요."
            followup_text = "⏸ 나중에 처리로 저장했습니다. 추후 다시 알림을 보냅니다."

        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text=callback_text,
                show_alert=False,
            )
        if bot_token and chat_id is not None:
            await _send_telegram_reply(bot_token, chat_id, followup_text)

        return {
            "handled": True,
            "reason": "feedback_callback_applied",
            "action": action,
        }

    fail_message = "이미 처리되었거나 만료된 요청입니다."
    if reason == "invalid_token":
        fail_message = "버튼 토큰이 유효하지 않습니다."
    elif reason == "token_expired":
        fail_message = "요청 유효기간이 만료되었습니다."
    elif reason == "already_handled":
        fail_message = "이미 처리된 요청입니다."
    elif reason == "candidate_not_found":
        fail_message = "대상을 찾지 못했습니다."
    if callback_query_id and bot_token:
        await _answer_callback_query(
            bot_token=bot_token,
            callback_query_id=callback_query_id,
            text=fail_message,
            show_alert=True,
        )
    return {
        "handled": False,
        "reason": reason,
    }


async def _handle_macro_candidate_callback_query(
    callback_query: Dict[str, Any],
    job_store: Any,
) -> Dict[str, Any]:
    """매크로 글 후보 선택 inline button callback을 처리한다."""
    callback_query_id = str(callback_query.get("id", "")).strip()
    callback_data = str(callback_query.get("data", "")).strip()
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if not is_macro_callback_data(callback_data):
        return {
            "handled": False,
            "reason": "not_macro_callback",
        }

    allowed_chat_id = _get_allowed_chat_id(job_store)
    if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
        return {
            "handled": False,
            "reason": "chat_id_not_allowed",
        }

    parsed = parse_macro_callback_data(callback_data)
    bot_token = _get_bot_token(job_store) or ""
    if not parsed:
        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text="알 수 없는 매크로 후보 요청입니다.",
                show_alert=True,
            )
        return {
            "handled": False,
            "reason": "invalid_callback_data",
        }

    result = apply_macro_candidate_callback(
        job_store,
        candidate_id=parsed["candidate_id"],
        action=parsed["action"],
    )
    ok = bool(isinstance(result, dict) and result.get("ok"))
    reason = str(result.get("reason", "unknown")) if isinstance(result, dict) else "invalid_result"

    if ok:
        job_id = str(result.get("job_id", "") or "").strip()
        title = str(result.get("title", "") or "").strip()
        callback_text = "초안 생성 큐에 등록했습니다."
        followup_lines = [
            "매크로 후보를 초안 생성 큐에 올렸습니다.",
            f"제목: {title or '-'}",
            f"job_id: {job_id or '-'}",
            "",
            "이후 텍스트 초안이 도착하면 기존처럼 승인 또는 수정본입력으로 처리하면 됩니다.",
        ]
        if callback_query_id and bot_token:
            await _answer_callback_query(
                bot_token=bot_token,
                callback_query_id=callback_query_id,
                text=callback_text,
                show_alert=False,
            )
        if bot_token and chat_id is not None:
            await _send_telegram_reply(bot_token, chat_id, "\n".join(followup_lines))
        return {
            "handled": True,
            "reason": "macro_candidate_promoted",
            "action": parsed["action"],
            "job_id": job_id,
        }

    fail_message = "매크로 후보를 처리하지 못했습니다."
    if reason == "already_handled":
        fail_message = "이미 초안 생성 큐에 올린 후보입니다."
    elif reason == "candidate_not_found":
        fail_message = "매크로 후보를 찾지 못했습니다."
    elif reason == "invalid_candidate_status":
        fail_message = f"현재 처리할 수 없는 후보 상태입니다. status={result.get('current_status', '-')}"
    elif reason == "invalid_candidate":
        fail_message = "후보 데이터가 불완전해 초안 생성 큐에 올리지 못했습니다."
    elif reason == "promotion_failed":
        fail_message = "초안 생성 큐 등록 중 오류가 발생했습니다."

    if callback_query_id and bot_token:
        await _answer_callback_query(
            bot_token=bot_token,
            callback_query_id=callback_query_id,
            text=fail_message,
            show_alert=True,
        )
    return {
        "handled": False,
        "reason": reason,
        "action": parsed["action"],
    }


async def _handle_draft_update_message(
    *,
    text: str,
    chat_id: int | str | None,
    job_store: Any,
) -> Dict[str, Any]:
    """텔레그램 텍스트 명령으로 승인 대기 초안을 수정한다."""
    parsed = parse_draft_update_message(text)
    if not parsed:
        return {
            "handled": False,
            "reason": "not_draft_update",
        }

    bot_token = _get_bot_token(job_store) or ""
    if parsed.get("error"):
        if bot_token and chat_id is not None:
            await _send_telegram_reply(
                bot_token,
                chat_id,
                "수정본 형식이 맞지 않습니다.\n\n/draft_update job_id\n수정한 완성본 본문",
            )
        return {
            "handled": True,
            "reason": parsed["error"],
        }

    result = apply_draft_manual_revision(
        job_store,
        job_id=str(parsed.get("job_id", "")),
        revised_text=str(parsed.get("content", "")),
    )
    ok = bool(isinstance(result, dict) and result.get("ok"))
    reason = str(result.get("reason", "unknown")) if isinstance(result, dict) else "invalid_result"

    if ok:
        reply = (
            "수정본 반영 완료.\n"
            f"job_id: {result.get('job_id')}\n"
            f"본문 길이: {result.get('content_length')}자\n\n"
            "이제 기존 초안 메시지의 승인 버튼을 누르면 수정본이 임시저장 대기열로 이동합니다."
        )
    elif reason == "content_too_short":
        reply = f"수정본이 너무 짧습니다. 최소 {result.get('min_content_chars', 80)}자 이상으로 보내주세요."
    elif reason == "invalid_job_status":
        reply = f"현재 수정할 수 없는 초안 상태입니다. current_status={result.get('current_status', '-')}"
    elif reason == "job_not_found":
        reply = "job_id에 해당하는 초안을 찾지 못했습니다."
    elif reason == "missing_payload":
        reply = "저장된 초안 본문을 찾지 못했습니다."
    else:
        reply = f"수정본 반영 실패: {reason}"

    if bot_token and chat_id is not None:
        await _send_telegram_reply(bot_token, chat_id, reply)

    return {
        "handled": True,
        "reason": reason,
        "ok": ok,
    }


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


class TelegramVerifyTokenRequest(BaseModel):
    """텔레그램 토큰 검증 요청."""

    bot_token: str = Field(min_length=10)


class TelegramVerifyTokenResponse(BaseModel):
    """텔레그램 토큰 검증 응답."""

    success: bool
    message: str
    bot_username: Optional[str] = None
    auth_code: Optional[str] = None
    auth_command: Optional[str] = None
    deep_link: Optional[str] = None
    expires_in_sec: int = _AUTH_CODE_TTL_SEC


class TelegramVerifyRequest(BaseModel):
    """텔레그램 인증코드 확인 요청."""

    auth_code: str = Field(min_length=3)


class TelegramVerifyResponse(BaseModel):
    """텔레그램 인증코드 확인 응답."""

    success: bool
    message: str
    bot_username: Optional[str] = None
    chat_id: Optional[str] = None
    used_fallback: bool = False


class TelegramWebhookResponse(BaseModel):
    """텔레그램 웹훅 처리 응답."""

    ok: bool
    stored: bool = False
    reason: Optional[str] = None
    mapped_category: Optional[str] = None
    auth_verified: Optional[bool] = None
    callback_handled: Optional[bool] = None
    callback_action: Optional[str] = None


@router.post(
    "/telegram/verify-token",
    response_model=TelegramVerifyTokenResponse,
    summary="Telegram bot token 검증 및 인증코드 발급",
)
async def verify_telegram_token(
    request: TelegramVerifyTokenRequest,
) -> TelegramVerifyTokenResponse:
    """Telegram getMe API로 토큰을 검증하고 1회용 인증코드를 발급한다."""
    bot_token = str(request.bot_token or "").strip()
    if not _BOT_TOKEN_PATTERN.fullmatch(bot_token):
        raise HTTPException(status_code=422, detail="Bot Token 형식이 올바르지 않습니다.")

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            payload = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram getMe 요청 실패: {exc}") from exc

    if not payload.get("ok"):
        description = str(payload.get("description", "Bot Token 검증 실패"))
        raise HTTPException(status_code=422, detail=description)

    bot_username = str(payload.get("result", {}).get("username", "")).strip()
    if not bot_username:
        raise HTTPException(status_code=422, detail="Telegram bot username을 찾지 못했습니다.")

    auth_code = secrets.token_hex(3).lower()
    await _register_pending_auth_code(
        auth_code,
        bot_token=bot_token,
        bot_username=bot_username,
    )
    auth_command = f"/start {_AUTH_CODE_PREFIX}{auth_code}"
    deep_link = f"https://t.me/{bot_username}?start={_AUTH_CODE_PREFIX}{auth_code}"

    return TelegramVerifyTokenResponse(
        success=True,
        message="토큰 검증이 완료되었습니다. 인증 명령을 봇 채팅에 전송해 주세요.",
        bot_username=bot_username,
        auth_code=auth_code,
        auth_command=auth_command,
        deep_link=deep_link,
        expires_in_sec=_AUTH_CODE_TTL_SEC,
    )


@router.post(
    "/telegram/verify",
    response_model=TelegramVerifyResponse,
    summary="Telegram 인증코드 승인 확인 및 chat_id 저장",
)
async def verify_telegram_connection(
    request: TelegramVerifyRequest,
    job_store: Any = Depends(get_job_store),
) -> TelegramVerifyResponse:
    """웹훅에서 승인된 인증코드를 확인해 chat_id를 저장한다."""
    auth_code = _normalize_auth_code(request.auth_code)
    if not auth_code:
        raise HTTPException(status_code=422, detail="인증코드를 입력해 주세요.")

    state = await _get_pending_auth_state(auth_code)
    if not state:
        raise HTTPException(status_code=404, detail="인증코드가 없거나 만료되었습니다. 다시 시도해 주세요.")

    used_fallback = False
    chat_id = state.verified_chat_id.strip()

    # 웹훅 유실 시 getUpdates 폴백으로 1회 확인한다.
    if not chat_id:
        fallback_chat_id = await _try_collect_auth_code_from_updates(state.bot_token, auth_code)
        if fallback_chat_id:
            await _mark_auth_code_verified(auth_code, fallback_chat_id)
            chat_id = fallback_chat_id
            used_fallback = True

    if not chat_id:
        raise HTTPException(
            status_code=409,
            detail="아직 인증 메시지를 확인하지 못했습니다. 봇 개인채팅에서 인증 명령 전송 후 다시 시도해 주세요.",
        )

    completed = await _pop_pending_auth_state(auth_code)
    if not completed:
        raise HTTPException(status_code=409, detail="인증코드가 만료되었습니다. 다시 시도해 주세요.")

    job_store.set_system_setting("telegram_bot_token", completed.bot_token.strip())
    job_store.set_system_setting("telegram_chat_id", chat_id)
    if not str(job_store.get_system_setting("telegram_webhook_secret", "")).strip():
        job_store.set_system_setting("telegram_webhook_secret", secrets.token_urlsafe(24))

    await _send_telegram_reply(
        completed.bot_token,
        chat_id,
        "✅ Auto Blog 텔레그램 연동이 완료되었습니다.",
    )

    return TelegramVerifyResponse(
        success=True,
        message="텔레그램 연동이 완료되었습니다.",
        bot_username=completed.bot_username,
        chat_id=chat_id,
        used_fallback=used_fallback,
    )


@router.post(
    "/telegram/webhook",
    response_model=TelegramWebhookResponse,
    summary="텔레그램 봇 Webhook 수신 (Track B)",
    status_code=200,
)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    job_store: Any = Depends(get_job_store),
) -> TelegramWebhookResponse:
    """텔레그램 봇으로 전송된 메시지를 수신해 아이디어 금고에 적재한다.

    보안 흐름:
    1. TELEGRAM_WEBHOOK_SECRET 이 설정된 경우, 헤더와 비교 → 불일치 시 403
    2. TELEGRAM_CHAT_ID 가 설정된 경우, 발신자 chat_id 필터 → 불일치 시 403
    3. 메시지 텍스트를 IdeaVaultBatchParser 로 정제 후 vault 에 저장
    4. "✅ 금고 적재: [카테고리]" 답장 전송
    """
    # 1. Webhook Secret 검증
    secret = _get_webhook_secret(job_store)
    if secret:
        if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret token")

    # 2. 페이로드 파싱
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    expire_pending_draft_revision_sessions(job_store)

    callback_query: Dict[str, Any] = payload.get("callback_query") or {}
    if callback_query:
        callback_result = await _handle_draft_approval_callback_query(callback_query, job_store)
        if (
            not bool(callback_result.get("handled", False))
            and str(callback_result.get("reason", "")) == "not_draft_callback"
        ):
            callback_result = await _handle_draft_saved_callback_query(callback_query, job_store)
        if (
            not bool(callback_result.get("handled", False))
            and str(callback_result.get("reason", "")) == "not_draft_saved_callback"
        ):
            callback_result = await _handle_macro_candidate_callback_query(callback_query, job_store)
        if (
            not bool(callback_result.get("handled", False))
            and str(callback_result.get("reason", "")) == "not_macro_callback"
        ):
            callback_result = await _handle_feedback_callback_query(callback_query, job_store)
        return {
            "ok": True,
            "stored": False,
            "reason": callback_result.get("reason"),
            "callback_handled": bool(callback_result.get("handled", False)),
            "callback_action": str(callback_result.get("action", "")).strip() or None,
        }

    message: Dict[str, Any] = payload.get("message") or payload.get("edited_message") or {}
    if not message:
        # 채널 포스트 등 무시
        return {"ok": True, "stored": False, "reason": "no_message"}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = str(chat.get("type", "")).lower()
    text: str = message.get("text", "").strip()

    # 인증 명령은 아이디어 금고 처리보다 우선한다.
    auth_code = _extract_auth_code_from_text(text)
    if auth_code:
        if chat_type != "private":
            return {
                "ok": True,
                "stored": False,
                "reason": "auth_requires_private_chat",
            }
        verified = await _mark_auth_code_verified(auth_code, str(chat_id))
        if verified:
            bot_token = _get_bot_token(job_store)
            if bot_token and chat_id is not None:
                asyncio.create_task(
                    _send_telegram_reply(
                        bot_token,
                        chat_id,
                        "✅ 인증코드 확인 완료! 앱 화면에서 [연동 확인 및 완료]를 눌러주세요.",
                    )
                )
            return {
                "ok": True,
                "stored": False,
                "reason": "auth_code_verified",
                "auth_verified": True,
            }
        return {
            "ok": True,
            "stored": False,
            "reason": "auth_code_not_found_or_expired",
            "auth_verified": False,
        }

    # 3. Chat ID 필터 (선택적)
    allowed_chat_id = _get_allowed_chat_id(job_store)
    if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    document_result = await _handle_draft_document_message(
        message=message,
        chat_id=chat_id,
        job_store=job_store,
    )
    if bool(document_result.get("handled", False)):
        return {
            "ok": True,
            "stored": False,
            "reason": document_result.get("reason"),
        }

    if not text:
        return {"ok": True, "stored": False, "reason": "empty_text"}

    draft_update_result = await _handle_draft_update_message(
        text=text,
        chat_id=chat_id,
        job_store=job_store,
    )
    if bool(draft_update_result.get("handled", False)):
        return {
            "ok": True,
            "stored": False,
            "reason": draft_update_result.get("reason"),
        }

    pending_revision_result = await _handle_pending_draft_revision_message(
        text=text,
        chat_id=chat_id,
        job_store=job_store,
    )
    if bool(pending_revision_result.get("handled", False)):
        return {
            "ok": True,
            "stored": False,
            "reason": pending_revision_result.get("reason"),
        }

    # 4. 아이디어 금고 적재
    category = await _parse_and_store_text(text, job_store)

    # 5. 봇 답장 (비차단)
    bot_token = _get_bot_token(job_store)
    if bot_token and chat_id is not None:
        reply = f"✅ 금고 적재: [{category or '기타'}]"
        asyncio.create_task(_send_telegram_reply(bot_token, chat_id, reply))

    return {"ok": True, "stored": True, "mapped_category": category}


# ---------------------------------------------------------------------------
# 오프라인 폴백: getUpdates 호출
# ---------------------------------------------------------------------------


async def collect_pending_updates(job_store: Any) -> int:
    """웹훅 없이 운영할 때 오프라인 구간의 메시지를 수집한다.

    Telegram getUpdates long-polling 방식으로 offset 관리를 통해
    이미 처리한 update_id 는 건너뛴다.

    Returns:
        새로 적재된 아이디어 아이템 수.
    """
    bot_token = _get_bot_token(job_store)
    if not bot_token:
        logger.debug("Telegram offline fallback: no bot token configured, skipping")
        return 0

    expire_pending_draft_revision_sessions(job_store)

    # 마지막 처리 update_id 조회
    last_id_raw = job_store.get_system_setting(_LAST_UPDATE_ID_KEY, "0")
    try:
        last_processed_id = int(last_id_raw)
    except (ValueError, TypeError):
        last_processed_id = 0

    # getUpdates 호출
    offset = last_processed_id + 1 if last_processed_id > 0 else 0
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        params: Dict[str, Any] = {
            "limit": _GETUP_LIMIT,
            "timeout": 0,
            # Telegram은 allowed_updates를 마지막 호출 값으로 기억한다.
            # 다른 폴러가 message만 요청해도 승인 버튼 callback_query는 계속 받아야 한다.
            "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
        }
        if offset > 0:
            params["offset"] = offset

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
    except Exception as exc:
        logger.warning("Telegram offline fallback: getUpdates failed: %s", exc)
        return 0

    if not data.get("ok"):
        logger.warning("Telegram offline fallback: getUpdates returned ok=false: %s", data)
        return 0

    updates: List[Dict[str, Any]] = data.get("result", [])
    if not updates:
        logger.info("Telegram offline fallback: no pending updates")
        return 0

    logger.info("Telegram offline fallback: processing %d pending updates", len(updates))

    allowed_chat_id = _get_allowed_chat_id(job_store)
    stored = 0
    max_update_id = last_processed_id

    for update in updates:
        update_id: int = update.get("update_id", 0)
        callback_query: Dict[str, Any] = update.get("callback_query") or {}
        if callback_query:
            try:
                callback_result = await _handle_draft_approval_callback_query(callback_query, job_store)
                if (
                    not bool(callback_result.get("handled", False))
                    and str(callback_result.get("reason", "")) == "not_draft_callback"
                ):
                    callback_result = await _handle_draft_saved_callback_query(callback_query, job_store)
                if (
                    not bool(callback_result.get("handled", False))
                    and str(callback_result.get("reason", "")) == "not_draft_saved_callback"
                ):
                    callback_result = await _handle_macro_candidate_callback_query(callback_query, job_store)
                if (
                    not bool(callback_result.get("handled", False))
                    and str(callback_result.get("reason", "")) == "not_macro_callback"
                ):
                    await _handle_feedback_callback_query(callback_query, job_store)
            except Exception as exc:
                logger.warning("Telegram offline fallback: callback handling failed for update %d: %s", update_id, exc)
            max_update_id = max(max_update_id, update_id)
            continue

        message: Dict[str, Any] = update.get("message") or update.get("edited_message") or {}
        text: str = message.get("text", "").strip()
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        chat_type = str(chat.get("type", "")).lower()

        # Chat ID 필터
        if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
            max_update_id = max(max_update_id, update_id)
            continue

        # 인증 명령은 금고 적재에서 제외하고 승인 상태만 반영한다.
        auth_code = _extract_auth_code_from_text(text)
        if auth_code and chat_type == "private":
            await _mark_auth_code_verified(auth_code, str(chat_id))
            max_update_id = max(max_update_id, update_id)
            continue

        try:
            document_result = await _handle_draft_document_message(
                message=message,
                chat_id=chat_id,
                job_store=job_store,
            )
            if bool(document_result.get("handled", False)):
                max_update_id = max(max_update_id, update_id)
                continue
        except Exception as exc:
            logger.warning("Telegram offline fallback: document handling failed for update %d: %s", update_id, exc)
            max_update_id = max(max_update_id, update_id)
            continue

        if text:
            try:
                draft_update_result = await _handle_draft_update_message(
                    text=text,
                    chat_id=chat_id,
                    job_store=job_store,
                )
                if bool(draft_update_result.get("handled", False)):
                    max_update_id = max(max_update_id, update_id)
                    continue
                pending_revision_result = await _handle_pending_draft_revision_message(
                    text=text,
                    chat_id=chat_id,
                    job_store=job_store,
                )
                if bool(pending_revision_result.get("handled", False)):
                    max_update_id = max(max_update_id, update_id)
                    continue
                category = await _parse_and_store_text(text, job_store)
                if category is not None:
                    stored += 1
            except Exception as exc:
                logger.warning("Telegram offline fallback: store failed for update %d: %s", update_id, exc)

        max_update_id = max(max_update_id, update_id)

    # 마지막 처리 ID 저장
    if max_update_id > last_processed_id:
        job_store.set_system_setting(_LAST_UPDATE_ID_KEY, str(max_update_id))

    logger.info("Telegram offline fallback: stored %d new vault items", stored)
    return stored


# ---------------------------------------------------------------------------
# 텔레그램 라이브 상태 엔드포인트
# ---------------------------------------------------------------------------


class TelegramStatusResponse(BaseModel):
    """텔레그램 봇 라이브 연결 상태."""

    configured: bool
    live_ok: bool
    bot_username: Optional[str] = None
    error: Optional[str] = None


@router.get(
    "/telegram/status",
    response_model=TelegramStatusResponse,
    summary="텔레그램 봇 라이브 상태 확인",
)
async def get_telegram_status(
    job_store: Any = Depends(get_job_store),
) -> TelegramStatusResponse:
    """Telegram getMe API를 호출해 봇 연결 상태를 실시간으로 확인한다."""
    bot_token = _get_bot_token(job_store)

    if not bot_token:
        return TelegramStatusResponse(
            configured=False,
            live_ok=False,
            error="봇 토큰 미설정",
        )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getMe"
            )
            data = resp.json()

        if data.get("ok"):
            username = data.get("result", {}).get("username")
            return TelegramStatusResponse(
                configured=True,
                live_ok=True,
                bot_username=username,
            )
        else:
            description = data.get("description", "알 수 없는 오류")
            return TelegramStatusResponse(
                configured=True,
                live_ok=False,
                error=description,
            )
    except Exception as exc:
        logger.warning("Telegram status check failed: %s", exc)
        return TelegramStatusResponse(
            configured=True,
            live_ok=False,
            error=str(exc),
        )
