"""통합 대시보드 통계 API.

/api/stats/dashboard 한 번의 요청으로 대시보드에 필요한
모든 데이터를 반환한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from modules.automation.time_utils import now_utc
from server.dependencies import get_app_config, get_job_store
from server.routers.scheduler import get_scheduler_instance, _next_publish_slot_kst

if TYPE_CHECKING:
    from modules.automation.job_store import JobStore
    from modules.config import AppConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# LLM 모델별 단가 테이블 (USD per 1K tokens)
# 입력(input) / 출력(output) 단가 모두 포함
# ---------------------------------------------------------------------------

_USD_TO_KRW = 1_400  # 대략적 환율 (고정, 추후 외부 조회로 교체 가능)

# metric_type 키워드 → (input_per_1k_usd, output_per_1k_usd)
LLM_PRICE_TABLE: Dict[str, tuple[float, float]] = {
    # Qwen / Alibaba
    "qwen": (0.0004, 0.0012),
    "qwen2": (0.0004, 0.0012),
    "qwen3": (0.0004, 0.0012),
    # DeepSeek
    "deepseek": (0.00027, 0.0011),
    # Groq (Llama 기반, 매우 저렴)
    "groq": (0.00005, 0.00008),
    "llama": (0.00005, 0.00008),
    # Cerebras
    "cerebras": (0.00006, 0.00011),
    # OpenAI
    "openai": (0.005, 0.015),
    "gpt-4": (0.01, 0.03),
    "gpt-3.5": (0.0005, 0.0015),
    # Anthropic Claude
    "claude": (0.003, 0.015),
    # Google Gemini
    "gemini": (0.00035, 0.00105),
    # 기타 기본값
    "default": (0.001, 0.002),
}


def _lookup_price(metric_type: str) -> tuple[float, float]:
    """metric_type 문자열에서 단가를 조회한다."""
    lowered = metric_type.lower()
    for key, price in LLM_PRICE_TABLE.items():
        if key in lowered:
            return price
    return LLM_PRICE_TABLE["default"]


def _calc_llm_cost_usd(
    avg_input_tokens: float,
    avg_output_tokens: float,
    total_calls: int,
    metric_type: str,
) -> float:
    """LLM 호출 비용을 USD로 계산한다."""
    in_price, out_price = _lookup_price(metric_type)
    cost = (avg_input_tokens / 1000 * in_price + avg_output_tokens / 1000 * out_price) * total_calls
    return cost


# ---------------------------------------------------------------------------
# 응답 스키마
# ---------------------------------------------------------------------------


class MetricsSummaryData(BaseModel):
    """오늘/누적 발행 통계."""

    today_published: int = 0
    total_published: int = 0
    idea_vault_pending: int = 0
    idea_vault_total: int = 0
    llm_cost_usd: float = 0.0
    llm_cost_krw: int = 0
    llm_total_calls: int = 0


class SchedulerData(BaseModel):
    """스케줄러 상태."""

    scheduler_running: bool = False
    today_date: str = ""
    daily_target: int = 3
    today_completed: int = 0
    today_failed: int = 0
    ready_to_publish: int = 0
    queued: int = 0
    next_publish_slot_kst: Optional[str] = None
    active_hours: str = "08:00~22:00"
    last_seed_date: str = ""
    last_seed_count: int = 0


class TelegramStatusData(BaseModel):
    """텔레그램 연결 상태."""

    configured: bool = False
    live_ok: bool = False
    bot_username: Optional[str] = None
    error: Optional[str] = None


class HealthSummaryData(BaseModel):
    """LLM/API 헬스 요약."""

    status: str = "unknown"
    ok: int = 0
    fail: int = 0
    total: int = 0


class DashboardResponse(BaseModel):
    """통합 대시보드 응답."""

    timestamp: str
    metrics: MetricsSummaryData
    scheduler: SchedulerData
    telegram: TelegramStatusData
    health: HealthSummaryData


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _get_today_kst() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    except Exception:
        from datetime import datetime, timezone, timedelta

        return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


async def _fetch_telegram_status(job_store: "JobStore") -> TelegramStatusData:
    """텔레그램 봇 라이브 상태를 확인한다 (getMe API 호출)."""
    bot_token = job_store.get_system_setting("telegram_bot_token", "")
    if not bot_token:
        return TelegramStatusData(configured=False, live_ok=False, error="봇 토큰 미설정")

    try:
        import httpx  # type: ignore[import]

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getMe"
            )
            data = resp.json()

        if data.get("ok"):
            username = data.get("result", {}).get("username")
            return TelegramStatusData(
                configured=True,
                live_ok=True,
                bot_username=username,
            )
        else:
            description = data.get("description", "알 수 없는 오류")
            return TelegramStatusData(
                configured=True,
                live_ok=False,
                error=description,
            )
    except Exception as exc:
        logger.warning("Telegram getMe failed: %s", exc)
        return TelegramStatusData(
            configured=True,
            live_ok=False,
            error=str(exc),
        )


def _build_metrics(job_store: "JobStore") -> MetricsSummaryData:
    """DB에서 발행 통계 및 LLM 비용을 집계한다."""
    today = _get_today_kst()

    with job_store.connection() as conn:
        # 오늘 발행 완료 건수
        today_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM jobs
            WHERE status = 'published'
              AND date(updated_at) = ?
            """,
            (today,),
        ).fetchone()
        today_published = int(today_row["cnt"]) if today_row else 0

        # 전체 누적 발행 건수
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'published'"
        ).fetchone()
        total_published = int(total_row["cnt"]) if total_row else 0

        # Idea Vault pending
        vault_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM idea_vault WHERE status = 'pending'"
        ).fetchone()
        idea_vault_pending = int(vault_row["cnt"]) if vault_row else 0

        # Idea Vault 전체
        vault_total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM idea_vault"
        ).fetchone()
        idea_vault_total = int(vault_total_row["cnt"]) if vault_total_row else 0

        # LLM 누적 비용 계산 (전체 기간)
        llm_rows = conn.execute(
            """
            SELECT
                metric_type,
                COUNT(*) AS total_calls,
                AVG(input_tokens) AS avg_input,
                AVG(output_tokens) AS avg_output
            FROM job_metrics
            GROUP BY metric_type
            """
        ).fetchall()

    total_cost_usd = 0.0
    total_llm_calls = 0
    for row in llm_rows:
        calls = int(row["total_calls"])
        total_llm_calls += calls
        cost = _calc_llm_cost_usd(
            avg_input_tokens=float(row["avg_input"] or 0),
            avg_output_tokens=float(row["avg_output"] or 0),
            total_calls=calls,
            metric_type=str(row["metric_type"]),
        )
        total_cost_usd += cost

    total_cost_krw = int(total_cost_usd * _USD_TO_KRW)

    return MetricsSummaryData(
        today_published=today_published,
        total_published=total_published,
        idea_vault_pending=idea_vault_pending,
        idea_vault_total=idea_vault_total,
        llm_cost_usd=round(total_cost_usd, 6),
        llm_cost_krw=total_cost_krw,
        llm_total_calls=total_llm_calls,
    )


def _build_scheduler_data(job_store: "JobStore") -> SchedulerData:
    """스케줄러 상태를 조회한다."""
    scheduler = get_scheduler_instance()
    scheduler_running = (
        scheduler is not None and getattr(scheduler, "_scheduler", None) is not None
    )

    today_date = _get_today_kst()

    daily_target_raw = job_store.get_system_setting("scheduler_daily_posts_target", "3")
    try:
        daily_target = max(1, int(daily_target_raw))
    except (ValueError, TypeError):
        daily_target = 3

    last_seed_date = job_store.get_system_setting("scheduler_last_seed_date", "")
    last_seed_count_raw = job_store.get_system_setting("scheduler_last_seed_count", "0")
    try:
        last_seed_count = int(last_seed_count_raw)
    except (ValueError, TypeError):
        last_seed_count = 0

    today_completed = job_store.get_today_completed_count()
    today_failed_fn = getattr(job_store, "get_today_failed_count", None)
    today_failed = int(today_failed_fn()) if callable(today_failed_fn) else 0

    queue_stats = job_store.get_queue_stats()
    queued = int(queue_stats.get("queued", 0))
    # prepared_payload 가 있는 queued 잡 = 발행 즉시 가능
    try:
        with job_store.connection() as _conn:
            _row = _conn.execute(
                "SELECT COUNT(*) AS cnt FROM jobs WHERE status='queued' AND prepared_payload IS NOT NULL"
            ).fetchone()
            ready_to_publish = int(_row["cnt"]) if _row else 0
    except Exception:
        ready_to_publish = 0

    next_slot: Optional[str] = None
    if scheduler_running and scheduler:
        next_slot = _next_publish_slot_kst(scheduler)

    return SchedulerData(
        scheduler_running=scheduler_running,
        today_date=today_date,
        daily_target=daily_target,
        today_completed=today_completed,
        today_failed=today_failed,
        ready_to_publish=ready_to_publish,
        queued=queued,
        next_publish_slot_kst=next_slot,
        active_hours="08:00~22:00",
        last_seed_date=last_seed_date,
        last_seed_count=last_seed_count,
    )


async def _build_health_summary(app_config: "AppConfig") -> HealthSummaryData:
    """LLM/API 헬스 요약을 조회한다 (헤비한 체크는 skip)."""
    try:
        from modules.llm.api_health import check_all_providers

        rows = await check_all_providers(skip_expensive=True, llm_config=app_config.llm)
        ok_count = sum(1 for r in rows if str(r.get("status", "")).upper() == "OK")
        fail_count = len(rows) - ok_count
        overall = "ok" if fail_count == 0 else "degraded"
        return HealthSummaryData(
            status=overall,
            ok=ok_count,
            fail=fail_count,
            total=len(rows),
        )
    except Exception as exc:
        logger.warning("Health summary fetch failed: %s", exc)
        return HealthSummaryData(status="unknown")


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


@router.get(
    "/stats/dashboard",
    response_model=DashboardResponse,
    summary="통합 대시보드 통계 조회",
)
async def get_dashboard_stats(
    job_store: "JobStore" = Depends(get_job_store),
    app_config: "AppConfig" = Depends(get_app_config),
) -> DashboardResponse:
    """대시보드에 필요한 모든 통계를 한 번의 요청으로 반환한다.

    병렬로 여러 소스를 집계하여 응답 속도를 최소화한다.
    """
    # 동기 작업 → run_in_executor 없이 직접 호출 (DB는 단일 연결 SQLite)
    metrics = _build_metrics(job_store)
    scheduler_data = _build_scheduler_data(job_store)

    # 비동기 병렬 실행
    telegram_data, health_data = await asyncio.gather(
        _fetch_telegram_status(job_store),
        _build_health_summary(app_config),
    )

    return DashboardResponse(
        timestamp=now_utc(),
        metrics=metrics,
        scheduler=scheduler_data,
        telegram=telegram_data,
        health=health_data,
    )
