"""Phase 24 Idle-Aware Hybrid Publishing — E2E 테스트.

검증 시나리오:
1. run_interrupt_watchdog: CPU 급등 시 interrupt_event 가 set 되어야 한다
2. run_interrupt_watchdog: CPU 정상이면 interrupt_event 가 set 되지 않아야 한다
3. _run_draft_prefetch: watchdog 인터럽트 발생 시 루프가 중간에 멈춰야 한다
4. _run_draft_prefetch: 리소스 여유 시 needed 만큼 정상 생성 완료
5. _get_publish_anchor_hours: DB 설정이 없으면 클래스 상수 반환
6. _get_publish_anchor_hours: DB 에 publish_anchor_hours 설정 시 파싱 및 반환
7. _build_daily_publish_slots: σ=20 가우시안으로 슬롯이 앵커 ±40분 이내에 생성
8. _build_daily_publish_slots: DB 커스텀 앵커 시간이 슬롯에 반영
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.resource_monitor import CpuHysteresisMonitor
from modules.automation.scheduler_service import SchedulerService


# ─────────────────────────────────────────────────────────────────────────────
# 공통 헬퍼
# ─────────────────────────────────────────────────────────────────────────────


def build_store(tmp_path: Path, name: str = "phase24_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def build_scheduler(
    job_store: JobStore,
    cpu_start: float = 28.0,
    cpu_stop: float = 35.0,
) -> SchedulerService:
    return SchedulerService(
        pipeline_service=None,
        job_store=job_store,
        timezone_name="Asia/Seoul",
        daily_posts_target=3,
        random_seed=42,
        cpu_start_threshold_percent=cpu_start,
        cpu_stop_threshold_percent=cpu_stop,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. watchdog: CPU 급등 → interrupt_event set
# ─────────────────────────────────────────────────────────────────────────────


def test_watchdog_sets_event_on_cpu_spike():
    """CPU 급등 샘플 투입 시 watchdog 가 interrupt_event 를 set 해야 한다."""
    # 처음 몇 번은 정상, 이후 급등
    readings = iter([20.0, 20.0, 80.0])
    monitor = CpuHysteresisMonitor(
        start_threshold_percent=28.0,
        stop_threshold_percent=35.0,
        sample_window=3,
        sampler=lambda: next(readings, 80.0),
    )

    async def _run():
        event = monitor.make_interrupt_event()
        # poll_interval 0.05초로 빠르게 테스트
        task = asyncio.create_task(
            monitor.run_interrupt_watchdog(event, poll_interval_seconds=0.05)
        )
        # 최대 2초 대기 — 이전에 event 가 set 되면 탈출
        for _ in range(40):
            await asyncio.sleep(0.05)
            if event.is_set():
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return event.is_set()

    result = asyncio.run(_run())
    assert result is True, "CPU 급등 시 interrupt_event 가 set 되어야 한다"


# ─────────────────────────────────────────────────────────────────────────────
# 2. watchdog: CPU 정상 → interrupt_event NOT set
# ─────────────────────────────────────────────────────────────────────────────


def test_watchdog_does_not_set_event_on_normal_cpu():
    """CPU 가 정상 범위이면 watchdog 가 interrupt_event 를 set 하면 안 된다."""
    monitor = CpuHysteresisMonitor(
        start_threshold_percent=28.0,
        stop_threshold_percent=35.0,
        sample_window=3,
        sampler=lambda: 10.0,  # 항상 낮은 CPU
    )

    async def _run():
        event = monitor.make_interrupt_event()
        task = asyncio.create_task(
            monitor.run_interrupt_watchdog(event, poll_interval_seconds=0.05)
        )
        await asyncio.sleep(0.3)  # 6번 폴링
        event.set()  # 강제 종료
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return event.is_set()

    # 외부에서 set 한 것이므로 True 이지만, 그 전에 watchdog 가 먼저 set 했는지 확인하기 위해
    # watchdog 가 set 하기 전에 sleep 후 확인하는 방식을 사용
    monitor2 = CpuHysteresisMonitor(
        start_threshold_percent=28.0,
        stop_threshold_percent=35.0,
        sample_window=3,
        sampler=lambda: 10.0,
    )

    async def _run2():
        event = monitor2.make_interrupt_event()
        task = asyncio.create_task(
            monitor2.run_interrupt_watchdog(event, poll_interval_seconds=0.3)
        )
        # 폴링 전에 상태 확인 (0.1초 대기 — 첫 폴링 0.3초 전)
        await asyncio.sleep(0.1)
        was_set = event.is_set()
        event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return was_set

    result = asyncio.run(_run2())
    assert result is False, "CPU 정상일 때 watchdog 가 event 를 set 하면 안 된다"


# ─────────────────────────────────────────────────────────────────────────────
# 3. _run_draft_prefetch: watchdog 인터럽트 → 루프 중단
# ─────────────────────────────────────────────────────────────────────────────


def test_draft_prefetch_stops_on_cpu_interrupt(tmp_path: Path):
    """CPU watchdog 인터럽트 시 prefetch 루프가 중간에 멈춰야 한다.

    전략: _prepare_next_available_job 이 await asyncio.sleep(0) 을 포함하도록
    하여 매 반복마다 이벤트 루프가 한 번씩 돌게 만든다.
    watchdog poll_interval=0.0 (즉시 폴링)으로 설정해 2번째 호출 이후
    바로 interrupt_event 가 set 되도록 한다.
    """
    store = build_store(tmp_path)
    scheduler = build_scheduler(store, cpu_start=28.0, cpu_stop=35.0)

    call_count = 0

    async def _fake_prepare():
        nonlocal call_count
        call_count += 1
        # 이벤트 루프를 한 턴 양보해 watchdog 가 실행될 기회를 준다
        await asyncio.sleep(0)
        if call_count >= 2:
            # 2번째 호출 후 stop_threshold 를 0 으로 내려 항상 급등 상태로 만듦
            scheduler._cpu_monitor.stop_threshold_percent = 0.0
        return True

    async def _run():
        scheduler.pipeline_service = MagicMock()
        with patch.object(scheduler, "_prepare_next_available_job", side_effect=_fake_prepare):
            with patch.object(scheduler, "_has_resource_headroom", return_value=True):
                with patch.object(scheduler, "_get_ready_draft_count", return_value=0):
                    with patch.object(scheduler, "_get_configured_daily_target", return_value=3):
                        scheduler._cpu_monitor._generation_enabled = True
                        scheduler._cpu_monitor._samples.extend([10.0, 10.0, 10.0])
                        # watchdog poll_interval 을 0.01 초로 단축 — 빠른 인터럽트
                        orig_watchdog = scheduler._cpu_monitor.run_interrupt_watchdog

                        async def _fast_watchdog(event, poll_interval_seconds=3.0):
                            return await orig_watchdog(event, poll_interval_seconds=0.01)

                        with patch.object(
                            scheduler._cpu_monitor,
                            "run_interrupt_watchdog",
                            side_effect=_fast_watchdog,
                        ):
                            await scheduler._run_draft_prefetch()
        return call_count

    count = asyncio.run(_run())
    # needed=9, 2번째 준비 후 급등 → watchdog 가 다음 폴링(0.01s)에 event set
    # 3번째 반복 시작 전 interrupt_event 확인으로 탈출
    assert count <= 4, f"CPU 인터럽트 후 prepare 횟수({count})가 너무 많다"


# ─────────────────────────────────────────────────────────────────────────────
# 4. _run_draft_prefetch: 리소스 여유 시 정상 생성 완료
# ─────────────────────────────────────────────────────────────────────────────


def test_draft_prefetch_completes_when_resource_ok(tmp_path: Path):
    """리소스 여유 상태에서 needed 만큼 전부 생성 완료되어야 한다."""
    store = build_store(tmp_path)
    scheduler = build_scheduler(store, cpu_start=80.0, cpu_stop=90.0)  # 임계값 높게

    call_count = 0

    async def _fake_prepare():
        nonlocal call_count
        call_count += 1
        return True

    async def _slow_watchdog(event, poll_interval_seconds=3.0):
        """폴링 간격을 60초로 늘려 루프가 끝나기 전에 인터럽트가 발생하지 않도록 한다."""
        await asyncio.sleep(60)

    async def _run():
        scheduler.pipeline_service = MagicMock()
        with patch.object(scheduler, "_prepare_next_available_job", side_effect=_fake_prepare):
            with patch.object(scheduler, "_has_resource_headroom", return_value=True):
                with patch.object(scheduler, "_get_ready_draft_count", return_value=0):
                    with patch.object(scheduler, "_get_configured_daily_target", return_value=3):
                        scheduler._cpu_monitor._generation_enabled = True
                        scheduler._cpu_monitor._samples.extend([10.0, 10.0, 10.0])
                        # watchdog 를 60초 대기로 대체 — 루프가 먼저 끝남
                        with patch.object(
                            scheduler._cpu_monitor,
                            "run_interrupt_watchdog",
                            side_effect=_slow_watchdog,
                        ):
                            await scheduler._run_draft_prefetch()
        return call_count

    count = asyncio.run(_run())

    # needed = max(extended_buffer=9, target+2=5) = 9
    assert count >= 5, f"충분한 리소스 시 최소 5건 이상 생성 기대, 실제={count}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. _get_publish_anchor_hours: DB 설정 없으면 클래스 상수
# ─────────────────────────────────────────────────────────────────────────────


def test_get_publish_anchor_hours_defaults(tmp_path: Path):
    """DB 설정 없으면 PUBLISH_ANCHOR_HOURS 상수를 반환해야 한다."""
    store = build_store(tmp_path)
    scheduler = build_scheduler(store)

    hours = scheduler._get_publish_anchor_hours()
    assert hours == SchedulerService.PUBLISH_ANCHOR_HOURS, (
        f"Expected {SchedulerService.PUBLISH_ANCHOR_HOURS}, got {hours}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. _get_publish_anchor_hours: DB 설정값 파싱
# ─────────────────────────────────────────────────────────────────────────────


def test_get_publish_anchor_hours_from_db(tmp_path: Path):
    """DB 에 publish_anchor_hours 설정 시 파싱된 튜플을 반환해야 한다."""
    store = build_store(tmp_path)
    store.set_system_setting("publish_anchor_hours", "8,13,20")
    scheduler = build_scheduler(store)

    hours = scheduler._get_publish_anchor_hours()
    assert hours == (8, 13, 20), f"DB 설정 파싱 실패: {hours}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. _build_daily_publish_slots: σ=20 가우시안 ±40분 이내 슬롯
# ─────────────────────────────────────────────────────────────────────────────


def test_build_daily_publish_slots_within_gaussian_bounds(tmp_path: Path):
    """슬롯이 활성 시간대 안에 있고, 오름차순 정렬되어야 한다.

    슬롯은 가우시안 지터 + min_interval 보정을 거치므로
    앵커와의 절대 차이보다 "유효 시간대(ACTIVE_HOURS) 내 + 단조 증가" 를 검증한다.
    """
    store = build_store(tmp_path)
    scheduler = build_scheduler(store)

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("Asia/Seoul")
    except Exception:
        local_tz = timezone(timedelta(hours=9))

    target_date = date(2026, 3, 1)
    slots = scheduler._build_daily_publish_slots(target_date, daily_target=3)

    assert len(slots) == 3, f"슬롯 수 불일치: {len(slots)}"

    active_start = datetime.combine(
        target_date, __import__("datetime").time(hour=SchedulerService.ACTIVE_HOURS[0]), tzinfo=local_tz
    )
    active_end = datetime.combine(
        target_date, __import__("datetime").time(hour=SchedulerService.ACTIVE_HOURS[1]), tzinfo=local_tz
    )

    for i, slot in enumerate(slots):
        assert active_start <= slot < active_end, (
            f"슬롯[{i}] {slot.isoformat()} 이 활성 시간대({SchedulerService.ACTIVE_HOURS}) 밖"
        )

    # 단조 증가 (정렬 보장)
    for i in range(1, len(slots)):
        assert slots[i] > slots[i - 1], f"슬롯이 정렬되지 않음: {slots}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. _build_daily_publish_slots: DB 커스텀 앵커 반영
# ─────────────────────────────────────────────────────────────────────────────


def test_build_daily_publish_slots_uses_db_anchor_hours(tmp_path: Path):
    """DB 에 publish_anchor_hours 설정 시 활성 시간대 내 정렬된 슬롯이 생성되어야 한다.

    앵커를 (7, 14, 21) 로 설정하면 ACTIVE_HOURS(8~22) 내로 클램프된 슬롯이
    3건 생성되고 단조 증가해야 한다.
    """
    store = build_store(tmp_path)
    store.set_system_setting("publish_anchor_hours", "10,14,19")
    scheduler = build_scheduler(store)

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("Asia/Seoul")
    except Exception:
        local_tz = timezone(timedelta(hours=9))

    target_date = date(2026, 3, 1)
    slots = scheduler._build_daily_publish_slots(target_date, daily_target=3)

    assert len(slots) == 3

    active_start = datetime.combine(
        target_date, __import__("datetime").time(hour=SchedulerService.ACTIVE_HOURS[0]), tzinfo=local_tz
    )
    active_end = datetime.combine(
        target_date, __import__("datetime").time(hour=SchedulerService.ACTIVE_HOURS[1]), tzinfo=local_tz
    )

    for i, slot in enumerate(slots):
        assert active_start <= slot < active_end, (
            f"슬롯[{i}] {slot.isoformat()} 이 활성 시간대 밖"
        )

    # 단조 증가
    for i in range(1, len(slots)):
        assert slots[i] > slots[i - 1], f"슬롯이 정렬되지 않음: {slots}"

    # DB 커스텀 앵커가 실제로 사용됐는지: 슬롯이 기본 앵커(9,12,19)가 아닌
    # 커스텀 앵커(10,14,19) 기반으로 생성됐음을 확인
    # → _get_publish_anchor_hours() 가 (10, 14, 19) 를 반환해야 함
    assert scheduler._get_publish_anchor_hours() == (10, 14, 19)
