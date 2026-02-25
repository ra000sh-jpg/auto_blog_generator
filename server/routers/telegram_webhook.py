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
    """DB 또는 환경변수에서 허용 Chat ID 를 조회한다 (빈 값 = 모두 허용)."""
    chat_id = ""
    try:
        chat_id = str(job_store.get_system_setting("telegram_chat_id", "")).strip()
    except Exception:
        pass
    if not chat_id:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return chat_id or None


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
) -> None:
    """Telegram sendMessage API 로 답장을 보낸다 (비차단, 오류 무시)."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as exc:
        logger.debug("Telegram reply failed (ignored): %s", exc)


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

    if not text:
        return {"ok": True, "stored": False, "reason": "empty_text"}

    # 4. 아이디어 금고 적재
    category = await _parse_and_store_text(text, job_store)

    # 5. 봇 답장 (비차단)
    bot_token = _get_bot_token(job_store)
    if bot_token and chat_id is not None:
        reply = f"✅ 금고 적재: [{category or '기타'}]"
        asyncio.create_task(_send_telegram_reply(bot_token, chat_id, reply))

    return {"ok": True, "stored": True, "mapped_category": category}


# ---------------------------------------------------------------------------
# 오프라인 폴백: 시작 시 getUpdates 호출
# ---------------------------------------------------------------------------


async def collect_pending_updates(job_store: Any) -> int:
    """FastAPI lifespan 시작 시 오프라인 구간의 메시지를 수집한다.

    Telegram getUpdates long-polling 방식으로 offset 관리를 통해
    이미 처리한 update_id 는 건너뛴다.

    Returns:
        새로 적재된 아이디어 아이템 수.
    """
    bot_token = _get_bot_token(job_store)
    if not bot_token:
        logger.debug("Telegram offline fallback: no bot token configured, skipping")
        return 0

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
        params: Dict[str, Any] = {"limit": _GETUP_LIMIT, "timeout": 0}
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

        if text:
            try:
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
