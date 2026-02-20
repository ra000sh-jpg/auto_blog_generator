"""리소스 모니터링 유틸리티."""

from __future__ import annotations

import logging
import os
from collections import deque
from statistics import mean
from typing import Callable, Deque, Optional, Tuple

logger = logging.getLogger(__name__)


class CpuHysteresisMonitor:
    """CPU 이동평균 + 히스테리시스 기반 생성 허용 판단기."""

    def __init__(
        self,
        start_threshold_percent: float = 28.0,
        stop_threshold_percent: float = 35.0,
        sample_window: int = 5,
        sampler: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        if start_threshold_percent >= stop_threshold_percent:
            raise ValueError("start threshold must be lower than stop threshold")

        self.start_threshold_percent = float(start_threshold_percent)
        self.stop_threshold_percent = float(stop_threshold_percent)
        self.sample_window = max(3, int(sample_window))
        self._samples: Deque[float] = deque(maxlen=self.sample_window)
        self._generation_enabled = False
        self._sampler = sampler or self._sample_cpu_percent
        self._missing_warning_logged = False
        self._source: str = "unknown"

    @property
    def generation_enabled(self) -> bool:
        """현재 생성 허용 상태."""
        return self._generation_enabled

    @property
    def source(self) -> str:
        """CPU 측정 소스."""
        return self._source

    def check(self) -> Tuple[bool, float]:
        """현재 CPU 샘플을 반영해 생성 허용 여부를 계산한다."""
        sample = self._sampler()
        if sample is None:
            if not self._missing_warning_logged:
                logger.warning(
                    "CPU sampler unavailable; generator will pause until valid sample appears"
                )
                self._missing_warning_logged = True
            return False, 100.0

        if not self._missing_warning_logged:
            pass
        else:
            logger.info("CPU sampler recovered")
            self._missing_warning_logged = False

        bounded = max(0.0, min(100.0, float(sample)))
        self._samples.append(bounded)
        avg_percent = mean(self._samples)

        previous_state = self._generation_enabled
        if self._generation_enabled:
            # 실행 중에는 상한을 넘을 때만 멈춘다.
            if avg_percent >= self.stop_threshold_percent:
                self._generation_enabled = False
        else:
            # 정지 중에는 하한 미만일 때만 다시 시작한다.
            if avg_percent <= self.start_threshold_percent:
                self._generation_enabled = True

        if previous_state != self._generation_enabled:
            logger.info(
                "CPU hysteresis state changed",
                extra={
                    "enabled": self._generation_enabled,
                    "avg_percent": round(avg_percent, 2),
                    "start_threshold": self.start_threshold_percent,
                    "stop_threshold": self.stop_threshold_percent,
                    "source": self._source,
                },
            )

        return self._generation_enabled, float(avg_percent)

    def _sample_cpu_percent(self) -> Optional[float]:
        """CPU 사용률 샘플을 가져온다."""
        try:
            import psutil  # type: ignore[import-untyped]

            self._source = "psutil"
            return float(psutil.cpu_percent(interval=0.2))
        except Exception:
            # psutil 미설치/실패 시 loadavg로 폴백한다.
            try:
                load_avg, _, _ = os.getloadavg()
                cpu_count = os.cpu_count() or 1
                self._source = "loadavg"
                return (load_avg / cpu_count) * 100.0
            except Exception:
                self._source = "unavailable"
                return None
