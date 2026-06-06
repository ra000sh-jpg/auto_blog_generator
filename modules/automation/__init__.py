"""자동화 모듈 공개 API.

무거운 스케줄러/수집기 의존성은 실제 접근 시점에만 불러온다.
"""

from .time_utils import now_utc, to_utc, to_kst, parse_iso
from .job_store import JobStore

__all__ = [
    "now_utc",
    "to_utc",
    "to_kst",
    "parse_iso",
    "JobStore",
    "TrendJobService",
    "CATEGORY_TO_TOPIC",
    "SchedulerService",
    "Worker",
]


def __getattr__(name: str):
    """선택 의존성이 필요한 객체를 지연 import한다."""

    if name in {"TrendJobService", "CATEGORY_TO_TOPIC"}:
        from .trend_job_service import CATEGORY_TO_TOPIC, TrendJobService

        return {"TrendJobService": TrendJobService, "CATEGORY_TO_TOPIC": CATEGORY_TO_TOPIC}[name]
    if name == "SchedulerService":
        from .scheduler_service import SchedulerService

        return SchedulerService
    if name == "Worker":
        from .worker import Worker

        return Worker
    raise AttributeError(name)
