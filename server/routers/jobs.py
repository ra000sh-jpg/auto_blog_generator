"""작업(Job) API."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from modules.automation.job_store import Job, JobStore
from modules.automation.time_utils import now_utc, parse_iso
from modules.llm.prompts import normalize_topic_mode
from modules.seo.platform_strategy import get_category_for_topic
from server.dependencies import get_job_store

router = APIRouter()


class JobsListResponse(BaseModel):
    """작업 목록 응답."""

    page: int
    size: int
    total: int
    pages: int
    queue_stats: Dict[str, int]
    items: List[Dict[str, Any]]


class CreateJobRequest(BaseModel):
    """작업 생성 요청."""

    title: str = Field(description="포스트 제목")
    seed_keywords: List[str] = Field(default_factory=list, description="시드 키워드")
    platform: str = "naver"
    persona_id: str = "P1"
    scheduled_at: Optional[str] = None
    topic_mode: Optional[str] = None
    category: Optional[str] = None
    max_retries: int = 3
    tags: List[str] = Field(default_factory=list)


class CreateJobResponse(BaseModel):
    """작업 생성 응답."""

    job_id: str
    status: str
    scheduled_at: str
    platform: str
    persona_id: str
    topic_mode: str
    category: str


def _serialize_job(job: Job) -> Dict[str, Any]:
    """Job dataclass를 API 응답용 dict로 변환한다."""
    payload = asdict(job)
    return payload


def _normalize_scheduled_at(raw_value: Optional[str]) -> str:
    """입력 시간을 UTC ISO 문자열로 정규화한다."""
    if not raw_value:
        return now_utc()
    parsed = parse_iso(raw_value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_keywords(keywords: List[str]) -> List[str]:
    """키워드를 정리한다."""
    normalized = []
    seen = set()
    for keyword in keywords:
        value = str(keyword).strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(value)
    return normalized


@router.get("/jobs", response_model=JobsListResponse, summary="작업 목록 조회")
def list_jobs(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    job_store: JobStore = Depends(get_job_store),
) -> JobsListResponse:
    """작업 목록을 페이지네이션으로 조회한다."""
    offset = (page - 1) * size
    statuses = [
        token.strip() for token in (status_filter or "").split(",") if token.strip()
    ]

    where_sql = ""
    params: List[Any] = []
    if statuses:
        placeholders = ",".join(["?"] * len(statuses))
        where_sql = f" WHERE status IN ({placeholders})"
        params.extend(statuses)

    with job_store.connection() as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM jobs{where_sql}",
            tuple(params),
        ).fetchone()
        total = int(total_row["total"]) if total_row else 0

        cursor = conn.execute(
            f"""
            SELECT *
            FROM jobs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
            OFFSET ?
            """,
            tuple(params + [size, offset]),
        )
        jobs = [Job.from_row(row) for row in cursor.fetchall()]

    pages = max(1, math.ceil(total / size)) if total else 1
    return JobsListResponse(
        page=page,
        size=size,
        total=total,
        pages=pages,
        queue_stats=job_store.get_queue_stats(),
        items=[_serialize_job(job) for job in jobs],
    )


@router.post(
    "/jobs",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="작업 예약 생성",
)
def create_job(
    request: CreateJobRequest,
    job_store: JobStore = Depends(get_job_store),
) -> CreateJobResponse:
    """신규 포스팅 예약 작업을 생성한다."""
    title = request.title.strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title은 비어 있을 수 없습니다.",
        )

    seed_keywords = _normalize_keywords(request.seed_keywords)
    if not seed_keywords:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="seed_keywords는 최소 1개 이상 필요합니다.",
        )

    platform = request.platform.strip().lower() or "naver"
    persona_id = request.persona_id.strip() or "P1"
    scheduled_at = _normalize_scheduled_at(request.scheduled_at)
    raw_topic_mode = (request.topic_mode or "").strip()
    topic_mode = normalize_topic_mode(raw_topic_mode) if raw_topic_mode else ""

    category = (request.category or "").strip()
    if not category and topic_mode:
        category = get_category_for_topic(topic_mode=topic_mode, platform=platform)

    job_id = str(uuid.uuid4())
    success = job_store.schedule_job(
        job_id=job_id,
        title=title,
        seed_keywords=seed_keywords,
        platform=platform,
        persona_id=persona_id,
        scheduled_at=scheduled_at,
        max_retries=max(1, int(request.max_retries)),
        tags=request.tags,
        category=category,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="중복 작업 또는 동일 idempotency key로 등록 실패했습니다.",
        )

    return CreateJobResponse(
        job_id=job_id,
        status=job_store.STATUS_QUEUED,
        scheduled_at=scheduled_at,
        platform=platform,
        persona_id=persona_id,
        topic_mode=topic_mode,
        category=category,
    )


@router.get(
    "/jobs/{job_id}",
    summary="작업 상세 조회",
)
def get_job_detail(
    job_id: str,
    job_store: JobStore = Depends(get_job_store),
) -> Dict[str, Any]:
    """특정 작업(Job)의 상세 정보를 조회한다."""
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"작업을 찾을 수 없습니다: {job_id}",
        )
    return _serialize_job(job)
