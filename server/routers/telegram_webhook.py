"""텔레그램 봇 Webhook 라우터 (Track B — 아이디어 금고 자동 수집).

지원 기능:
1. POST /api/telegram/webhook  — 봇 토큰 비밀 헤더 검증 후 메시지를 idea_vault 에 적재
2. collect_pending_updates()   — FastAPI lifespan 시작 시 오프라인 구간의 메시지를 폴링

보안:
- TELEGRAM_WEBHOOK_SECRET 환경변수 (또는 DB system_settings) 를
  X-Telegram-Bot-Api-Secret-Token 헤더와 비교해 검증한다.
- 헤더 누락 또는 불일치 시 403 반환.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from server.dependencies import get_job_store

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# 내부 상수
# ---------------------------------------------------------------------------

_LAST_UPDATE_ID_KEY = "telegram_last_processed_update_id"
_GETUP_LIMIT = 100  # getUpdates 한 번에 가져올 최대 건수


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


@router.post(
    "/telegram/webhook",
    summary="텔레그램 봇 Webhook 수신 (Track B)",
    status_code=200,
)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    job_store: Any = Depends(get_job_store),
) -> Dict[str, Any]:
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

    chat_id = message.get("chat", {}).get("id")
    text: str = message.get("text", "").strip()

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
        chat_id = message.get("chat", {}).get("id")

        # Chat ID 필터
        if allowed_chat_id and chat_id is not None and str(chat_id) != str(allowed_chat_id):
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
