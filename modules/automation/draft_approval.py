"""텔레그램 기반 초안 승인 헬퍼."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..content_sources import summarize_strategy_for_message


CALLBACK_PREFIX = "abd:v1:"
ACTION_TO_CODE = {"approve": "a", "cancel": "c", "revise": "r"}
CODE_TO_ACTION = {value: key for key, value in ACTION_TO_CODE.items()}
STATUS_AWAITING_APPROVAL = "awaiting_approval"
DEFAULT_APPROVAL_TTL_HOURS = 24
DEFAULT_PREVIEW_CHARS = 1200
DEFAULT_REVISION_TIMEOUT_MINUTES = 30
DRAFT_UPDATE_COMMANDS = {"/draft_update", "/draftupdate", "/초안수정", "/수정본"}
DRAFT_BODY_START = "--- 본문 시작 ---"
DRAFT_BODY_END = "--- 본문 끝 ---"


@dataclass(frozen=True)
class DraftApprovalRequest:
    """텔레그램 승인 요청 메타데이터."""

    approval_id: str
    token: str
    job_id: str
    expires_at: str


def _now_utc() -> datetime:
    """UTC 현재 시각을 반환한다."""
    return datetime.now(timezone.utc)


def _approval_key(approval_id: str) -> str:
    """승인 요청 저장 키를 만든다."""
    return f"telegram_draft_approval:{approval_id}"


def _job_approval_key(job_id: str) -> str:
    """잡별 최신 승인 요청 키를 만든다."""
    return f"telegram_draft_approval_job:{job_id}"


def _revision_session_key(chat_id: str | int) -> str:
    """채팅방별 수정본 입력 대기 키를 만든다."""
    return f"telegram_draft_revision_session:{chat_id}"


def _parse_utc_datetime(raw_value: str) -> Optional[datetime]:
    """UTC ISO 문자열을 datetime으로 변환한다."""
    try:
        return datetime.strptime(str(raw_value or "").strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _format_utc(value: datetime) -> str:
    """UTC datetime을 저장용 문자열로 변환한다."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _truthy(raw_value: Any) -> bool:
    """환경변수/설정값을 bool로 해석한다."""
    normalized = str(raw_value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on", "enabled"}


def is_draft_approval_enabled(job_store: Any) -> bool:
    """텔레그램 초안 승인 모드 활성 여부를 반환한다."""
    raw_value = ""
    try:
        raw_value = str(job_store.get_system_setting("telegram_draft_approval_enabled", "") or "")
    except Exception:
        raw_value = ""
    if not raw_value.strip():
        raw_value = os.getenv("TELEGRAM_DRAFT_APPROVAL_ENABLED", "")
    return _truthy(raw_value)


def get_approval_ttl_hours(job_store: Any) -> int:
    """승인 버튼 유효시간을 반환한다."""
    raw_value = ""
    try:
        raw_value = str(job_store.get_system_setting("telegram_draft_approval_ttl_hours", "") or "")
    except Exception:
        raw_value = ""
    if not raw_value.strip():
        raw_value = os.getenv("TELEGRAM_DRAFT_APPROVAL_TTL_HOURS", "")
    try:
        return max(1, min(168, int(raw_value)))
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_TTL_HOURS


def get_preview_chars(job_store: Any) -> int:
    """텔레그램 미리보기 본문 길이를 반환한다."""
    raw_value = ""
    try:
        raw_value = str(job_store.get_system_setting("telegram_draft_preview_chars", "") or "")
    except Exception:
        raw_value = ""
    if not raw_value.strip():
        raw_value = os.getenv("TELEGRAM_DRAFT_PREVIEW_CHARS", "")
    try:
        return max(300, min(3000, int(raw_value)))
    except (TypeError, ValueError):
        return DEFAULT_PREVIEW_CHARS


def get_revision_timeout_minutes(job_store: Any) -> int:
    """수정본 입력 대기 제한시간을 반환한다."""
    raw_value = ""
    try:
        raw_value = str(job_store.get_system_setting("telegram_draft_revision_timeout_minutes", "") or "")
    except Exception:
        raw_value = ""
    if not raw_value.strip():
        raw_value = os.getenv("TELEGRAM_DRAFT_REVISION_TIMEOUT_MINUTES", "")
    try:
        return max(5, min(720, int(raw_value)))
    except (TypeError, ValueError):
        return DEFAULT_REVISION_TIMEOUT_MINUTES


def create_draft_approval_request(
    job_store: Any,
    *,
    job_id: str,
    title: str,
    ttl_hours: int = DEFAULT_APPROVAL_TTL_HOURS,
) -> DraftApprovalRequest:
    """승인 요청 토큰을 생성해 system_settings에 저장한다."""
    approval_id = secrets.token_hex(5)
    token = secrets.token_hex(4)
    expires_at_dt = _now_utc() + timedelta(hours=max(1, int(ttl_hours)))
    expires_at = expires_at_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "approval_id": approval_id,
        "job_id": str(job_id),
        "title": str(title or ""),
        "token": token,
        "status": "pending",
        "created_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at,
    }
    job_store.set_system_setting(_approval_key(approval_id), json.dumps(record, ensure_ascii=False))
    job_store.set_system_setting(_job_approval_key(str(job_id)), approval_id)
    return DraftApprovalRequest(
        approval_id=approval_id,
        token=token,
        job_id=str(job_id),
        expires_at=expires_at,
    )


def build_callback_data(*, action: str, approval_id: str, token: str) -> str:
    """텔레그램 inline button callback_data를 만든다."""
    action_code = ACTION_TO_CODE.get(str(action or "").strip().lower())
    if not action_code:
        raise ValueError(f"Unsupported draft approval action: {action}")
    return f"{CALLBACK_PREFIX}{action_code}:{approval_id}:{token}"


def is_draft_callback_data(raw_data: str) -> bool:
    """초안 승인 콜백인지 확인한다."""
    return str(raw_data or "").strip().startswith(CALLBACK_PREFIX)


def parse_draft_callback_data(raw_data: str) -> Optional[Dict[str, str]]:
    """초안 승인 callback_data를 파싱한다."""
    normalized = str(raw_data or "").strip()
    if not normalized.startswith(CALLBACK_PREFIX):
        return None
    parts = normalized.split(":")
    if len(parts) != 5:
        return None
    _, version, action_code, approval_id, token = parts
    if version != "v1":
        return None
    action = CODE_TO_ACTION.get(action_code)
    if not action or not approval_id or not token:
        return None
    return {
        "action": action,
        "approval_id": approval_id,
        "token": token,
    }


def build_inline_keyboard(request: DraftApprovalRequest) -> Dict[str, Any]:
    """승인/수정본입력 inline keyboard를 만든다."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "승인",
                    "callback_data": build_callback_data(
                        action="approve",
                        approval_id=request.approval_id,
                        token=request.token,
                    ),
                },
                {
                    "text": "수정본입력",
                    "callback_data": build_callback_data(
                        action="revise",
                        approval_id=request.approval_id,
                        token=request.token,
                    ),
                },
            ]
        ]
    }


def build_draft_preview_message(
    *,
    job_id: str,
    title: str,
    payload: Dict[str, Any],
    expires_at: str,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> str:
    """텔레그램으로 보낼 텍스트 초안 미리보기를 만든다."""
    content = str(payload.get("content") or payload.get("final_content") or "").strip()
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(content) > preview_chars:
        content = f"{content[:preview_chars].rstrip()}\n...(미리보기 생략)"

    tags_raw = payload.get("tags", [])
    tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
    category = str(payload.get("category", "") or "").strip() or "-"
    image_count = len(payload.get("images", []) or []) if isinstance(payload.get("images", []), list) else 0
    quality_snapshot = payload.get("quality_snapshot", {})
    insight_quality = quality_snapshot.get("insight_quality", {}) if isinstance(quality_snapshot, dict) else {}
    insight_score = 0
    needs_rewrite = False
    if isinstance(insight_quality, dict):
        try:
            insight_score = int(insight_quality.get("overall_score", 0) or 0)
        except (TypeError, ValueError):
            insight_score = 0
        needs_rewrite = bool(insight_quality.get("needs_rewrite", False))

    if insight_score > 0 and needs_rewrite:
        status_line = f"상태: 수정필요 (통찰 품질 {insight_score}/100)"
    elif insight_score > 0:
        status_line = f"상태: 승인 가능 (통찰 품질 {insight_score}/100)"
    else:
        status_line = "상태: 승인 대기"

    lines = [
        "AutoBlog 초안 승인 요청",
        status_line,
        f"제목: {str(title or '').strip()}",
        f"job_id: {job_id}",
        f"카테고리: {category}",
        f"태그: {', '.join(tags[:8]) if tags else '-'}",
        f"이미지: {image_count}개",
        f"버튼 유효기간: {expires_at}",
        "",
        "본문 미리보기",
        content or "(본문 없음)",
        "",
        "수정하려면 아래 `수정본입력` 버튼을 누른 뒤 완성본 본문을 그대로 보내주세요.",
        "제목까지 바꾸려면 수정본 첫 줄에 `제목: 새 제목`을 넣어주세요.",
        f"명령어 백업: /draft_update {job_id}",
        "",
        "승인하면 ready_to_publish 큐로 이동하고, publisher 워커가 네이버 블로그 임시저장/발행 설정에 따라 처리합니다.",
    ]
    return "\n".join(lines)


def build_draft_compact_message(
    *,
    job_id: str,
    title: str,
    payload: Dict[str, Any],
    expires_at: str,
    reason: str = "",
) -> str:
    """텔레그램 초안 승인 요청을 짧은 메시지로 만든다."""

    quality_snapshot = payload.get("quality_snapshot", {})
    insight_quality = quality_snapshot.get("insight_quality", {}) if isinstance(quality_snapshot, dict) else {}
    insight_score = 0
    needs_rewrite = False
    if isinstance(insight_quality, dict):
        try:
            insight_score = int(insight_quality.get("overall_score", 0) or 0)
        except (TypeError, ValueError):
            insight_score = 0
        needs_rewrite = bool(insight_quality.get("needs_rewrite", False))

    if reason:
        status_line = f"상태: {reason}"
    elif insight_score > 0 and needs_rewrite:
        status_line = f"상태: 수정필요 ({insight_score}/100)"
    elif insight_score > 0:
        status_line = f"상태: 승인 가능 ({insight_score}/100)"
    else:
        status_line = "상태: 승인 대기"

    tags_raw = payload.get("tags", [])
    tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
    strategy_summary = summarize_strategy_for_message(payload)
    lines = [
        "AutoBlog 초안 승인 요청",
        status_line,
        f"제목: {str(title or '').strip()}",
        f"job_id: {job_id}",
    ]
    if strategy_summary:
        if strategy_summary.get("label"):
            lines.append(f"추천 전략: {strategy_summary['label']}")
        if strategy_summary.get("intent"):
            lines.append(f"검색 의도: {strategy_summary['intent']}")
        if strategy_summary.get("axis"):
            lines.append(f"전략 비율: {strategy_summary['axis']}")
    if tags:
        lines.append(f"태그: {', '.join(tags[:5])}")
    if expires_at:
        lines.append(f"유효기간: {expires_at}")
    lines.extend(
        [
            "",
            "본문 TXT: 첨부됨",
            "수정은 TXT를 복사해 Grok/Gemini에서 고친 뒤, 수정본입력 버튼 다음 메시지로 보내거나 TXT 파일을 다시 업로드하세요.",
        ]
    )
    return "\n".join(lines)


def build_draft_text_attachment(
    *,
    job_id: str,
    title: str,
    payload: Dict[str, Any],
    expires_at: str = "",
) -> str:
    """복사/수정용 전체 본문 TXT 내용을 만든다."""

    content = str(payload.get("content") or payload.get("final_content") or "").strip()
    tags_raw = payload.get("tags", [])
    tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
    category = str(payload.get("category", "") or "").strip()
    strategy_summary = summarize_strategy_for_message(payload)
    lines = [
        f"job_id: {str(job_id or '').strip()}",
        f"title: {str(title or payload.get('title', '') or '').strip()}",
        f"category: {category}",
        f"tags: {', '.join(tags)}",
    ]
    if strategy_summary:
        lines.extend(
            [
                f"writing_strategy: {strategy_summary.get('label', '')}",
                f"writing_intent: {strategy_summary.get('intent', '')}",
                f"writing_axis: {strategy_summary.get('axis', '')}",
            ]
        )
    if expires_at:
        lines.append(f"expires_at: {expires_at}")
    lines.extend(
        [
            "",
            "아래 본문만 수정해도 되고, 제목을 바꾸려면 title 값을 고쳐서 다시 업로드해도 됩니다.",
            "",
            DRAFT_BODY_START,
            content,
            DRAFT_BODY_END,
            "",
        ]
    )
    return "\n".join(lines)


def parse_draft_text_attachment(raw_text: str, *, fallback_job_id: str = "") -> Dict[str, str]:
    """업로드된 TXT 수정본에서 job_id와 본문을 추출한다."""

    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return {"job_id": "", "content": "", "error": "empty_document"}

    job_id = str(fallback_job_id or "").strip()
    title = ""
    for line in text.splitlines()[:20]:
        match = re.match(r"^\s*(job_id|title|제목)\s*:\s*(.+?)\s*$", line, flags=re.IGNORECASE)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key == "job_id":
            job_id = value
        elif key in {"title", "제목"}:
            title = value

    body = ""
    start_index = text.find(DRAFT_BODY_START)
    end_index = text.find(DRAFT_BODY_END)
    if start_index >= 0:
        start_index += len(DRAFT_BODY_START)
        body = text[start_index:end_index if end_index > start_index else None].strip()
    else:
        body = text

    if title and not re.match(r"^(?:제목|title)\s*:", body, flags=re.IGNORECASE):
        body = f"제목: {title}\n{body}".strip()

    return {
        "job_id": job_id,
        "content": body,
        "error": "" if body else "empty_content",
    }


def parse_draft_update_message(raw_text: str) -> Optional[Dict[str, str]]:
    """텔레그램 수정본 반영 명령을 파싱한다."""
    text = str(raw_text or "").strip()
    if not text:
        return None
    lines = text.splitlines()
    if not lines:
        return None

    first_line = lines[0].strip()
    tokens = first_line.split(maxsplit=1)
    if not tokens:
        return None
    command = tokens[0].strip().lower()
    if command not in DRAFT_UPDATE_COMMANDS:
        return None

    if len(tokens) >= 2 and tokens[1].strip():
        job_id = tokens[1].strip()
        revised_text = "\n".join(lines[1:]).strip()
    elif len(lines) >= 3:
        job_id = lines[1].strip()
        revised_text = "\n".join(lines[2:]).strip()
    else:
        return {
            "job_id": "",
            "content": "",
            "error": "invalid_format",
        }

    return {
        "job_id": job_id,
        "content": revised_text,
        "error": "" if job_id and revised_text else "invalid_format",
    }


def _split_optional_title(revised_text: str, fallback_title: str) -> tuple[str, str]:
    """수정본 첫 줄의 제목: 표기를 분리한다."""
    text = str(revised_text or "").strip()
    if not text:
        return str(fallback_title or "").strip(), ""
    lines = text.splitlines()
    first_line = lines[0].strip()
    match = re.match(r"^(?:제목|title)\s*:\s*(.+)$", first_line, flags=re.IGNORECASE)
    if not match:
        return str(fallback_title or "").strip(), text
    title = match.group(1).strip() or str(fallback_title or "").strip()
    body = "\n".join(lines[1:]).strip()
    return title, body


def apply_draft_manual_revision(
    job_store: Any,
    *,
    job_id: str,
    revised_text: str,
    min_content_chars: int = 80,
) -> Dict[str, Any]:
    """승인 대기 초안에 사용자가 보낸 수정본을 반영한다."""
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"ok": False, "reason": "job_id_missing"}

    job = job_store.get_job(normalized_job_id)
    if not job:
        return {"ok": False, "reason": "job_not_found"}

    awaiting_status = getattr(job_store, "STATUS_AWAITING_APPROVAL", STATUS_AWAITING_APPROVAL)
    if str(job.status) != str(awaiting_status):
        return {
            "ok": False,
            "reason": "invalid_job_status",
            "current_status": str(job.status),
        }

    payload = job_store.load_prepared_payload(normalized_job_id)
    if not payload:
        return {"ok": False, "reason": "missing_payload"}

    resolved_title, resolved_content = _split_optional_title(
        revised_text,
        str(payload.get("title", "") or job.title),
    )
    if len(resolved_content.strip()) < max(1, int(min_content_chars)):
        return {
            "ok": False,
            "reason": "content_too_short",
            "min_content_chars": max(1, int(min_content_chars)),
        }

    updated_payload = dict(payload)
    updated_payload["title"] = resolved_title or str(payload.get("title", "") or job.title)
    updated_payload["content"] = resolved_content

    quality_snapshot = updated_payload.get("quality_snapshot", {})
    if not isinstance(quality_snapshot, dict):
        quality_snapshot = {}
    quality_snapshot["manual_revision_applied"] = True
    quality_snapshot["manual_revision_at"] = _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    quality_snapshot["manual_revision_content_length"] = len(resolved_content)
    insight_quality = quality_snapshot.get("insight_quality", {})
    if isinstance(insight_quality, dict):
        insight_quality["manual_revision_applied"] = True
        insight_quality["needs_rewrite"] = False
        quality_snapshot["insight_quality"] = insight_quality
    updated_payload["quality_snapshot"] = quality_snapshot

    replace_fn = getattr(job_store, "replace_prepared_payload", None)
    if not callable(replace_fn):
        return {"ok": False, "reason": "payload_replace_unavailable"}

    updated = replace_fn(
        normalized_job_id,
        updated_payload,
        allowed_statuses=[awaiting_status],
        event_name="manual_revision_applied",
    )
    if not updated:
        return {"ok": False, "reason": "payload_update_failed"}

    return {
        "ok": True,
        "reason": "draft_update_applied",
        "job_id": normalized_job_id,
        "title": updated_payload["title"],
        "content_length": len(resolved_content),
    }


def start_draft_revision_session(
    job_store: Any,
    *,
    chat_id: str | int,
    job_id: str,
    approval_id: str,
    token: str,
    timeout_minutes: int = DEFAULT_REVISION_TIMEOUT_MINUTES,
) -> Dict[str, Any]:
    """다음 텔레그램 텍스트를 해당 초안의 수정본으로 받도록 대기 상태를 저장한다."""
    normalized_chat_id = str(chat_id or "").strip()
    normalized_job_id = str(job_id or "").strip()
    if not normalized_chat_id:
        return {"ok": False, "reason": "chat_id_missing"}
    if not normalized_job_id:
        return {"ok": False, "reason": "job_id_missing"}

    job = job_store.get_job(normalized_job_id)
    if not job:
        return {"ok": False, "reason": "job_not_found"}
    awaiting_status = getattr(job_store, "STATUS_AWAITING_APPROVAL", STATUS_AWAITING_APPROVAL)
    if str(job.status) != str(awaiting_status):
        return {
            "ok": False,
            "reason": "invalid_job_status",
            "current_status": str(job.status),
        }

    created_at_dt = _now_utc()
    expires_at_dt = created_at_dt + timedelta(minutes=max(1, int(timeout_minutes)))
    record = {
        "chat_id": normalized_chat_id,
        "job_id": normalized_job_id,
        "approval_id": str(approval_id or "").strip(),
        "token": str(token or "").strip(),
        "title": str(job.title or ""),
        "status": "waiting",
        "created_at": _format_utc(created_at_dt),
        "expires_at": _format_utc(expires_at_dt),
    }
    job_store.set_system_setting(_revision_session_key(normalized_chat_id), json.dumps(record, ensure_ascii=False))
    return {
        "ok": True,
        "reason": "revision_session_started",
        "job_id": normalized_job_id,
        "expires_at": record["expires_at"],
        "timeout_minutes": max(1, int(timeout_minutes)),
    }


def _load_revision_session(job_store: Any, chat_id: str | int) -> Dict[str, Any]:
    """채팅방의 수정본 입력 대기 상태를 읽는다."""
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_chat_id:
        return {}
    raw_record = str(job_store.get_system_setting(_revision_session_key(normalized_chat_id), "") or "").strip()
    if not raw_record:
        return {}
    try:
        record = json.loads(raw_record)
    except Exception:
        return {}
    return record if isinstance(record, dict) else {}


def clear_draft_revision_session(job_store: Any, chat_id: str | int) -> None:
    """채팅방의 수정본 입력 대기 상태를 비운다."""
    normalized_chat_id = str(chat_id or "").strip()
    if normalized_chat_id:
        job_store.set_system_setting(_revision_session_key(normalized_chat_id), "")


def _mark_approval_record_status(
    job_store: Any,
    *,
    approval_id: str,
    status: str,
) -> None:
    """승인 요청 레코드 상태를 갱신한다."""
    normalized_id = str(approval_id or "").strip()
    if not normalized_id:
        return
    raw_record = str(job_store.get_system_setting(_approval_key(normalized_id), "") or "").strip()
    if not raw_record:
        return
    try:
        record = json.loads(raw_record)
    except Exception:
        return
    if not isinstance(record, dict):
        return
    record["status"] = str(status or "").strip() or "handled"
    record["handled_at"] = _format_utc(_now_utc())
    job_store.set_system_setting(_approval_key(normalized_id), json.dumps(record, ensure_ascii=False))


def expire_pending_draft_revision_sessions(
    job_store: Any,
    *,
    chat_id: str | int | None = None,
    now_override: Optional[datetime] = None,
) -> int:
    """수정본 입력 제한시간이 지난 초안을 취소한다."""
    now_dt = now_override or _now_utc()
    records: list[tuple[str, Dict[str, Any]]] = []

    if chat_id is not None:
        key = _revision_session_key(str(chat_id))
        session = _load_revision_session(job_store, str(chat_id))
        if session:
            records.append((key, session))
    else:
        try:
            settings = job_store.get_system_settings()
        except Exception:
            settings = {}
        prefix = "telegram_draft_revision_session:"
        for key, raw_value in settings.items():
            if not str(key).startswith(prefix):
                continue
            try:
                session = json.loads(str(raw_value or "").strip() or "{}")
            except Exception:
                session = {}
            if isinstance(session, dict) and session:
                records.append((str(key), session))

    expired_count = 0
    awaiting_status = getattr(job_store, "STATUS_AWAITING_APPROVAL", STATUS_AWAITING_APPROVAL)
    for key, session in records:
        expires_at_dt = _parse_utc_datetime(str(session.get("expires_at", "")))
        if not expires_at_dt or expires_at_dt > now_dt:
            continue
        job_id = str(session.get("job_id", "")).strip()
        if job_id:
            job = job_store.get_job(job_id)
            if job and str(job.status) == str(awaiting_status):
                job_store.update_job_status(job_id, job_store.STATUS_CANCELLED)
                expired_count += 1
        _mark_approval_record_status(
            job_store,
            approval_id=str(session.get("approval_id", "")),
            status="revision_timeout",
        )
        job_store.set_system_setting(key, "")
    return expired_count


def apply_pending_draft_revision(
    job_store: Any,
    *,
    chat_id: str | int,
    revised_text: str,
    min_content_chars: int = 80,
) -> Dict[str, Any]:
    """수정본 입력 대기 상태에서 다음 텍스트를 초안에 반영한다."""
    session = _load_revision_session(job_store, chat_id)
    if not session:
        return {"handled": False, "reason": "revision_session_not_found"}

    expires_at_dt = _parse_utc_datetime(str(session.get("expires_at", "")))
    if not expires_at_dt or expires_at_dt <= _now_utc():
        expire_pending_draft_revision_sessions(job_store, chat_id=chat_id)
        return {
            "handled": True,
            "ok": False,
            "reason": "revision_timeout",
            "job_id": str(session.get("job_id", "")).strip(),
        }

    result = apply_draft_manual_revision(
        job_store,
        job_id=str(session.get("job_id", "")),
        revised_text=revised_text,
        min_content_chars=min_content_chars,
    )
    result["handled"] = True
    if bool(result.get("ok")):
        clear_draft_revision_session(job_store, chat_id)
        payload = job_store.load_prepared_payload(str(session.get("job_id", "")))
        result["payload"] = payload
        result["approval_id"] = str(session.get("approval_id", "")).strip()
        result["token"] = str(session.get("token", "")).strip()
        result["expires_at"] = str(session.get("expires_at", "")).strip()
    return result


def apply_draft_callback_action(
    job_store: Any,
    *,
    approval_id: str,
    token: str,
    action: str,
) -> Dict[str, Any]:
    """초안 승인 콜백을 검증하고 잡 상태를 갱신한다."""
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"approve", "cancel", "revise"}:
        return {"ok": False, "reason": "invalid_action"}

    raw_record = str(job_store.get_system_setting(_approval_key(approval_id), "") or "").strip()
    if not raw_record:
        return {"ok": False, "reason": "approval_not_found"}
    try:
        record = json.loads(raw_record)
    except Exception:
        return {"ok": False, "reason": "invalid_record"}
    if not isinstance(record, dict):
        return {"ok": False, "reason": "invalid_record"}

    if str(record.get("token", "")).strip() != str(token or "").strip():
        return {"ok": False, "reason": "invalid_token"}
    if str(record.get("status", "")).strip() != "pending":
        return {"ok": False, "reason": "already_handled"}

    expires_at = str(record.get("expires_at", "")).strip()
    expires_at_dt = _parse_utc_datetime(expires_at) or (_now_utc() - timedelta(seconds=1))
    if expires_at_dt <= _now_utc():
        record["status"] = "expired"
        record["handled_at"] = _format_utc(_now_utc())
        job_store.set_system_setting(_approval_key(approval_id), json.dumps(record, ensure_ascii=False))
        return {"ok": False, "reason": "token_expired"}

    job_id = str(record.get("job_id", "")).strip()
    if not job_id:
        return {"ok": False, "reason": "job_not_found"}
    job = job_store.get_job(job_id)
    if not job:
        return {"ok": False, "reason": "job_not_found"}

    awaiting_status = getattr(job_store, "STATUS_AWAITING_APPROVAL", STATUS_AWAITING_APPROVAL)
    if str(job.status) != str(awaiting_status):
        return {
            "ok": False,
            "reason": "invalid_job_status",
            "current_status": str(job.status),
        }

    if normalized_action == "revise":
        record["last_revision_requested_at"] = _format_utc(_now_utc())
        job_store.set_system_setting(_approval_key(approval_id), json.dumps(record, ensure_ascii=False))
        return {
            "ok": True,
            "reason": "draft_revision_requested",
            "action": normalized_action,
            "job_id": job_id,
            "status": awaiting_status,
        }

    if normalized_action == "approve":
        payload = job_store.load_prepared_payload(job_id)
        if not payload:
            return {"ok": False, "reason": "missing_payload"}
        updated = job_store.update_job_status(job_id, job_store.STATUS_READY)
        target_status = job_store.STATUS_READY
    else:
        updated = job_store.update_job_status(job_id, job_store.STATUS_CANCELLED)
        job_store.clear_prepared_payload(job_id)
        target_status = job_store.STATUS_CANCELLED

    if not updated:
        return {"ok": False, "reason": "status_update_failed"}

    record["status"] = "approved" if normalized_action == "approve" else "cancelled"
    record["handled_at"] = _format_utc(_now_utc())
    job_store.set_system_setting(_approval_key(approval_id), json.dumps(record, ensure_ascii=False))
    return {
        "ok": True,
        "reason": "draft_callback_applied",
        "action": normalized_action,
        "job_id": job_id,
        "status": target_status,
    }
