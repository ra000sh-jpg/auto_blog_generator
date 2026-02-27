"""Brave Search API 클라이언트."""

from __future__ import annotations

import logging
from typing import List

import httpx

from .base_client import BaseWebSearchClient, SearchResult

logger = logging.getLogger(__name__)


class BraveSearchClient(BaseWebSearchClient):
    """Brave Search API v1 웹 검색 클라이언트."""

    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        api_key: str,
        timeout_sec: float = 10.0,
        country: str = "KR",
        search_lang: str = "ko",
    ) -> None:
        if not api_key:
            raise ValueError("Brave API key is required")
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        self._country = country
        self._search_lang = search_lang

    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[SearchResult]:
        """Brave Search API로 웹 검색을 수행한다."""
        if not query.strip():
            return []

        params = {
            "q": query.strip(),
            "count": min(max_results, 20),
            "country": self._country,
            "search_lang": self._search_lang,
            "text_decorations": "false",
        }

        try:
            response = await self._client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Brave Search HTTP error: status=%s",
                exc.response.status_code,
            )
            return []
        except Exception as exc:
            logger.warning("Brave Search request failed: %s", exc)
            return []

        results: List[SearchResult] = []
        raw_results = data.get("web", {}).get("results", [])
        for rank, item in enumerate(raw_results[:max_results], start=1):
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("description", "")).strip()
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source="brave",
                    rank=rank,
                )
            )
        return results

    async def close(self) -> None:
        """내부 클라이언트를 정리한다."""
        await self._client.aclose()
