"""URL 본문 텍스트 추출 클라이언트."""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

import httpx

from .base_client import BaseWebFetchClient

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    BeautifulSoup = None


class WebFetchClient(BaseWebFetchClient):
    """URL에서 본문 텍스트를 추출하는 클라이언트."""

    def __init__(
        self,
        timeout_sec: float = 15.0,
        max_chars: int = 3000,
    ) -> None:
        self._max_chars = max_chars
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            },
        )

    async def fetch_content(
        self,
        url: str,
        max_chars: int = 3000,
    ) -> Optional[Dict[str, str]]:
        """URL 본문 텍스트를 추출한다."""
        effective_max = max_chars if max_chars > 0 else self._max_chars
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Web fetch failed (%s): %s", url, exc)
            return None

        html = response.text
        if BeautifulSoup is None:
            return {
                "title": "",
                "url": url,
                "content": self._truncate(self._strip_tags(html), effective_max),
            }

        try:
            soup = BeautifulSoup(html, "html.parser")
            for selector in ("script", "style", "nav", "footer", "header", "aside", "form"):
                for node in soup.select(selector):
                    node.decompose()

            title_tag = soup.find("title")
            page_title = self._clean_text(title_tag.get_text(strip=True)) if title_tag else ""

            article_node = soup.find("article")
            target = article_node if article_node else soup.body
            if target is None:
                return None

            blocks = [
                self._clean_text(node.get_text(" ", strip=True))
                for node in target.find_all(["p", "li"])
            ]
            text = " ".join(item for item in blocks if item)
            if not text:
                text = self._clean_text(target.get_text(" ", strip=True))
            if not text:
                return None

            return {
                "title": page_title,
                "url": url,
                "content": self._truncate(text, effective_max),
            }
        except Exception as exc:
            logger.debug("Web parse failed (%s): %s", url, exc)
            return None

    async def close(self) -> None:
        """내부 클라이언트를 정리한다."""
        await self._client.aclose()

    def _strip_tags(self, html: str) -> str:
        return self._clean_text(re.sub(r"<[^>]+>", " ", html))

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _truncate(self, text: str, max_chars: int) -> str:
        cleaned = self._clean_text(text)
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars].rstrip() + "..."
