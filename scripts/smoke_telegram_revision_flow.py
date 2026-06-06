#!/usr/bin/env python3
"""텔레그램 초안 수정/승인 흐름을 실제 봇으로 점검한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from modules.automation.draft_approval import (
    DraftApprovalRequest,
    apply_draft_callback_action,
    apply_draft_manual_revision,
    apply_pending_draft_revision,
    build_draft_preview_message,
    build_inline_keyboard,
    create_draft_approval_request,
    get_approval_ttl_hours,
    get_preview_chars,
    get_revision_timeout_minutes,
    parse_draft_callback_data,
    parse_draft_update_message,
    start_draft_revision_session,
)
from modules.automation.job_store import JobConfig, JobStore
from modules.automation.notifier import TelegramNotifier
from modules.automation.time_utils import now_utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="텔레그램 초안 수정/승인 라이브 스모크 테스트",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        help="스케줄러/큐 DB 경로",
    )
    parser.add_argument(
        "--title",
        default="",
        help="테스트 초안 제목. 비우면 자동 생성",
    )
    parser.add_argument(
        "--job-id",
        default="",
        help="테스트 job_id. 비우면 자동 생성",
    )
    parser.add_argument(
        "--poll-sec",
        type=int,
        default=0,
        help="텔레그램 getUpdates를 폴링할 시간(초). 0이면 메시지만 전송",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=0,
        help="텔레그램 미리보기 본문 길이. 0이면 설정값 사용",
    )
    return parser.parse_args()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _build_payload(title: str) -> Dict[str, Any]:
    """수정필요 상태를 의도적으로 포함한 점검용 초안을 만든다."""
    content = (
        f"## {title}\n\n"
        "오늘은 국장 시작 전에 시장을 맞히겠다는 마음보다, 제가 어떤 기준으로 흔들림을 줄일지부터 "
        "같이 확인해보려 합니다. 지난밤 미장과 환율, 금리, 코인 흐름은 각각 따로 움직이는 뉴스처럼 "
        "보이지만, 막상 장이 열리면 외국인 수급과 반도체 대형주에 하나의 압력처럼 모이는 경우가 많았습니다.\n\n"
        "저도 아직 이 흐름을 완벽하게 해석한다고 말할 수는 없습니다. 다만 초심자일수록 더 많은 종목을 "
        "붙잡기보다, 오늘 무엇을 줄이고 무엇을 보류할지 먼저 정리하는 편이 더 현실적이라고 느낍니다.\n\n"
        "## 오늘 같이 확인할 기준\n\n"
        "첫째, 환율이 다시 올라가면 국장에서는 위험을 넓히기보다 반도체와 지수 대형주의 수급을 먼저 "
        "보겠습니다. 둘째, 미 10년물 금리가 강하게 버티면 성장주 기대감보다 포지션 크기를 줄이는 기준을 "
        "앞에 두겠습니다. 셋째, 코인이 강하더라도 그것을 매수 신호로 단정하지 않고 위험 선호의 온도계로만 "
        "다루겠습니다.\n\n"
        "결국 오늘의 질문은 단순합니다. 시장이 내 생각과 다르게 움직일 때, 저는 무엇을 더할 것인가가 아니라 "
        "무엇을 덜어낼 것인가를 먼저 볼 수 있을까요. 이 기준 하나만 지켜도 하루의 판단은 조금 덜 거칠어질 수 "
        "있다고 봅니다.\n"
    )
    return {
        "title": title,
        "content": content,
        "tags": ["국장", "미장", "투자공부", "초심자", "시장브리핑"],
        "category": "economy",
        "images": [],
        "thumbnail": "",
        "image_sources": {},
        "image_points": [],
        "quality_snapshot": {
            "score": 86,
            "manual_revision_applied": False,
            "insight_quality": {
                "overall_score": 82,
                "needs_rewrite": True,
                "reasons": [
                    "스모크 테스트용으로 수정필요 상태를 강제로 표시합니다.",
                    "사용자가 텔레그램에서 수정본을 보내면 이 값이 승인 가능 상태로 바뀝니다.",
                ],
            },
        },
        "seo_snapshot": {
            "topic_mode": "economy",
            "provider_used": "smoke",
            "provider_model": "manual",
        },
    }


def _create_awaiting_job(store: JobStore, *, job_id: str, title: str) -> Dict[str, Any]:
    """실제 큐에 승인 대기 초안을 만든다."""
    scheduled_at = now_utc()
    created = store.schedule_job(
        job_id=job_id,
        title=title,
        seed_keywords=["국장", "미장", "투자공부", "초심자"],
        platform="naver",
        persona_id="P4",
        scheduled_at=scheduled_at,
        tags=["국장", "미장", "투자공부", "초심자"],
        category="economy",
        status=store.STATUS_RUNNING,
    )
    if not created:
        raise RuntimeError(f"테스트 job 생성 실패: {job_id}")

    payload = _build_payload(title)
    if not store.save_prepared_payload(job_id, payload, mark_ready=False):
        raise RuntimeError(f"prepared_payload 저장 실패: {job_id}")
    if not store.update_job_status(job_id, store.STATUS_AWAITING_APPROVAL):
        raise RuntimeError(f"awaiting_approval 전환 실패: {job_id}")
    return payload


def _telegram_post(bot_token: str, method: str, payload: Dict[str, Any], *, timeout: float = 12.0) -> Dict[str, Any]:
    """텔레그램 Bot API를 호출한다."""
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"https://api.telegram.org/bot{bot_token}/{method}", json=payload)
    try:
        parsed = response.json()
    except json.JSONDecodeError:
        parsed = {
            "ok": False,
            "status_code": response.status_code,
            "description": response.text[:240],
        }
    return parsed if isinstance(parsed, dict) else {}


def _get_updates(bot_token: str, *, offset: int = 0, timeout_sec: int = 0) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"limit": 20, "timeout": max(0, int(timeout_sec))}
    if offset > 0:
        payload["offset"] = offset
    return _telegram_post(bot_token, "getUpdates", payload, timeout=max(12.0, timeout_sec + 4.0))


def _get_initial_offset(bot_token: str) -> int:
    """기존 누적 업데이트를 건너뛸 시작 offset을 계산한다."""
    try:
        data = _get_updates(bot_token, timeout_sec=0)
    except Exception as exc:
        print(f"[poll] 기존 업데이트 확인 실패: {exc}")
        return 0
    if not data.get("ok"):
        print(f"[poll] getUpdates 사용 불가: {str(data)[:240]}")
        return 0
    updates = data.get("result", [])
    if not isinstance(updates, list) or not updates:
        return 0
    update_ids = [int(item.get("update_id", 0) or 0) for item in updates if isinstance(item, dict)]
    return max(update_ids or [0]) + 1


def _send_reply(bot_token: str, chat_id: Any, text: str) -> None:
    try:
        _telegram_post(
            bot_token,
            "sendMessage",
            {"chat_id": str(chat_id), "text": text},
        )
    except Exception as exc:
        print(f"[poll] 회신 전송 실패: {exc}")


def _answer_callback(bot_token: str, callback_query_id: str, text: str) -> None:
    if not callback_query_id:
        return
    try:
        _telegram_post(
            bot_token,
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text, "show_alert": "false"},
        )
    except Exception as exc:
        print(f"[poll] callback 응답 실패: {exc}")


def _handle_pending_revision_text(store: JobStore, bot_token: str, chat_id: Any, text: str) -> bool:
    """수정본 입력 대기 상태의 평문을 처리한다."""
    result = apply_pending_draft_revision(
        store,
        chat_id=chat_id,
        revised_text=text,
    )
    if not result.get("handled"):
        return False

    if result.get("ok"):
        job_id = str(result.get("job_id", "") or "")
        payload = result.get("payload", {})
        title = str(result.get("title", "") or "")
        if not title and isinstance(payload, dict):
            title = str(payload.get("title", "") or "")
        request = DraftApprovalRequest(
            approval_id=str(result.get("approval_id", "")),
            token=str(result.get("token", "")),
            job_id=job_id,
            expires_at=str(result.get("expires_at", "")),
        )
        preview = build_draft_preview_message(
            job_id=job_id,
            title=title,
            payload=payload if isinstance(payload, dict) else {},
            expires_at=request.expires_at,
            preview_chars=get_preview_chars(store),
        )
        try:
            _telegram_post(
                bot_token,
                "sendMessage",
                {
                    "chat_id": str(chat_id),
                    "text": (
                        "수정본 반영 완료.\n"
                        f"job_id: {job_id}\n"
                        f"본문 길이: {result.get('content_length')}자\n\n"
                        f"{preview}"
                    ),
                    "reply_markup": build_inline_keyboard(request),
                },
            )
        except Exception as exc:
            print(f"[poll] 수정본 미리보기 전송 실패: {exc}")
    elif str(result.get("reason", "")) == "content_too_short":
        _send_reply(
            bot_token,
            chat_id,
            f"수정본이 너무 짧습니다. 최소 {result.get('min_content_chars', 80)}자 이상으로 다시 보내주세요.",
        )
    elif str(result.get("reason", "")) == "revision_timeout":
        _send_reply(bot_token, chat_id, "수정본 입력 제한시간이 지나 이 초안의 오늘 업로드를 취소했습니다.")
    else:
        _send_reply(bot_token, chat_id, f"수정본 반영 실패: {result.get('reason', 'unknown')}")
    return True


def _handle_text_update(store: JobStore, bot_token: str, chat_id: Any, text: str) -> bool:
    parsed = parse_draft_update_message(text)
    if not parsed:
        return _handle_pending_revision_text(store, bot_token, chat_id, text)
    if parsed.get("error"):
        _send_reply(bot_token, chat_id, "형식이 맞지 않습니다.\n\n/draft_update job_id\n수정한 완성본 본문")
        return True

    result = apply_draft_manual_revision(
        store,
        job_id=str(parsed.get("job_id", "")),
        revised_text=str(parsed.get("content", "")),
    )
    if result.get("ok"):
        _send_reply(
            bot_token,
            chat_id,
            "수정본 반영 완료.\n"
            f"job_id: {result.get('job_id')}\n"
            f"본문 길이: {result.get('content_length')}자\n\n"
            "이제 기존 초안 메시지의 승인 버튼을 누르면 됩니다.",
        )
    else:
        _send_reply(bot_token, chat_id, f"수정본 반영 실패: {result.get('reason', 'unknown')}")
    return True


def _handle_callback_update(store: JobStore, bot_token: str, callback_query: Dict[str, Any]) -> bool:
    data = str(callback_query.get("data", "") or "")
    parsed = parse_draft_callback_data(data)
    if not parsed:
        return False

    result = apply_draft_callback_action(
        store,
        approval_id=str(parsed.get("approval_id", "")),
        token=str(parsed.get("token", "")),
        action=str(parsed.get("action", "")),
    )
    callback_query_id = str(callback_query.get("id", "") or "")
    if result.get("ok"):
        status = str(result.get("status", ""))
        action = str(result.get("action", ""))
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if action == "revise" and chat_id is not None:
            revision = start_draft_revision_session(
                store,
                chat_id=chat_id,
                job_id=str(result.get("job_id", "")),
                approval_id=str(parsed.get("approval_id", "")),
                token=str(parsed.get("token", "")),
                timeout_minutes=get_revision_timeout_minutes(store),
            )
            _answer_callback(bot_token, callback_query_id, "수정본 입력 대기 상태로 전환했습니다.")
            _send_reply(
                bot_token,
                chat_id,
                "수정본 입력 모드로 전환했습니다.\n"
                "이 메시지 다음에 수정한 완성본 전체를 그대로 붙여넣어 주세요.\n"
                f"{revision.get('timeout_minutes', get_revision_timeout_minutes(store))}분 안에 입력하지 않으면 오늘 업로드는 취소됩니다.",
            )
        else:
            _answer_callback(bot_token, callback_query_id, f"{action} 완료: {status}")
        if chat_id is not None and action != "revise":
            _send_reply(
                bot_token,
                chat_id,
                f"승인 처리 완료.\njob_id: {result.get('job_id')}\nstatus: {status}",
            )
    else:
        _answer_callback(bot_token, callback_query_id, f"처리 실패: {result.get('reason', 'unknown')}")
    return True


def _poll_telegram(
    *,
    store: JobStore,
    notifier: TelegramNotifier,
    job_id: str,
    poll_sec: int,
    initial_offset: int,
) -> None:
    """수정본/승인 버튼 입력을 짧게 폴링한다."""
    bot_token = notifier.bot_token
    allowed_chat_id = str(notifier.chat_id)
    offset = max(0, int(initial_offset))
    started = time.monotonic()
    deadline = started + max(1, int(poll_sec))
    last_status = ""

    print(f"[poll] {poll_sec}초 동안 텔레그램 입력을 기다립니다.")
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        status = str(job.status if job else "missing")
        if status != last_status:
            print(f"[poll] job status: {status}")
            last_status = status
        if status in {store.STATUS_READY, store.STATUS_CANCELLED}:
            break

        try:
            data = _get_updates(bot_token, offset=offset, timeout_sec=4)
        except Exception as exc:
            print(f"[poll] getUpdates 실패: {exc}")
            break
        if not data.get("ok"):
            print(f"[poll] getUpdates 응답 실패: {str(data)[:240]}")
            break

        updates = data.get("result", [])
        if not isinstance(updates, list):
            time.sleep(1)
            continue

        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", 0) or 0)
            offset = max(offset, update_id + 1)

            callback_query = update.get("callback_query") or {}
            if isinstance(callback_query, dict) and callback_query:
                message = callback_query.get("message") or {}
                chat = message.get("chat") or {}
                if chat.get("id") is None or str(chat.get("id")) == allowed_chat_id:
                    _handle_callback_update(store, bot_token, callback_query)
                continue

            message = update.get("message") or update.get("edited_message") or {}
            if not isinstance(message, dict):
                continue
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is not None and str(chat_id) != allowed_chat_id:
                continue
            text = str(message.get("text", "") or "").strip()
            if text:
                _handle_text_update(store, bot_token, chat_id, text)

        time.sleep(1)

    job = store.get_job(job_id)
    final_status = str(job.status if job else "missing")
    payload = store.load_prepared_payload(job_id)
    quality_snapshot = payload.get("quality_snapshot", {}) if isinstance(payload, dict) else {}
    manual_revision = bool(
        isinstance(quality_snapshot, dict)
        and quality_snapshot.get("manual_revision_applied", False)
    )
    print(f"[poll] 종료: status={final_status}, manual_revision_applied={manual_revision}")


async def main_async(args: argparse.Namespace) -> int:
    store = JobStore(db_path=args.db, config=JobConfig(max_llm_calls_per_job=15))
    notifier = TelegramNotifier.from_env(db_path=store.db_path)

    print("[config]")
    print(f"DB: {store.db_path}")
    print(f"Telegram configured: {'yes' if notifier.enabled else 'no'}")
    if not notifier.enabled:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 또는 DB system_settings 설정이 필요합니다.")
        return 2

    initial_offset = 0
    if args.poll_sec > 0:
        initial_offset = _get_initial_offset(notifier.bot_token)

    stamp = _utc_stamp()
    title = str(args.title or f"[SMOKE] 텔레그램 수정 승인 점검 {stamp}").strip()
    job_id = str(args.job_id or f"smoke-telegram-revision-{stamp}").strip()
    payload = _create_awaiting_job(store, job_id=job_id, title=title)

    request = create_draft_approval_request(
        store,
        job_id=job_id,
        title=title,
        ttl_hours=get_approval_ttl_hours(store),
    )
    preview_chars = args.preview_chars if args.preview_chars > 0 else get_preview_chars(store)
    message = build_draft_preview_message(
        job_id=job_id,
        title=title,
        payload=payload,
        expires_at=request.expires_at,
        preview_chars=preview_chars,
    )
    sent = await notifier.send_message(
        message,
        reply_markup=build_inline_keyboard(request),
    )
    if not sent:
        print("텔레그램 메시지 전송 실패. 토큰/채팅 ID/네트워크 상태를 확인해야 합니다.")
        return 3

    print("[sent]")
    print(f"job_id: {job_id}")
    print("텔레그램에 초안 승인 요청을 보냈습니다.")
    print("수정본 예시:")
    print(f"/draft_update {job_id}")
    print("제목: 수정된 제목")
    print("수정한 완성본 본문을 여기에 붙여넣기")

    if args.poll_sec > 0:
        _poll_telegram(
            store=store,
            notifier=notifier,
            job_id=job_id,
            poll_sec=args.poll_sec,
            initial_offset=initial_offset,
        )
    return 0


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
