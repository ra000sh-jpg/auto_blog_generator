"""
시간 유틸리티 모듈 - UTC 표준화

모든 timestamp는 UTC ISO 8601 형식으로 저장:
- 저장 형식: 2026-02-21T00:00:00Z
- 표시 시 KST (+09:00)로 변환

P0 #5 해결: datetime('now') 대신 now_utc() 사용
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

# 한국 표준시 (KST = UTC+9)
KST = timezone(timedelta(hours=9))


def now_utc() -> str:
    """
    현재 시각을 UTC ISO 8601 형식으로 반환.

    Returns:
        str: "2026-02-19T15:30:00Z" 형식

    Example:
        >>> now_utc()
        '2026-02-19T15:30:00Z'
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc(dt: datetime) -> str:
    """
    datetime 객체를 UTC ISO 8601 문자열로 변환.

    naive datetime은 KST로 가정하고 UTC로 변환.
    aware datetime은 UTC로 변환.

    Args:
        dt: datetime 객체

    Returns:
        str: UTC ISO 8601 문자열

    Example:
        >>> from datetime import datetime
        >>> kst_time = datetime(2026, 2, 21, 9, 0, 0)  # KST 09:00
        >>> to_utc(kst_time)
        '2026-02-21T00:00:00Z'  # UTC 00:00
    """
    if dt.tzinfo is None:
        # naive datetime은 KST로 가정
        dt = dt.replace(tzinfo=KST)

    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_kst(utc_str: str) -> datetime:
    """
    UTC ISO 문자열을 KST datetime으로 변환.

    Args:
        utc_str: "2026-02-21T00:00:00Z" 형식

    Returns:
        datetime: KST timezone aware datetime

    Example:
        >>> to_kst("2026-02-21T00:00:00Z")
        datetime(2026, 2, 21, 9, 0, 0, tzinfo=KST)
    """
    utc_dt = parse_iso(utc_str)
    return utc_dt.astimezone(KST)


def parse_iso(iso_str: str) -> datetime:
    """
    ISO 8601 문자열을 datetime으로 파싱.

    지원 형식:
    - 2026-02-21T00:00:00Z (UTC)
    - 2026-02-21T09:00:00+09:00 (KST)
    - 2026-02-21T00:00:00 (naive, UTC로 가정)

    Args:
        iso_str: ISO 8601 형식 문자열

    Returns:
        datetime: UTC timezone aware datetime
    """
    # Z 접미사 처리
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        # 대체 포맷 시도
        for fmt in [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]:
            try:
                dt = datetime.strptime(iso_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Cannot parse ISO string: {iso_str}")

    # naive datetime은 UTC로 가정
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def add_seconds(utc_str: str, seconds: int) -> str:
    """
    UTC 시간에 초를 더해서 새 UTC 문자열 반환.

    Args:
        utc_str: 기준 UTC ISO 문자열
        seconds: 더할 초 (음수 가능)

    Returns:
        str: 새 UTC ISO 문자열

    Example:
        >>> add_seconds("2026-02-21T00:00:00Z", 300)
        '2026-02-21T00:05:00Z'
    """
    dt = parse_iso(utc_str)
    new_dt = dt + timedelta(seconds=seconds)
    return new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_past(utc_str: str, reference: Optional[str] = None) -> bool:
    """
    주어진 UTC 시간이 기준 시간보다 과거인지 확인.

    Args:
        utc_str: 확인할 UTC ISO 문자열
        reference: 기준 시간 (None이면 현재 시각)

    Returns:
        bool: 과거이면 True
    """
    target = parse_iso(utc_str)
    ref = parse_iso(reference) if reference else datetime.now(timezone.utc)
    return target <= ref


def format_kst_display(utc_str: str) -> str:
    """
    UTC 시간을 KST로 변환하여 읽기 쉬운 형식으로 반환.

    Args:
        utc_str: UTC ISO 문자열

    Returns:
        str: "2026-02-21 09:00 KST" 형식
    """
    kst_dt = to_kst(utc_str)
    return kst_dt.strftime("%Y-%m-%d %H:%M KST")


def calculate_retry_delay(
    retry_count: int,
    base_delays: Optional[list[int]] = None,
) -> int:
    """
    재시도 횟수에 따른 대기 시간 계산 (Exponential Backoff + Jitter).

    Args:
        retry_count: 현재 재시도 횟수 (0-based)
        base_delays: 기본 대기 시간 리스트 (초)

    Returns:
        int: 대기 시간 (초), Jitter 적용됨

    Example:
        >>> calculate_retry_delay(0)  # 첫 번째 재시도
        3~5초 범위 (±50% jitter)
    """
    import random

    if base_delays is None:
        base_delays = [3, 10, 60, 180, 300]  # 3초, 10초, 1분, 3분, 5분

    # 인덱스 범위 제한
    idx = min(retry_count, len(base_delays) - 1)
    base = base_delays[idx]

    # Full Jitter: 0 ~ 2*base 범위
    # AWS 권장: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    jitter = random.uniform(0.5, 1.5)
    return int(base * jitter)
