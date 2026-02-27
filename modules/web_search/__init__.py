"""웹 검색 및 본문 추출 모듈."""

from .base_client import BaseWebFetchClient, BaseWebSearchClient, SearchResult
from .provider_factory import create_web_search_client
from .web_fetch_client import WebFetchClient

__all__ = [
    "BaseWebFetchClient",
    "BaseWebSearchClient",
    "SearchResult",
    "WebFetchClient",
    "create_web_search_client",
]
