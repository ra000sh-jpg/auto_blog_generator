"""성과 지표 API."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from modules.automation.job_store import JobStore
from server.dependencies import get_job_store

router = APIRouter()


class MetricsSummary(BaseModel):
    """메트릭 요약."""

    total_posts: int = 0
    total_views: int = 0
    total_likes: int = 0
    total_comments: int = 0
    avg_views: float = 0.0


class MetricsListResponse(BaseModel):
    """메트릭 목록 응답."""

    page: int
    size: int
    total: int
    pages: int
    summary: MetricsSummary
    items: List[Dict[str, Any]]


@router.get("/metrics", response_model=MetricsListResponse, summary="최근 성과 지표 조회")
def get_metrics(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    job_store: JobStore = Depends(get_job_store),
) -> MetricsListResponse:
    """최근 발행 포스트의 성과 데이터를 반환한다."""
    offset = (page - 1) * size
    metrics_payload = job_store.get_post_metrics_page(size=size, offset=offset)
    total = int(metrics_payload["total"])
    summary_row = metrics_payload["summary"]
    items = metrics_payload["items"]

    pages = max(1, math.ceil(total / size)) if total else 1
    summary = MetricsSummary(
        total_posts=int(summary_row["total_posts"]),
        total_views=int(summary_row["total_views"]),
        total_likes=int(summary_row["total_likes"]),
        total_comments=int(summary_row["total_comments"]),
        avg_views=float(summary_row["avg_views"]),
    )

    return MetricsListResponse(
        page=page,
        size=size,
        total=total,
        pages=pages,
        summary=summary,
        items=items,
    )


class LLMProviderStat(BaseModel):
    """LLM 프로바이더별 통계."""

    metric_type: str
    total_calls: int = 0
    success_calls: int = 0
    error_calls: int = 0
    error_rate: float = 0.0
    avg_duration_ms: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0


class LLMMetricsResponse(BaseModel):
    """LLM 관찰성 메트릭 응답."""

    window_hours: int
    total_llm_calls: int
    by_type: List[LLMProviderStat]


@router.get("/metrics/llm", response_model=LLMMetricsResponse, summary="LLM 호출 관찰성 메트릭")
def get_llm_metrics(
    hours: int = Query(default=24, ge=1, le=168, description="집계 기간(시간)"),
    job_store: JobStore = Depends(get_job_store),
) -> LLMMetricsResponse:
    """최근 N시간 내 LLM 호출 횟수·오류율·평균 응답시간을 반환한다."""
    with job_store.connection() as conn:
        cursor = conn.execute(
            """
            SELECT
                metric_type,
                COUNT(*) AS total_calls,
                SUM(CASE WHEN status IN ('ok', 'pass', 'success') THEN 1 ELSE 0 END) AS success_calls,
                SUM(CASE WHEN status NOT IN ('ok', 'pass', 'success') THEN 1 ELSE 0 END) AS error_calls,
                AVG(duration_ms) AS avg_duration_ms,
                AVG(input_tokens) AS avg_input_tokens,
                AVG(output_tokens) AS avg_output_tokens
            FROM job_metrics
            WHERE created_at >= datetime('now', ? || ' hours')
            GROUP BY metric_type
            ORDER BY total_calls DESC
            """,
            (f"-{hours}",),
        )
        rows = cursor.fetchall()

    stats: List[LLMProviderStat] = []
    total_calls = 0
    for row in rows:
        total = int(row["total_calls"])
        errors = int(row["error_calls"])
        total_calls += total
        stats.append(
            LLMProviderStat(
                metric_type=str(row["metric_type"]),
                total_calls=total,
                success_calls=int(row["success_calls"]),
                error_calls=errors,
                error_rate=round(errors / total, 4) if total > 0 else 0.0,
                avg_duration_ms=round(float(row["avg_duration_ms"] or 0.0), 1),
                avg_input_tokens=round(float(row["avg_input_tokens"] or 0.0), 1),
                avg_output_tokens=round(float(row["avg_output_tokens"] or 0.0), 1),
            )
        )

    return LLMMetricsResponse(
        window_hours=hours,
        total_llm_calls=total_calls,
        by_type=stats,
    )
