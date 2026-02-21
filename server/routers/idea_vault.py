"""아이디어 창고(Idea Vault) API."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from modules.automation.job_store import JobStore
from modules.llm.idea_vault_parser import IdeaVaultBatchParser
from server.dependencies import get_idea_vault_parser, get_job_store

router = APIRouter()


class IdeaVaultIngestRequest(BaseModel):
    """아이디어 창고 입력 요청."""

    raw_text: str = Field(description="여러 줄 아이디어 텍스트")
    batch_size: int = Field(default=20, ge=1, le=50)


class IdeaVaultItemDTO(BaseModel):
    """아이디어 창고 아이템 DTO."""

    id: int
    raw_text: str
    mapped_category: str
    topic_mode: str
    status: str
    queued_job_id: str
    created_at: str
    updated_at: str
    consumed_at: str


class IdeaVaultStatsResponse(BaseModel):
    """아이디어 창고 통계 응답."""

    total: int
    pending: int
    queued: int
    consumed: int


class IdeaVaultIngestResponse(BaseModel):
    """아이디어 창고 적재 응답."""

    total_lines: int
    accepted_count: int
    rejected_count: int
    parser_used: str
    pending_count: int
    rejected_preview: List[Dict[str, str]]


class IdeaVaultListResponse(BaseModel):
    """아이디어 창고 목록 응답."""

    page: int
    size: int
    total: int
    pages: int
    items: List[IdeaVaultItemDTO]


def _parse_json_list(raw_value: str) -> List[str]:
    """JSON 문자열 리스트를 안전 파싱한다."""
    try:
        decoded = json.loads(raw_value)
    except Exception:
        return []
    if not isinstance(decoded, list):
        return []
    result: List[str] = []
    for item in decoded:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _get_allowed_categories(job_store: JobStore) -> List[str]:
    """사용자 설정 카테고리 목록을 반환한다."""
    raw = job_store.get_system_setting("custom_categories", "[]")
    categories = _parse_json_list(raw)
    if not categories:
        categories = ["다양한 생각"]
    return categories


@router.get(
    "/idea-vault/stats",
    response_model=IdeaVaultStatsResponse,
    summary="아이디어 창고 재고 통계",
)
def get_idea_vault_stats(
    job_store: JobStore = Depends(get_job_store),
) -> IdeaVaultStatsResponse:
    """아이디어 창고 상태별 카운트를 반환한다."""
    stats = job_store.get_idea_vault_stats()
    return IdeaVaultStatsResponse(
        total=int(stats.get("total", 0)),
        pending=int(stats.get("pending", 0)),
        queued=int(stats.get("queued", 0)),
        consumed=int(stats.get("consumed", 0)),
    )


@router.get(
    "/idea-vault/items",
    response_model=IdeaVaultListResponse,
    summary="아이디어 창고 목록 조회",
)
def list_idea_vault_items(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    job_store: JobStore = Depends(get_job_store),
) -> IdeaVaultListResponse:
    """아이디어 창고 목록을 페이지네이션으로 조회한다."""
    offset = (page - 1) * size
    total = job_store.count_idea_vault_items(status_filter=status_filter)
    items = job_store.list_idea_vault_items(
        limit=size,
        offset=offset,
        status_filter=status_filter,
    )
    pages = max(1, math.ceil(total / size)) if total else 1
    return IdeaVaultListResponse(
        page=page,
        size=size,
        total=total,
        pages=pages,
        items=[IdeaVaultItemDTO(**item) for item in items],
    )


@router.post(
    "/idea-vault/ingest",
    response_model=IdeaVaultIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="아이디어 창고 대량 적재",
)
async def ingest_idea_vault(
    request: IdeaVaultIngestRequest,
    parser: IdeaVaultBatchParser = Depends(get_idea_vault_parser),
    job_store: JobStore = Depends(get_job_store),
) -> IdeaVaultIngestResponse:
    """여러 줄 아이디어를 필터링/분류해 창고에 적재한다."""
    raw_text = request.raw_text.strip()
    if not raw_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="raw_text는 비어 있을 수 없습니다.",
        )

    categories = _get_allowed_categories(job_store)
    result = await parser.parse_bulk(
        raw_text,
        categories=categories,
        batch_size=request.batch_size,
    )

    inserted = 0
    if result.accepted_items:
        inserted = job_store.add_idea_vault_items(
            [
                {
                    "raw_text": item.raw_text,
                    "mapped_category": item.mapped_category,
                    "topic_mode": item.topic_mode,
                    "parser_used": item.parser_used,
                }
                for item in result.accepted_items
            ]
        )

    pending_count = job_store.get_idea_vault_pending_count()
    return IdeaVaultIngestResponse(
        total_lines=result.total_lines,
        accepted_count=inserted,
        rejected_count=len(result.rejected_lines),
        parser_used=result.parser_used,
        pending_count=pending_count,
        rejected_preview=result.rejected_lines[:20],
    )
