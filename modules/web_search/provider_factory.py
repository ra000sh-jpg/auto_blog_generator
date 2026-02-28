"""웹 검색 프로바이더 팩토리."""

from __future__ import annotations

import logging
from typing import List, Optional

from .base_client import BaseWebSearchClient
from .brave_client import BraveSearchClient

logger = logging.getLogger(__name__)


def create_web_search_client(
    provider: str = "brave",
    api_key: str = "",
    timeout_sec: float = 10.0,
    allowed_domains: Optional[List[str]] = None,
    blocked_domains: Optional[List[str]] = None,
    cache_ttl_sec: int = 86400 * 7,
) -> Optional[BaseWebSearchClient]:
    """프로바이더에 따른 웹 검색 클라이언트를 생성한다."""
    normalized = str(provider).strip().lower()
    if not api_key:
        logger.info("Web search disabled: no API key for provider=%s", normalized)
        return None

    if normalized == "brave":
        return BraveSearchClient(
            api_key=api_key,
            timeout_sec=timeout_sec,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            cache_ttl_sec=cache_ttl_sec,
        )

    logger.warning("Unknown web search provider '%s', fallback to brave", normalized)
    return BraveSearchClient(
        api_key=api_key,
        timeout_sec=timeout_sec,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        cache_ttl_sec=cache_ttl_sec,
    )
