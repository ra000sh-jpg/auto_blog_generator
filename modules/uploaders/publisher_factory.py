"""채널 정보 기반 퍼블리셔 팩토리."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Dict, Set
from urllib.parse import urlparse

from .base_publisher import BasePublisher
from .publisher_registry import PUBLISHER_REGISTRY, get_publisher as get_registered_publisher

_AUTOLOADED = False


def _autoload_publishers() -> None:
    """현재 패키지의 퍼블리셔 모듈을 자동 import한다."""
    global _AUTOLOADED
    if _AUTOLOADED:
        return

    package_name = __package__ or "modules.uploaders"
    package_dir = Path(__file__).resolve().parent
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        module_name = str(module_info.name)
        if not module_name.endswith("_publisher"):
            continue
        if module_name in {"base_publisher", "playwright_publisher"}:
            continue
        importlib.import_module(f"{package_name}.{module_name}")

    _AUTOLOADED = True


def extract_blog_id(blog_url: str) -> str:
    """네이버 blog_url에서 blog_id를 추출한다."""
    text = str(blog_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"

    parsed = urlparse(text)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        return path_parts[0]
    return ""


def get_supported_platforms() -> Set[str]:
    """레지스트리 기반 지원 플랫폼 목록을 반환한다."""
    _autoload_publishers()
    return set(PUBLISHER_REGISTRY.keys())


def get_publisher(channel: Dict[str, Any]) -> BasePublisher:
    """채널 플랫폼에 맞는 퍼블리셔를 생성한다."""
    _autoload_publishers()
    platform = str(channel.get("platform", "")).strip().lower()
    publisher_cls = get_registered_publisher(platform)

    try:
        return publisher_cls(channel=channel)
    except TypeError:
        # channel 인자를 지원하지 않는 구현체와의 호환성 fallback.
        return publisher_cls()
