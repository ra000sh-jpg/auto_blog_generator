"""스케줄러 상태 조회 및 수동 트리거 라우터."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.dependencies import get_job_store

if TYPE_CHECKING:
    from modules.automation.job_store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# 응답 스키마
# ---------------------------------------------------------------------------


class SchedulerStatusResponse(BaseModel):
    scheduler_running: bool
    today_date: str
    daily_target: int
    today_completed: int
    today_failed: int
    ready_to_publish: int
    queued: int
    next_publish_slot_kst: Optional[str]
    active_hours: str
    last_seed_date: str
    last_seed_count: int


class TriggerResponse(BaseModel):
    ok: bool
    message: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# 전역 스케줄러 참조 (main.py 에서 lifespan 기동 후 주입)
# ---------------------------------------------------------------------------

_scheduler_instance: Any = None  # SchedulerService | None


def set_scheduler_instance(scheduler: Any) -> None:  # noqa: ANN001
    """main.py lifespan 에서 기동된 SchedulerService 를 등록한다."""
    global _scheduler_instance  # noqa: PLW0603
    _scheduler_instance = scheduler


def get_scheduler_instance() -> Any:  # noqa: ANN201
    return _scheduler_instance


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _get_kst_now_iso() -> str:
    """현재 KST 시각을 ISO 8601 형식으로 반환한다."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    except Exception:
        from datetime import datetime, timezone, timedelta

        kst = timezone(timedelta(hours=9))
        return datetime.now(kst).isoformat(timespec="seconds")


def _next_publish_slot_kst(scheduler: Any) -> Optional[str]:
    """스케줄러의 다음 발행 슬롯 시각을 KST ISO 문자열로 반환한다."""
    try:
        now_local = scheduler._get_now_local()
        today_completed = scheduler._get_today_post_count()
        slots = scheduler._daily_publish_slots
        if not slots:
            return None
        idx = today_completed
        if idx < len(slots):
            slot = slots[idx]
            return slot.isoformat(timespec="seconds")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


@router.get(
    "/scheduler/status",
    response_model=SchedulerStatusResponse,
    summary="스케줄러 상태 조회",
)
async def get_scheduler_status(
    job_store: "JobStore" = Depends(get_job_store),
) -> SchedulerStatusResponse:
    """스케줄러 실행 상태와 오늘의 발행 현황을 반환한다."""
    scheduler = get_scheduler_instance()
    scheduler_running = scheduler is not None and getattr(scheduler, "_scheduler", None) is not None

    # 날짜
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today_date = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    except Exception:
        from datetime import datetime, timezone, timedelta

        today_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()

    # DB에서 설정값 조회
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

    # 오늘 완료/실패 건수
    today_completed = job_store.get_today_completed_count()
    today_failed_fn = getattr(job_store, "get_today_failed_count", None)
    today_failed = int(today_failed_fn()) if callable(today_failed_fn) else 0

    # 큐 통계
    queue_stats = job_store.get_queue_stats()
    ready_to_publish = int(queue_stats.get("ready_to_publish", 0))
    queued = int(queue_stats.get("queued", 0))

    # 다음 슬롯
    next_slot = None
    if scheduler_running and scheduler:
        next_slot = _next_publish_slot_kst(scheduler)

    return SchedulerStatusResponse(
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


@router.post(
    "/scheduler/trigger/seed",
    response_model=TriggerResponse,
    summary="일간 큐 시드 수동 실행",
)
async def trigger_seed() -> TriggerResponse:
    """오늘 날짜의 큐 시드를 수동으로 생성한다 (테스트·복구용)."""
    scheduler = get_scheduler_instance()
    if scheduler is None:
        raise HTTPException(status_code=503, detail="스케줄러가 실행 중이 아닙니다.")
    try:
        # 중복 방지 날짜 키를 초기화해 강제 재실행
        if scheduler.job_store:
            scheduler.job_store.set_system_setting("scheduler_last_seed_date", "")
        await scheduler._run_daily_quota_seed()
        count_raw = scheduler.job_store.get_system_setting("scheduler_last_seed_count", "0") if scheduler.job_store else "0"
        return TriggerResponse(ok=True, message=f"큐 시드 완료 ({count_raw}건 생성)")
    except Exception as exc:
        logger.error("Manual seed trigger failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/scheduler/trigger/draft",
    response_model=TriggerResponse,
    summary="초안 선생성 1회 수동 실행",
)
async def trigger_draft() -> TriggerResponse:
    """초안 선생성 사이클을 수동으로 1회 실행한다."""
    scheduler = get_scheduler_instance()
    if scheduler is None:
        raise HTTPException(status_code=503, detail="스케줄러가 실행 중이 아닙니다.")
    try:
        await scheduler._run_draft_prefetch()
        ready = scheduler._get_ready_draft_count()
        return TriggerResponse(ok=True, message=f"초안 선생성 완료 (ready_to_publish={ready}건)")
    except Exception as exc:
        logger.error("Manual draft trigger failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/scheduler/trigger/publish",
    response_model=TriggerResponse,
    summary="발행 1회 수동 실행",
)
async def trigger_publish() -> TriggerResponse:
    """준비된 초안을 1건 즉시 발행한다."""
    scheduler = get_scheduler_instance()
    if scheduler is None:
        raise HTTPException(status_code=503, detail="스케줄러가 실행 중이 아닙니다.")
    try:
        published = await scheduler._publish_next_available_job()
        if published:
            return TriggerResponse(ok=True, message="발행 1건 완료")
        return TriggerResponse(ok=False, message="발행할 준비된 초안이 없습니다.")
    except Exception as exc:
        logger.error("Manual publish trigger failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
