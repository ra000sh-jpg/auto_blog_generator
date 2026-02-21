"""AI 토글 검증 리포트 조회 API."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class AIToggleSummary(BaseModel):
    """AI 토글 요약 정보."""

    expected_on: int = 0
    verified_on: int = 0
    repaired: int = 0
    failed: int = 0
    passed: int = 0


class AIToggleReportResponse(BaseModel):
    """AI 토글 리포트 응답."""

    available: bool
    mode: str = "metadata"
    post_url: str = ""
    created_at: int = 0
    created_at_iso: str = ""
    expected_on: int = 0
    actual_on: int = 0
    post_verify_passed: int = 0
    unresolved_images: List[str] = []
    recent_failure_streak: int = 0
    prepublish: AIToggleSummary = AIToggleSummary()
    postverify: AIToggleSummary = AIToggleSummary()


def _to_int(value: Any, default: int = 0) -> int:
    """숫자형을 안전하게 정수로 변환한다."""
    try:
        return int(value)
    except Exception:
        return int(default)


def _report_dir() -> Path:
    """리포트 디렉터리 경로를 반환한다."""
    raw = str(os.getenv("NAVER_AI_TOGGLE_REPORT_DIR", "data/ai_toggle")).strip()
    return Path(raw or "data/ai_toggle")


def _load_json(path: Path) -> Dict[str, Any]:
    """JSON 파일을 안전하게 로드한다."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_iso(created_at: int) -> str:
    """epoch seconds를 ISO 문자열로 변환한다."""
    if created_at <= 0:
        return ""
    try:
        return datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _count_recent_failure_streak(report_dir: Path, max_scan: int = 20) -> int:
    """최근 report 히스토리 기준 pre/post 실패 연속 횟수를 계산한다."""
    if not report_dir.exists():
        return 0
    history = [path for path in report_dir.glob("report_*.json") if path.is_file()]
    history.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    streak = 0
    for report_path in history[: max(1, int(max_scan))]:
        payload = _load_json(report_path)
        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        if not isinstance(summary, dict):
            break
        prepublish = summary.get("prepublish", {})
        postverify = summary.get("postverify", {})
        pre_map: Dict[str, Any] = prepublish if isinstance(prepublish, dict) else {}
        post_map: Dict[str, Any] = postverify if isinstance(postverify, dict) else {}
        pre_failed = _to_int(pre_map.get("failed", 0))
        post_failed = _to_int(post_map.get("failed", 0))
        if pre_failed > 0 or post_failed > 0:
            streak += 1
            continue
        break
    return streak


def _extract_unresolved_images(rows: List[Dict[str, Any]]) -> List[str]:
    """해결되지 않은 이미지 경로 목록을 추출한다."""
    unresolved: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        expected_on = bool(row.get("expected_on"))
        post_on = row.get("post_verify_on")
        actual_on = row.get("actual_on")
        if expected_on and post_on is not True and actual_on is not True:
            image_path = str(row.get("image_path", "")).strip()
            if image_path and image_path not in unresolved:
                unresolved.append(image_path)
    return unresolved


@router.get(
    "/ai-toggle/report",
    response_model=AIToggleReportResponse,
    summary="최근 AI 토글 검증 리포트 조회",
)
def get_ai_toggle_report() -> AIToggleReportResponse:
    """최근 AI 토글 검증 리포트를 반환한다."""
    report_dir = _report_dir()
    last_report = report_dir / "last_report.json"
    if not last_report.exists():
        return AIToggleReportResponse(available=False)

    payload = _load_json(last_report)
    if not payload:
        return AIToggleReportResponse(available=False)

    rows = payload.get("rows", [])
    row_list: List[Dict[str, Any]] = rows if isinstance(rows, list) else []
    summary = payload.get("summary", {})
    summary_map: Dict[str, Any] = summary if isinstance(summary, dict) else {}
    prepublish = summary_map.get("prepublish", {})
    postverify = summary_map.get("postverify", {})
    pre_map: Dict[str, Any] = prepublish if isinstance(prepublish, dict) else {}
    post_map: Dict[str, Any] = postverify if isinstance(postverify, dict) else {}
    created_at = _to_int(payload.get("created_at", 0))

    return AIToggleReportResponse(
        available=True,
        mode=str(payload.get("mode", "metadata")),
        post_url=str(payload.get("post_url", "")),
        created_at=created_at,
        created_at_iso=_build_iso(created_at),
        expected_on=_to_int(payload.get("expected_on", 0)),
        actual_on=_to_int(payload.get("actual_on", 0)),
        post_verify_passed=_to_int(payload.get("post_verify_passed", 0)),
        unresolved_images=_extract_unresolved_images(row_list),
        recent_failure_streak=_count_recent_failure_streak(report_dir),
        prepublish=AIToggleSummary(
            expected_on=_to_int(pre_map.get("expected_on", 0)),
            verified_on=_to_int(pre_map.get("verified_on", 0)),
            repaired=_to_int(pre_map.get("repaired", 0)),
            failed=_to_int(pre_map.get("failed", 0)),
        ),
        postverify=AIToggleSummary(
            expected_on=_to_int(post_map.get("expected_on", 0)),
            passed=_to_int(post_map.get("passed", 0)),
            failed=_to_int(post_map.get("failed", 0)),
        ),
    )
