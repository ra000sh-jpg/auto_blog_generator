"""퍼블리셔 플러그인 레지스트리."""

from __future__ import annotations

from typing import Dict, Type

PUBLISHER_REGISTRY: Dict[str, Type] = {}


def register_publisher(platform: str):
    """데코레이터: @register_publisher("naver")."""

    def decorator(cls):
        normalized = str(platform or "").strip().lower()
        if normalized:
            PUBLISHER_REGISTRY[normalized] = cls
        return cls

    return decorator


def get_publisher(platform: str):
    """플랫폼 키로 퍼블리셔 클래스를 조회한다."""
    normalized = str(platform or "").strip().lower()
    if normalized not in PUBLISHER_REGISTRY:
        raise ValueError(f"Unsupported platform: {platform}")
    return PUBLISHER_REGISTRY[normalized]
