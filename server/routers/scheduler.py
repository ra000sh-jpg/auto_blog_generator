"""스케줄러 상태 조회 및 수동 트리거 라우터."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.dependencies import get_job_store
from modules.constants import ACTIVE_HOURS_DISPLAY

if TYPE_CHECKING:
    from modules.automation.job_store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# 응답 스키마
# ---------------------------------------------------------------------------


class SchedulerStatusResponse(BaseModel):
    scheduler_running: bool
    daemon_alive: bool
    api_only_mode: bool
    paused: bool
    today_date: str
    daily_target: int
    today_completed: int
    today_failed: int
    ready_to_publish: int
    queued: int
    ready_master: int
    ready_sub: int
    queued_master: int
    queued_sub: int
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


def _is_api_only_scheduler(scheduler: Any) -> bool:
    """현재 주입된 스케줄러가 API 전용 모드인지 확인한다."""
    return bool(getattr(scheduler, "api_only_mode", False))


def _is_daemon_alive(
    job_store: "JobStore",
    freshness_seconds: int = 120,
) -> bool:
    """데몬 하트비트 신선도로 생존 여부를 판단한다."""
    try:
        heartbeat_raw = job_store.get_system_setting("scheduler_daemon_heartbeat_at", "")
        if not heartbeat_raw:
            return False
        heartbeat_time = datetime.fromisoformat(heartbeat_raw.replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - heartbeat_time).total_seconds()
        return age_seconds <= freshness_seconds
    except Exception:
        return False


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
    get_next_slot = getattr(scheduler, "get_next_publish_slot_kst", None)
    if callable(get_next_slot):
        return get_next_slot()

    # 하위 호환: legacy private 접근 경로
    try:
        get_completed = getattr(scheduler, "get_today_post_count", None)
        today_completed = int(get_completed()) if callable(get_completed) else 0
        slots = scheduler._daily_publish_slots
        if not slots:
            return None
        idx = today_completed
        if idx < len(slots):
            return slots[idx].isoformat(timespec="seconds")
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
    api_only_mode = _is_api_only_scheduler(scheduler)
    daemon_alive = _is_daemon_alive(job_store)
    next_slot = None
    if scheduler_running and scheduler:
        next_slot = _next_publish_slot_kst(scheduler)

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
    paused = job_store.get_system_setting("scheduler_paused", "") == "1"

    # 오늘 완료/실패 건수
    today_completed = job_store.get_today_completed_count()
    today_failed_fn = getattr(job_store, "get_today_failed_count", None)
    today_failed = int(today_failed_fn()) if callable(today_failed_fn) else 0

    # 큐 통계 (get_queue_stats 키: completed/failed/queued/retry_wait 등)
    queue_stats = job_store.get_queue_stats()
    queued = int(queue_stats.get("queued", 0))
    ready_master = int(queue_stats.get("ready_master", 0))
    ready_sub = int(queue_stats.get("ready_sub", 0))
    queued_master = int(queue_stats.get("queued_master", 0))
    queued_sub = int(queue_stats.get("queued_sub", 0))
    ready_to_publish = ready_master + ready_sub

    return SchedulerStatusResponse(
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


@router.post(
    "/scheduler/start",
    response_model=TriggerResponse,
    summary="스케줄러 시작",
)
async def start_scheduler() -> TriggerResponse:
    """스케줄러 서비스를 실시간으로 시작한다."""
    scheduler = get_scheduler_instance()
    if scheduler is None:
        raise HTTPException(status_code=503, detail="스케줄러 인스턴스가 없습니다. 서버를 재시작하세요.")

    if _is_api_only_scheduler(scheduler):
        return TriggerResponse(
            ok=True,
            message="API 전용 모드입니다. 스케줄러 자동 실행은 별도 데몬(run_scheduler)에서 처리됩니다.",
        )

    if getattr(scheduler, "_scheduler", None) is not None:
        return TriggerResponse(ok=True, message="스케줄러가 이미 실행 중입니다.")

    try:
        await scheduler.start()
        return TriggerResponse(ok=True, message="스케줄러가 시작되었습니다.")
    except Exception as exc:
        logger.error("Scheduler start failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/scheduler/stop",
    response_model=TriggerResponse,
    summary="스케줄러 중지",
)
async def stop_scheduler() -> TriggerResponse:
    """스케줄러 서비스를 실시간으로 중지한다."""
    scheduler = get_scheduler_instance()
    if scheduler is None:
        return TriggerResponse(ok=True, message="스케줄러가 이미 중지 상태입니다.")

    if _is_api_only_scheduler(scheduler):
        return TriggerResponse(
            ok=True,
            message="API 전용 모드입니다. 데몬 상태는 별도 run_scheduler 서비스를 통해 관리됩니다.",
        )

    if getattr(scheduler, "_scheduler", None) is None:
        return TriggerResponse(ok=True, message="스케줄러가 이미 중지 상태입니다.")

    try:
        await scheduler.stop()
        # _scheduler를 None으로 초기화하여 상태 조회에도 반영
        scheduler._scheduler = None
        return TriggerResponse(ok=True, message="스케줄러가 중지되었습니다.")
    except Exception as exc:
        logger.error("Scheduler stop failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/scheduler/pause",
    response_model=TriggerResponse,
    summary="스케줄러 일시정지",
)
async def pause_scheduler(
    job_store: "JobStore" = Depends(get_job_store),
) -> TriggerResponse:
    """스케줄러 워커 루프를 일시정지한다."""
    job_store.set_system_setting("scheduler_paused", "1")
    logger.info("Scheduler paused via API")
    return TriggerResponse(ok=True, message="스케줄러가 일시정지되었습니다.")


@router.post(
    "/scheduler/resume",
    response_model=TriggerResponse,
    summary="스케줄러 재개",
)
async def resume_scheduler(
    job_store: "JobStore" = Depends(get_job_store),
) -> TriggerResponse:
    """일시정지된 스케줄러 워커 루프를 재개한다."""
    job_store.set_system_setting("scheduler_paused", "")
    logger.info("Scheduler resumed via API")
    return TriggerResponse(ok=True, message="스케줄러가 재개되었습니다.")


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
        await scheduler.trigger_seed_cycle()
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
        ready = await scheduler.trigger_draft_cycle()
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
        published = await scheduler.trigger_publish_cycle()
        if published:
            return TriggerResponse(ok=True, message="발행 1건 완료")
        return TriggerResponse(ok=False, message="발행할 준비된 초안이 없습니다.")
    except Exception as exc:
        logger.error("Manual publish trigger failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
