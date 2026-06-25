"""통합 대시보드 통계 API.

/api/stats/dashboard 한 번의 요청으로 대시보드에 필요한
모든 데이터를 반환한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from modules.automation.time_utils import now_utc
from modules.constants import ACTIVE_HOURS_DISPLAY
from server.dependencies import get_app_config, get_job_store, get_llm_router
from server.routers.scheduler import (
    _is_api_only_scheduler,
    _is_daemon_alive,
    _next_publish_slot_kst,
    get_scheduler_instance,
)

if TYPE_CHECKING:
    from modules.automation.job_store import JobStore
    from modules.config import AppConfig
    from modules.llm.llm_router import LLMRouter

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# LLM 프로바이더별 단가 테이블 (USD per 1K tokens)
# 키: job_metrics.provider 에 기록되는 프로바이더 이름과 일치해야 함
# 단가 기준: llm_router.py TextModelSpec 의 대표 모델 기준 (2026-02-27)
# ---------------------------------------------------------------------------

_USD_TO_KRW = 1_400  # 대략적 환율 (고정, 추후 외부 조회로 교체 가능)

# provider 이름 → (input_per_1k_usd, output_per_1k_usd)
LLM_PRICE_TABLE: Dict[str, tuple[float, float]] = {
    # Qwen / Alibaba (대표: qwen-plus  0.40/1.20 per 1M)
    "qwen": (0.0004, 0.0012),
    # DeepSeek (0.28/0.42 per 1M)
    "deepseek": (0.00028, 0.00042),
    # Google Gemini (대표: gemini-2.0-flash  0.10/0.40 per 1M)
    "gemini": (0.0001, 0.0004),
    # OpenAI (대표: gpt-4.1-mini  0.40/1.60 per 1M)
    "openai": (0.0004, 0.0016),
    # Anthropic Claude (대표: claude-haiku-3.5  0.80/4.00 per 1M)
    "claude": (0.0008, 0.004),
    # Groq (무료 Tier)
    "groq": (0.0, 0.0),
    # Cerebras (무료 Tier)
    "cerebras": (0.0, 0.0),
    # Z.AI GLM Flash (무료 Tier)
    "zai": (0.0, 0.0),
    # NVIDIA NIM (이용권 기반 무료 — credits 차감 없이 사용)
    "nvidia": (0.0, 0.0),
    "nvidia_vlm": (0.0, 0.0),
    "gemini_vlm": (0.0001, 0.0004),
    "groq_vlm": (0.00011, 0.00034),
    "openai_vlm": (0.0004, 0.0016),
    "qwen_vlm": (0.00011, 0.00034),
    # 기타 기본값 (qwen-plus 수준)
    "default": (0.0004, 0.0012),
}


def _lookup_price(provider: str) -> tuple[float, float]:
    """provider 이름에서 단가를 조회한다.

    job_metrics.provider 컬럼에 저장된 프로바이더 이름으로 조회한다.
    정확 일치 → 부분 일치 → default 순으로 폴백한다.
    """
    lowered = (provider or "").lower().strip()
    # 1) 정확 일치
    if lowered in LLM_PRICE_TABLE:
        return LLM_PRICE_TABLE[lowered]
    # 2) 부분 일치 (provider 문자열이 키를 포함하거나 키가 포함된 경우)
    for key, price in LLM_PRICE_TABLE.items():
        if key == "default":
            continue
        if key in lowered or lowered in key:
            return price
    return LLM_PRICE_TABLE["default"]


def _calc_llm_cost_usd(
    avg_input_tokens: float,
    avg_output_tokens: float,
    total_calls: int,
    provider: str,
) -> float:
    """LLM 호출 비용을 USD로 계산한다."""
    in_price, out_price = _lookup_price(provider)
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
    avg_vlm_visual_score: float = 0.0
    score_per_won_trend: List[Dict[str, Union[float, str]]] = Field(default_factory=list)
    champion_history: List[Dict[str, Any]] = Field(default_factory=list)


class SchedulerData(BaseModel):
    """스케줄러 상태."""

    scheduler_running: bool = False
    daemon_alive: bool = False
    api_only_mode: bool = False
    paused: bool = False
    today_date: str = ""
    daily_target: int = 3
    today_completed: int = 0
    today_failed: int = 0
    ready_to_publish: int = 0
    queued: int = 0
    ready_master: int = 0
    ready_sub: int = 0
    queued_master: int = 0
    queued_sub: int = 0
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
    snapshot = job_store.get_dashboard_metrics_snapshot(today=today)
    today_published = int(snapshot.get("today_published", 0))
    total_published = int(snapshot.get("total_published", 0))
    idea_vault_pending = int(snapshot.get("idea_vault_pending", 0))
    idea_vault_total = int(snapshot.get("idea_vault_total", 0))
    llm_rows = list(snapshot.get("llm_rows", []))
    trend_rows = list(snapshot.get("trend_rows", []))
    avg_vlm_visual_score = float(snapshot.get("avg_vlm_visual_score", 0.0) or 0.0)

    total_cost_usd = 0.0
    total_llm_calls = 0
    for row in llm_rows:
        calls = int(row["total_calls"])
        total_llm_calls += calls
        cost = _calc_llm_cost_usd(
            avg_input_tokens=float(row["avg_input"] or 0),
            avg_output_tokens=float(row["avg_output"] or 0),
            total_calls=calls,
            provider=str(row.get("provider") or ""),
        )
        total_cost_usd += cost

    total_cost_krw = int(total_cost_usd * _USD_TO_KRW)
    trend_payload: List[Dict[str, Union[float, str]]] = []
    for row in trend_rows:
        trend_payload.append(
            {
                "week_start": str(row["week_start"] or ""),
                "avg_score_per_won": round(float(row["avg_score_per_won"] or 0.0), 4),
                "avg_quality_score": round(float(row["avg_quality_score"] or 0.0), 2),
            }
        )

    champion_history = job_store.list_champion_history(limit=4)

    return MetricsSummaryData(
        today_published=today_published,
        total_published=total_published,
        idea_vault_pending=idea_vault_pending,
        idea_vault_total=idea_vault_total,
        llm_cost_usd=round(total_cost_usd, 6),
        llm_cost_krw=total_cost_krw,
        llm_total_calls=total_llm_calls,
        avg_vlm_visual_score=round(avg_vlm_visual_score, 2),
        score_per_won_trend=trend_payload,
        champion_history=champion_history,
    )


def _build_scheduler_data(job_store: "JobStore") -> SchedulerData:
    """스케줄러 상태를 조회한다."""
    scheduler = get_scheduler_instance()
    scheduler_running = (
        scheduler is not None and getattr(scheduler, "_scheduler", None) is not None
    )
    daemon_alive = _is_daemon_alive(job_store)
    api_only_mode = _is_api_only_scheduler(scheduler)
    paused = job_store.get_system_setting("scheduler_paused", "") == "1"

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
    ready_master = int(queue_stats.get("ready_master", 0))
    ready_sub = int(queue_stats.get("ready_sub", 0))
    queued_master = int(queue_stats.get("queued_master", 0))
    queued_sub = int(queue_stats.get("queued_sub", 0))
    ready_to_publish = ready_master + ready_sub

    next_slot: Optional[str] = None
    if scheduler_running and scheduler:
        next_slot = _next_publish_slot_kst(scheduler)

    return SchedulerData(
        scheduler_running=scheduler_running,
        daemon_alive=daemon_alive,
        api_only_mode=api_only_mode,
        paused=paused,
        today_date=today_date,
        daily_target=daily_target,
        today_completed=today_completed,
        today_failed=today_failed,
        ready_to_publish=ready_to_publish,
        queued=queued,
        ready_master=ready_master,
        ready_sub=ready_sub,
        queued_master=queued_master,
        queued_sub=queued_sub,
        next_publish_slot_kst=next_slot,
        active_hours=ACTIVE_HOURS_DISPLAY,
        last_seed_date=last_seed_date,
        last_seed_count=last_seed_count,
    )


async def _build_health_summary(app_config: "AppConfig", llm_router: "LLMRouter") -> HealthSummaryData:
    """LLM/API 헬스 요약을 조회한다 (헤비한 체크는 skip)."""
    try:
        from modules.llm.api_health import check_all_providers

        router_settings = llm_router.get_saved_settings()
        text_api_keys = router_settings.get("text_api_keys", {})

        rows = await check_all_providers(
            skip_expensive=True,
            llm_config=app_config.llm,
            api_keys=text_api_keys,
        )
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
    llm_router: "LLMRouter" = Depends(get_llm_router),
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
        _build_health_summary(app_config, llm_router),
    )

    return DashboardResponse(
        timestamp=now_utc(),
        metrics=metrics,
        scheduler=scheduler_data,
        telegram=telegram_data,
        health=health_data,
    )
