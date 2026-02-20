from __future__ import annotations

from modules.automation.resource_monitor import CpuHysteresisMonitor


def test_cpu_hysteresis_monitor_state_transitions():
    """이동평균 + 히스테리시스가 과도한 토글 없이 동작해야 한다."""
    samples = iter([40.0, 37.0, 34.0, 30.0, 27.0, 26.0, 29.0, 36.0, 42.0])
    monitor = CpuHysteresisMonitor(
        start_threshold_percent=28.0,
        stop_threshold_percent=35.0,
        sample_window=3,
        sampler=lambda: next(samples, None),
    )

    states = []
    for _ in range(9):
        enabled, _ = monitor.check()
        states.append(enabled)

    # 평균이 충분히 내려가기 전까지는 정지 상태 유지
    assert states[0] is False
    assert states[3] is False
    # 28% 이하 구간 진입 시 시작
    assert states[5] is True
    # 35% 이상으로 올라갈 때까지 유지
    assert states[7] is True
    # 상한 초과 시 정지
    assert states[8] is False


def test_cpu_hysteresis_monitor_graceful_when_sampler_missing():
    """샘플러가 실패해도 예외 없이 안전하게 False를 반환해야 한다."""
    monitor = CpuHysteresisMonitor(
        start_threshold_percent=28.0,
        stop_threshold_percent=35.0,
        sample_window=5,
        sampler=lambda: None,
    )

    enabled, avg = monitor.check()
    assert enabled is False
    assert avg == 100.0
