"""멀티채널 관리 및 서브잡 배포 API."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from modules.automation.job_store import JobStore
from modules.automation.time_utils import parse_iso
from modules.uploaders.publisher_factory import get_publisher, get_supported_platforms
from server.dependencies import get_job_store

router = APIRouter()

_SUPPORTED_PLATFORMS = set(get_supported_platforms())
_IMPLEMENTED_SUB_PLATFORMS = set(get_supported_platforms())
_MULTICHANNEL_SETTING_KEY = "multichannel_enabled"
_CHANNEL_TEST_TIMEOUT_SEC = 10.0


class ChannelResponse(BaseModel):
    channel_id: str
    platform: str
    label: str
    blog_url: str
    persona_id: str
    persona_desc: str
    daily_target: int
    style_level: int
    style_model: str
    publish_delay_minutes: int
    is_master: bool
    auth_json: str
    active: bool
    created_at: str
    updated_at: str


class ChannelListResponse(BaseModel):
    items: List[ChannelResponse]


class CreateChannelRequest(BaseModel):
    platform: str
    label: str
    blog_url: str
    persona_id: str = "P1"
    persona_desc: str = ""
    daily_target: int = 0
    style_level: int = 2
    style_model: str = ""
    publish_delay_minutes: int = 90
    is_master: bool = False
    auth_json: Dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class UpdateChannelRequest(BaseModel):
    platform: Optional[str] = None
    label: Optional[str] = None
    blog_url: Optional[str] = None
    persona_id: Optional[str] = None
    persona_desc: Optional[str] = None
    daily_target: Optional[int] = None
    style_level: Optional[int] = None
    style_model: Optional[str] = None
    publish_delay_minutes: Optional[int] = None
    is_master: Optional[bool] = None
    auth_json: Optional[Dict[str, Any]] = None
    active: Optional[bool] = None


class DeleteChannelResponse(BaseModel):
    ok: bool
    message: str
    cancelled_jobs: int


class ChannelTestResponse(BaseModel):
    success: bool
    message: str
    reason_code: Optional[str] = None


class DistributeDetailItem(BaseModel):
    channel_id: str
    channel_label: str
    action: str
    sub_job_id: Optional[str] = None
    reason: Optional[str] = None


class DistributeResponse(BaseModel):
    master_job_id: str
    created: int
    skipped: int
    failed: int
    details: List[DistributeDetailItem]


class ChannelSettingsResponse(BaseModel):
    multichannel_enabled: bool


def _mask_auth_json(raw_value: str) -> str:
    """민감정보 노출 방지를 위해 auth_json을 마스킹한다."""
    text = str(raw_value or "").strip()
    if not text or text == "{}":
        return "{}"
    return "***"


def _serialize_channel_for_response(channel: Dict[str, Any]) -> ChannelResponse:
    payload = dict(channel)
    payload["auth_json"] = _mask_auth_json(str(channel.get("auth_json", "{}")))
    return ChannelResponse(**payload)


def _normalize_platform(value: str) -> str:
    platform = str(value or "").strip().lower()
    if platform not in _SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"지원하지 않는 플랫폼입니다: {platform}",
        )
    return platform


def _parse_channel_auth_json(channel: Dict[str, Any]) -> Dict[str, Any]:
    """채널 auth_json 문자열을 dict로 파싱한다."""
    raw_auth = str(channel.get("auth_json", "{}") or "{}").strip() or "{}"
    try:
        parsed = json.loads(raw_auth)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _build_channel_test_response(success: bool, message: str, reason_code: Optional[str] = None) -> ChannelTestResponse:
    """채널 연동 테스트 응답을 일관된 포맷으로 생성한다."""
    normalized_code = str(reason_code or "").strip()
    if not success and normalized_code:
        normalized_message = f"{message} (code={normalized_code})"
        return ChannelTestResponse(success=False, message=normalized_message, reason_code=normalized_code)
    return ChannelTestResponse(success=success, message=message, reason_code=normalized_code or None)


def _infer_connection_failure_reason(channel: Dict[str, Any]) -> str:
    """플랫폼별 채널 연동 실패 원인 코드를 추론한다."""
    platform = str(channel.get("platform", "")).strip().lower()
    auth = _parse_channel_auth_json(channel)

    if platform == "naver":
        session_dir = str(auth.get("session_dir", "")).strip()
        if not session_dir:
            return "missing_session_dir"
        state_path = Path(session_dir) / "state.json"
        if not state_path.exists():
            return "missing_state_file"
        return "naver_connection_failed"

    if platform == "tistory":
        access_token = str(auth.get("access_token", "")).strip()
        blog_name = str(auth.get("blog_name", "")).strip()
        if not blog_name:
            raw_url = str(channel.get("blog_url", "")).strip()
            if raw_url and "://" not in raw_url:
                raw_url = f"https://{raw_url}"
            hostname = str(urlparse(raw_url).hostname or "").strip().lower()
            if hostname.endswith(".tistory.com"):
                blog_name = hostname.replace(".tistory.com", "").strip()
        if not access_token and not blog_name:
            return "missing_access_token_and_blog_name"
        if not access_token:
            return "missing_access_token"
        if not blog_name:
            return "missing_blog_name"
        return "tistory_auth_invalid"

    if platform == "wordpress":
        return "publisher_not_implemented"
    return "unsupported_platform"


def _is_multichannel_enabled(job_store: JobStore) -> bool:
    raw = str(job_store.get_system_setting(_MULTICHANNEL_SETTING_KEY, "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _set_multichannel_enabled(job_store: JobStore, enabled: bool) -> None:
    job_store.set_system_setting(_MULTICHANNEL_SETTING_KEY, "true" if enabled else "false")


def _validate_master_invariant_on_create(request: CreateChannelRequest, job_store: JobStore) -> None:
    """활성 채널의 마스터 1개 불변식을 생성 시점에 검증한다."""
    has_active_channels = job_store.has_any_active_channel()
    has_active_master = job_store.has_active_master_channel()
    if request.active and not has_active_channels and not request.is_master:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="첫 활성 채널은 반드시 마스터(is_master=true)여야 합니다.",
        )
    if request.active and request.is_master and has_active_master:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="활성 마스터 채널은 1개만 허용됩니다.",
        )
    if request.active and not request.is_master and not has_active_master:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="활성 상태의 마스터 채널이 필요합니다.",
        )


def _validate_master_invariant_on_update(
    existing: Dict[str, Any],
    request: UpdateChannelRequest,
    job_store: JobStore,
) -> None:
    """활성 채널의 마스터 1개 불변식을 수정 시점에 검증한다."""
    next_active = existing["active"] if request.active is None else bool(request.active)
    next_is_master = existing["is_master"] if request.is_master is None else bool(request.is_master)
    channel_id = str(existing["channel_id"])

    if next_active and next_is_master and job_store.has_active_master_channel(exclude_channel_id=channel_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="활성 마스터 채널은 1개만 허용됩니다.",
        )

    if (
        existing["active"]
        and existing["is_master"]
        and not next_active
        and not job_store.has_active_master_channel(exclude_channel_id=channel_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="활성 마스터 채널은 최소 1개 필요합니다.",
        )

    if next_active and not next_is_master and not job_store.has_active_master_channel(exclude_channel_id=channel_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="활성 상태의 마스터 채널이 최소 1개 필요합니다.",
        )


@router.get("/channels", response_model=ChannelListResponse, summary="채널 목록 조회")
def list_channels(
    include_inactive: bool = False,
    job_store: JobStore = Depends(get_job_store),
) -> ChannelListResponse:
    channels = job_store.list_channels(include_inactive=include_inactive)
    return ChannelListResponse(items=[_serialize_channel_for_response(item) for item in channels])


@router.get("/channels/settings", response_model=ChannelSettingsResponse, summary="멀티채널 설정 조회")
def get_channel_settings(
    job_store: JobStore = Depends(get_job_store),
) -> ChannelSettingsResponse:
    return ChannelSettingsResponse(multichannel_enabled=_is_multichannel_enabled(job_store))


@router.post("/channels/settings", response_model=ChannelSettingsResponse, summary="멀티채널 설정 저장")
def set_channel_settings(
    payload: ChannelSettingsResponse,
    job_store: JobStore = Depends(get_job_store),
) -> ChannelSettingsResponse:
    _set_multichannel_enabled(job_store, payload.multichannel_enabled)
    return ChannelSettingsResponse(multichannel_enabled=payload.multichannel_enabled)


@router.post("/channels", response_model=ChannelResponse, summary="채널 생성")
def create_channel(
    request: CreateChannelRequest,
    job_store: JobStore = Depends(get_job_store),
) -> ChannelResponse:
    platform = _normalize_platform(request.platform)
    label = str(request.label or "").strip()
    blog_url = str(request.blog_url or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="label은 비어 있을 수 없습니다.")
    if not blog_url:
        raise HTTPException(status_code=422, detail="blog_url은 비어 있을 수 없습니다.")

    _validate_master_invariant_on_create(request, job_store)

    channel_id = str(uuid.uuid4())
    payload = {
        "channel_id": channel_id,
        "platform": platform,
        "label": label,
        "blog_url": blog_url,
        "persona_id": str(request.persona_id or "P1").strip() or "P1",
        "persona_desc": str(request.persona_desc or "").strip(),
        "daily_target": max(0, int(request.daily_target)),
        "style_level": max(1, min(3, int(request.style_level))),
        "style_model": str(request.style_model or "").strip(),
        "publish_delay_minutes": max(0, int(request.publish_delay_minutes)),
        "is_master": bool(request.is_master),
        "auth_json": json.dumps(request.auth_json or {}, ensure_ascii=False),
        "active": bool(request.active),
    }

    try:
        ok = job_store.insert_channel(payload)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"채널 생성 실패: {exc}") from exc
    if not ok:
        raise HTTPException(status_code=409, detail="채널 생성에 실패했습니다.")

    created = job_store.get_channel(channel_id)
    if not created:
        raise HTTPException(status_code=500, detail="생성된 채널을 조회할 수 없습니다.")
    return _serialize_channel_for_response(created)


@router.put("/channels/{channel_id}", response_model=ChannelResponse, summary="채널 수정")
def update_channel(
    channel_id: str,
    request: UpdateChannelRequest,
    job_store: JobStore = Depends(get_job_store),
) -> ChannelResponse:
    existing = job_store.get_channel(channel_id)
    if not existing:
        raise HTTPException(status_code=404, detail="채널을 찾을 수 없습니다.")

    _validate_master_invariant_on_update(existing, request, job_store)

    updates: Dict[str, Any] = {}
    if request.platform is not None:
        updates["platform"] = _normalize_platform(request.platform)
    if request.label is not None:
        label = str(request.label).strip()
        if not label:
            raise HTTPException(status_code=422, detail="label은 비어 있을 수 없습니다.")
        updates["label"] = label
    if request.blog_url is not None:
        blog_url = str(request.blog_url).strip()
        if not blog_url:
            raise HTTPException(status_code=422, detail="blog_url은 비어 있을 수 없습니다.")
        updates["blog_url"] = blog_url
    if request.persona_id is not None:
        updates["persona_id"] = str(request.persona_id or "P1").strip() or "P1"
    if request.persona_desc is not None:
        updates["persona_desc"] = str(request.persona_desc or "").strip()
    if request.daily_target is not None:
        updates["daily_target"] = max(0, int(request.daily_target))
    if request.style_level is not None:
        updates["style_level"] = max(1, min(3, int(request.style_level)))
    if request.style_model is not None:
        updates["style_model"] = str(request.style_model or "").strip()
    if request.publish_delay_minutes is not None:
        updates["publish_delay_minutes"] = max(0, int(request.publish_delay_minutes))
    if request.is_master is not None:
        updates["is_master"] = bool(request.is_master)
    if request.auth_json is not None:
        updates["auth_json"] = json.dumps(request.auth_json or {}, ensure_ascii=False)
    if request.active is not None:
        updates["active"] = bool(request.active)

    if updates:
        ok = job_store.update_channel_fields(channel_id, updates)
        if not ok:
            raise HTTPException(status_code=409, detail="채널 수정에 실패했습니다.")

    updated = job_store.get_channel(channel_id)
    if not updated:
        raise HTTPException(status_code=500, detail="수정된 채널을 조회할 수 없습니다.")
    return _serialize_channel_for_response(updated)


@router.delete("/channels/{channel_id}", response_model=DeleteChannelResponse, summary="채널 비활성화")
def delete_channel(
    channel_id: str,
    job_store: JobStore = Depends(get_job_store),
) -> DeleteChannelResponse:
    existing = job_store.get_channel(channel_id)
    if not existing:
        raise HTTPException(status_code=404, detail="채널을 찾을 수 없습니다.")

    if existing["active"] and existing["is_master"] and not job_store.has_active_master_channel(exclude_channel_id=channel_id):
        raise HTTPException(
            status_code=409,
            detail="활성 마스터 채널은 최소 1개 필요하므로 삭제(비활성화)할 수 없습니다.",
        )

    result = job_store.deactivate_channel_and_cancel_jobs(channel_id)
    if result["updated_channels"] <= 0:
        raise HTTPException(status_code=409, detail="채널 비활성화에 실패했습니다.")
    return DeleteChannelResponse(
        ok=True,
        message="채널이 비활성화되었고, 대기 중 서브 잡을 취소했습니다.",
        cancelled_jobs=result["cancelled_jobs"],
    )


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResponse, summary="채널 연동 테스트")
def test_channel_connection(
    channel_id: str,
    job_store: JobStore = Depends(get_job_store),
) -> ChannelTestResponse:
    channel = job_store.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="채널을 찾을 수 없습니다.")

    try:
        publisher = get_publisher(channel)
    except NotImplementedError as exc:
        return _build_channel_test_response(
            success=False,
            message=str(exc),
            reason_code="publisher_not_implemented",
        )
    except Exception as exc:
        return _build_channel_test_response(
            success=False,
            message=f"연동 테스트 초기화 실패: {exc}",
            reason_code="publisher_init_failed",
        )

    try:
        is_ok = asyncio.run(
            asyncio.wait_for(
                publisher.test_connection(),
                timeout=_CHANNEL_TEST_TIMEOUT_SEC,
            )
        )
    except asyncio.TimeoutError:
        return _build_channel_test_response(
            success=False,
            message=f"연동 테스트가 {_CHANNEL_TEST_TIMEOUT_SEC:.0f}초 내에 완료되지 않았습니다.",
            reason_code="timeout",
        )
    except Exception as exc:
        return _build_channel_test_response(
            success=False,
            message=f"연동 테스트 실행 실패: {exc}",
            reason_code="test_execution_failed",
        )

    if is_ok:
        return _build_channel_test_response(
            success=True,
            message="채널 연동 테스트 성공",
        )
    return _build_channel_test_response(
        success=False,
        message="채널 연동 테스트 실패",
        reason_code=_infer_connection_failure_reason(channel),
    )


@router.post("/jobs/{job_id}/distribute", response_model=DistributeResponse, summary="마스터 잡 서브 배포")
def distribute_sub_jobs(
    job_id: str,
    job_store: JobStore = Depends(get_job_store),
) -> DistributeResponse:
    master_job = job_store.get_job(job_id)
    if not master_job:
        raise HTTPException(status_code=404, detail="마스터 작업을 찾을 수 없습니다.")
    if master_job.job_kind == job_store.JOB_KIND_SUB:
        raise HTTPException(status_code=409, detail="서브 잡은 distribute 대상이 아닙니다.")
    if master_job.status != job_store.STATUS_COMPLETED:
        raise HTTPException(
            status_code=409,
            detail="마스터 잡이 completed 상태일 때만 distribute 가능합니다. reason=master_not_completed",
        )
    if not _is_multichannel_enabled(job_store):
        raise HTTPException(
            status_code=409,
            detail="multichannel_enabled=false 상태입니다.",
        )

    base_time = parse_iso(master_job.completed_at or master_job.updated_at)
    channels = job_store.get_active_sub_channels()
    created = 0
    skipped = 0
    failed = 0
    details: List[DistributeDetailItem] = []

    for channel in channels:
        channel_id = str(channel["channel_id"])
        channel_label = str(channel["label"])
        platform = str(channel["platform"]).strip().lower()

        if platform not in _IMPLEMENTED_SUB_PLATFORMS:
            skipped += 1
            details.append(
                DistributeDetailItem(
                    channel_id=channel_id,
                    channel_label=channel_label,
                    action="skipped",
                    reason="publisher_not_implemented",
                )
            )
            continue

        existing = job_store.get_sub_job_by_master_channel(job_id, channel_id)
        if existing:
            skipped += 1
            details.append(
                DistributeDetailItem(
                    channel_id=channel_id,
                    channel_label=channel_label,
                    action="skipped",
                    sub_job_id=existing.job_id,
                    reason="already_exists",
                )
            )
            continue

        delay_minutes = max(0, int(channel.get("publish_delay_minutes", 90)))
        scheduled_at = (base_time + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sub_job_id = str(uuid.uuid4())
        sub_persona_id = str(channel.get("persona_id", "")).strip() or master_job.persona_id
        sub_title = f"[{channel_label}] {master_job.title}"
        success = job_store.schedule_job(
            job_id=sub_job_id,
            title=sub_title,
            seed_keywords=list(master_job.seed_keywords),
            platform=platform,
            persona_id=sub_persona_id,
            scheduled_at=scheduled_at,
            max_retries=max(1, int(master_job.max_retries)),
            tags=list(master_job.tags or []),
            category=str(master_job.category or ""),
            job_kind=job_store.JOB_KIND_SUB,
            master_job_id=job_id,
            channel_id=channel_id,
            status=job_store.STATUS_QUEUED,
        )

        if success:
            created += 1
            details.append(
                DistributeDetailItem(
                    channel_id=channel_id,
                    channel_label=channel_label,
                    action="created",
                    sub_job_id=sub_job_id,
                )
            )
            continue

        deduped = job_store.get_sub_job_by_master_channel(job_id, channel_id)
        if deduped:
            skipped += 1
            details.append(
                DistributeDetailItem(
                    channel_id=channel_id,
                    channel_label=channel_label,
                    action="skipped",
                    sub_job_id=deduped.job_id,
                    reason="already_exists",
                )
            )
        else:
            failed += 1
            details.append(
                DistributeDetailItem(
                    channel_id=channel_id,
                    channel_label=channel_label,
                    action="failed",
                    reason="insert_failed",
                )
            )

    return DistributeResponse(
        master_job_id=job_id,
        created=created,
        skipped=skipped,
        failed=failed,
        details=details,
    )
