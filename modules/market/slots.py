"""국장/미장 브리핑 슬롯과 휴장일 대체 규칙."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Dict, List
from zoneinfo import ZoneInfo


class BlogSlot(str, Enum):
    """하루 3편 운영 슬롯."""

    KR_PREOPEN = "KR_PREOPEN"
    US_PREOPEN = "US_PREOPEN"
    EVERGREEN_INSIGHT = "EVERGREEN_INSIGHT"
    WEEKLY_REFLECTION = "WEEKLY_REFLECTION"


@dataclass(frozen=True)
class MarketOpenState:
    """시장 개장 여부.

    실제 휴장일 판정은 KRX/미국장 캘린더 수집기가 채우고, 이 모듈은
    그 결과를 바탕으로 슬롯만 결정한다.
    """

    krx_open: bool
    us_open: bool
    is_weekend: bool = False


def get_default_daily_slots() -> List[BlogSlot]:
    """기본 하루 3편 슬롯을 반환한다."""

    return [
        BlogSlot.KR_PREOPEN,
        BlogSlot.US_PREOPEN,
        BlogSlot.EVERGREEN_INSIGHT,
    ]


def resolve_daily_slots(state: MarketOpenState) -> Dict[BlogSlot, BlogSlot]:
    """휴장/주말 여부에 따라 실제 작성 슬롯을 결정한다."""

    if state.is_weekend:
        return {
            BlogSlot.KR_PREOPEN: BlogSlot.EVERGREEN_INSIGHT,
            BlogSlot.US_PREOPEN: BlogSlot.EVERGREEN_INSIGHT,
            BlogSlot.EVERGREEN_INSIGHT: BlogSlot.WEEKLY_REFLECTION,
        }

    resolved = {
        BlogSlot.KR_PREOPEN: BlogSlot.KR_PREOPEN,
        BlogSlot.US_PREOPEN: BlogSlot.US_PREOPEN,
        BlogSlot.EVERGREEN_INSIGHT: BlogSlot.EVERGREEN_INSIGHT,
    }
    if not state.krx_open:
        resolved[BlogSlot.KR_PREOPEN] = BlogSlot.EVERGREEN_INSIGHT
    if not state.us_open:
        resolved[BlogSlot.US_PREOPEN] = BlogSlot.EVERGREEN_INSIGHT
    return resolved


def get_us_preopen_kst(
    trading_date: date,
    *,
    minutes_before_open: int = 120,
) -> datetime:
    """미국 정규장 개장 전 시각을 한국시간으로 반환한다.

    미국 정규장 09:30 ET를 기준으로 계산해 서머타임 전환 오차를 피한다.
    """

    eastern = ZoneInfo("America/New_York")
    seoul = ZoneInfo("Asia/Seoul")
    open_et = datetime.combine(trading_date, time(hour=9, minute=30), tzinfo=eastern)
    preopen_et = open_et.replace() - _minutes_delta(minutes_before_open)
    return preopen_et.astimezone(seoul)


def _minutes_delta(minutes: int):
    # datetime.timedelta를 import 위치에서 드러내지 않도록 작은 헬퍼로 둔다.
    from datetime import timedelta

    return timedelta(minutes=max(0, int(minutes)))
