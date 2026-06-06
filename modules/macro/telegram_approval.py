"""매크로 글 후보 텔레그램 승인 헬퍼."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .job_promoter import MacroCandidatePromoter


CALLBACK_PREFIX = "mac:v1:"
ACTION_TO_CODE = {"promote": "p"}
CODE_TO_ACTION = {value: key for key, value in ACTION_TO_CODE.items()}
DEFAULT_MAX_BUTTONS = 5


def build_callback_data(*, action: str, candidate_id: str) -> str:
    """텔레그램 inline button callback_data를 만든다."""
    action_code = ACTION_TO_CODE.get(str(action or "").strip().lower())
    normalized_candidate_id = str(candidate_id or "").strip()
    if not action_code:
        raise ValueError(f"Unsupported macro candidate action: {action}")
    if not normalized_candidate_id:
        raise ValueError("candidate_id is required")
    return f"{CALLBACK_PREFIX}{action_code}:{normalized_candidate_id}"


def is_macro_callback_data(raw_data: str) -> bool:
    """매크로 후보 콜백인지 확인한다."""
    return str(raw_data or "").strip().startswith(CALLBACK_PREFIX)


def parse_macro_callback_data(raw_data: str) -> Optional[Dict[str, str]]:
    """매크로 후보 callback_data를 파싱한다."""
    normalized = str(raw_data or "").strip()
    if not normalized.startswith(CALLBACK_PREFIX):
        return None
    parts = normalized.split(":", 3)
    if len(parts) != 4:
        return None
    _, version, action_code, candidate_id = parts
    if version != "v1":
        return None
    action = CODE_TO_ACTION.get(action_code)
    if not action or not candidate_id:
        return None
    return {
        "action": action,
        "candidate_id": candidate_id,
    }


def build_macro_candidate_keyboard(
    candidates: Iterable[Dict[str, Any]],
    *,
    max_buttons: int = DEFAULT_MAX_BUTTONS,
) -> Dict[str, Any]:
    """매크로 후보 선택용 inline keyboard를 만든다."""
    rows: List[List[Dict[str, str]]] = []
    safe_limit = max(1, min(DEFAULT_MAX_BUTTONS, int(max_buttons or DEFAULT_MAX_BUTTONS)))
    for index, candidate in enumerate(list(candidates)[:safe_limit], start=1):
        candidate_id = str(candidate.get("id", "") or "").strip()
        if not candidate_id:
            continue
        callback_data = build_callback_data(
            action="promote",
            candidate_id=candidate_id,
        )
        if len(callback_data.encode("utf-8")) > 64:
            continue
        rows.append(
            [
                {
                    "text": f"{index}번 초안생성",
                    "callback_data": callback_data,
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else {}


def apply_macro_candidate_callback(
    job_store: Any,
    *,
    candidate_id: str,
    action: str,
) -> Dict[str, Any]:
    """텔레그램 후보 선택을 블로그 생성 큐 등록으로 반영한다."""
    normalized_candidate_id = str(candidate_id or "").strip()
    normalized_action = str(action or "").strip().lower()
    if normalized_action != "promote":
        return {
            "ok": False,
            "reason": "unsupported_action",
            "action": normalized_action,
        }
    if not normalized_candidate_id:
        return {
            "ok": False,
            "reason": "candidate_id_missing",
            "action": normalized_action,
        }

    candidate = job_store.get_macro_blog_candidate(normalized_candidate_id)
    if not candidate:
        return {
            "ok": False,
            "reason": "candidate_not_found",
            "action": normalized_action,
        }

    status = str(candidate.get("status", "") or "").strip().lower()
    if status == "approved":
        return {
            "ok": False,
            "reason": "already_handled",
            "action": normalized_action,
            "candidate_id": normalized_candidate_id,
        }
    if status and status not in {"draft", "needs_review"}:
        return {
            "ok": False,
            "reason": "invalid_candidate_status",
            "action": normalized_action,
            "candidate_id": normalized_candidate_id,
            "current_status": status,
        }

    try:
        result = MacroCandidatePromoter(job_store=job_store).promote_candidate(
            normalized_candidate_id,
            status="queued",
        )
    except ValueError as exc:
        error_message = str(exc)
        reason = "candidate_not_found" if "not found" in error_message.lower() else "invalid_candidate"
        return {
            "ok": False,
            "reason": reason,
            "action": normalized_action,
            "error": error_message,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "promotion_failed",
            "action": normalized_action,
            "error": str(exc),
        }

    return {
        "ok": True,
        "reason": "macro_candidate_promoted",
        "action": normalized_action,
        **result,
    }
