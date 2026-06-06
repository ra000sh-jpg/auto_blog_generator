"""운영 점검과 백업 인덱스 API."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier
from modules.llm.llm_router import LLMRouter
from server.dependencies import get_job_store, get_llm_router

router = APIRouter()

_KST = ZoneInfo("Asia/Seoul")
_NAVER_STATE_PATH = Path("data/sessions/naver/state.json")
_FRONTEND_BUILD_ID_PATH = Path("frontend/.next/BUILD_ID")


class OpsCheckItem(BaseModel):
    """운영 점검 항목."""

    key: str
    label: str
    ok: bool
    detail: str = ""


class OpsCheckResponse(BaseModel):
    """운영 점검 응답."""

    ok: bool
    checked_at: str
    monthly_cost_krw: int = 0
    monthly_cost_warning_threshold_krw: int = 4200
    warnings: List[str] = Field(default_factory=list)
    notified: bool = False
    checks: List[OpsCheckItem]


class PostArchiveItem(BaseModel):
    """글 텍스트 백업 인덱스 항목."""

    job_id: str
    title: str
    slot: str = ""
    category: str = ""
    source_type: str = ""
    review_status: str = ""
    result_url: str = ""
    quality_score: float = 0.0
    insight_score: float = 0.0
    manual_revision_applied: bool = False
    content_length: int = 0
    image_count: int = 0
    table_count: int = 0
    image_items: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    final_content_preview: str = ""
    final_content: str = ""
    created_at: str = ""
    updated_at: str = ""


class PostArchiveListResponse(BaseModel):
    """글 텍스트 백업 인덱스 목록 응답."""

    items: List[PostArchiveItem]


def _today_key() -> str:
    """오늘 날짜를 KST 기준 문자열로 반환한다."""
    return datetime.now(tz=_KST).strftime("%Y-%m-%d")


def _read_text_file(path: Path) -> str:
    """존재하는 텍스트 파일의 내용을 짧게 읽는다."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _safe_json_loads(raw_value: Any, fallback: Any) -> Any:
    """JSON 문자열을 안전하게 파싱한다."""
    if isinstance(raw_value, (dict, list)):
        return raw_value
    try:
        parsed = json.loads(str(raw_value or ""))
    except Exception:
        return fallback
    return parsed if parsed is not None else fallback


def _monthly_cost_warning_threshold() -> int:
    """월 비용 경고 기준을 원화로 계산한다."""
    raw_value = os.getenv("AUTOBLOG_MONTHLY_COST_WARNING_KRW", "4200").strip()
    try:
        return max(0, int(float(raw_value)))
    except (TypeError, ValueError):
        return 4200


def _extract_monthly_cost(llm_router: LLMRouter) -> int:
    """라우터 견적에서 월 예상 비용을 추출한다."""
    try:
        payload = llm_router.export_for_ui()
        quote = payload.get("quote", {}) if isinstance(payload, dict) else {}
        return int(round(float(quote.get("monthly_cost_krw", 0) or 0)))
    except Exception:
        return 0


def _archive_to_item(row: Dict[str, Any]) -> PostArchiveItem:
    """DB row를 백업 인덱스 응답 항목으로 변환한다."""
    tags = _safe_json_loads(row.get("tags_json"), [])
    if not isinstance(tags, list):
        tags = []

    image_manifest = _safe_json_loads(row.get("image_manifest_json"), {})
    image_items: List[str] = []
    if isinstance(image_manifest, dict):
        for value in image_manifest.values():
            if isinstance(value, str) and value.strip():
                image_items.append(value.strip())
            elif isinstance(value, list):
                image_items.extend(str(item).strip() for item in value if str(item).strip())

    final_content = str(row.get("final_content") or "")
    slot = ""
    for tag in tags:
        tag_text = str(tag)
        if tag_text.startswith("market_slot:"):
            slot = tag_text.split(":", 1)[1]
            break

    table_lines = [
        line for line in final_content.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]
    image_count = max(final_content.count("!["), len(image_items))
    table_count = max(0, len(table_lines) // 3)
    preview = final_content.strip().replace("\r", "")
    if len(preview) > 360:
        preview = f"{preview[:360].rstrip()}..."

    return PostArchiveItem(
        job_id=str(row.get("job_id") or ""),
        title=str(row.get("title") or ""),
        slot=slot,
        category=str(row.get("category") or ""),
        source_type=str(row.get("source_type") or ""),
        review_status=str(row.get("review_status") or ""),
        result_url=str(row.get("result_url") or ""),
        quality_score=float(row.get("quality_score") or 0.0),
        insight_score=float(row.get("insight_score") or 0.0),
        manual_revision_applied=bool(int(row.get("manual_revision_applied") or 0)),
        content_length=len(final_content),
        image_count=image_count,
        table_count=table_count,
        image_items=image_items[:8],
        tags=[str(tag) for tag in tags],
        final_content_preview=preview,
        final_content=final_content,
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


@router.get(
    "/ops/check",
    response_model=OpsCheckResponse,
    summary="오늘 운영 점검",
)
async def check_ops(
    notify: bool = Query(default=False, description="비용 경고를 텔레그램으로 보낼지 여부"),
    job_store: JobStore = Depends(get_job_store),
    llm_router: LLMRouter = Depends(get_llm_router),
) -> OpsCheckResponse:
    """운영자가 하루 시작 전에 확인할 핵심 상태를 한 번에 반환한다."""
    checks: List[OpsCheckItem] = []
    warnings: List[str] = []

    try:
        queue_stats = job_store.get_queue_stats()
        queued = int(queue_stats.get("queued", 0) or 0)
        running = int(queue_stats.get("running", 0) or 0)
        ready = int(queue_stats.get("ready_to_publish", 0) or 0)
        checks.append(
            OpsCheckItem(
                key="database",
                label="DB",
                ok=True,
                detail=f"queued {queued}, running {running}, ready {ready}",
            )
        )
    except Exception as exc:
        checks.append(OpsCheckItem(key="database", label="DB", ok=False, detail=str(exc)[:160]))

    notifier = TelegramNotifier.from_env(db_path=job_store.db_path)
    checks.append(
        OpsCheckItem(
            key="telegram",
            label="텔레그램",
            ok=notifier.enabled,
            detail="토큰과 chat_id 확인됨" if notifier.enabled else "토큰 또는 chat_id가 비어 있습니다",
        )
    )

    naver_connected = _NAVER_STATE_PATH.exists()
    naver_detail = str(_NAVER_STATE_PATH)
    if naver_connected:
        naver_detail = f"{naver_detail} / updated {int(_NAVER_STATE_PATH.stat().st_mtime)}"
    checks.append(
        OpsCheckItem(
            key="naver",
            label="네이버 세션",
            ok=naver_connected,
            detail=naver_detail,
        )
    )

    build_id = _read_text_file(_FRONTEND_BUILD_ID_PATH)
    checks.append(
        OpsCheckItem(
            key="frontend_build",
            label="프론트 빌드",
            ok=bool(build_id),
            detail=build_id[:24] if build_id else "frontend/.next/BUILD_ID 없음",
        )
    )

    monthly_cost = _extract_monthly_cost(llm_router)
    threshold = _monthly_cost_warning_threshold()
    if threshold > 0 and monthly_cost >= threshold:
        warnings.append(f"월 예상 비용 {monthly_cost:,}원이 경고 기준 {threshold:,}원 이상입니다.")

    notified = False
    if notify and warnings and notifier.enabled:
        today_key = _today_key()
        setting_key = "ops_last_monthly_cost_warning_date"
        if job_store.get_system_setting(setting_key, "") != today_key:
            notified = await notifier.send_message(
                "\n".join(
                    [
                        "AutoBlog 월 비용 경고",
                        f"- 예상: {monthly_cost:,}원",
                        f"- 기준: {threshold:,}원",
                        "- 설정 > AI·이미지 라우터에서 이미지/VLM/모델 비용을 확인하세요.",
                    ]
                )
            )
            if notified:
                job_store.set_system_setting(setting_key, today_key)

    checks.append(
        OpsCheckItem(
            key="monthly_cost",
            label="월 비용",
            ok=not warnings,
            detail=f"{monthly_cost:,}원 / 기준 {threshold:,}원",
        )
    )

    ok = all(item.ok for item in checks)
    return OpsCheckResponse(
        ok=ok,
        checked_at=datetime.now(tz=_KST).isoformat(),
        monthly_cost_krw=monthly_cost,
        monthly_cost_warning_threshold_krw=threshold,
        warnings=warnings,
        notified=notified,
        checks=checks,
    )


@router.get(
    "/ops/backups",
    response_model=PostArchiveListResponse,
    summary="글 텍스트 백업 인덱스",
)
def list_post_backups(
    limit: int = Query(default=20, ge=1, le=100),
    job_store: JobStore = Depends(get_job_store),
) -> PostArchiveListResponse:
    """최근 보존된 글 텍스트 인덱스를 반환한다."""
    rows = job_store.list_post_text_archives(limit=limit)
    return PostArchiveListResponse(items=[_archive_to_item(row) for row in rows])


@router.get(
    "/ops/revisions",
    response_model=PostArchiveListResponse,
    summary="수정본 입력 반영 기록",
)
def list_manual_revision_archives(
    limit: int = Query(default=10, ge=1, le=100),
    job_store: JobStore = Depends(get_job_store),
) -> PostArchiveListResponse:
    """스마트폰 수정본입력이 반영된 글만 반환한다."""
    rows = job_store.list_post_text_archives(limit=limit, manual_revision_only=True)
    return PostArchiveListResponse(items=[_archive_to_item(row) for row in rows])
