"""경제 뉴스 RSS 수집기.

무료 RSS 피드에서 최신 기사를 수집하고, 키워드 관련 기사만 골라
RAG 컨텍스트로 사용할 수 있는 텍스트를 반환한다.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

try:
    import feedparser  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - 의존성 미설치 환경 보호
    feedparser = None

try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - 의존성 미설치 환경 보호
    BeautifulSoup = None

logger = logging.getLogger(__name__)


class RssNewsCollector:
    """RSS 뉴스 수집 및 키워드 필터링."""

    DEFAULT_FEED_URLS: List[str] = [
        "https://www.mk.co.kr/rss/30100041/",
        "https://www.hankyung.com/feed/economy",
        # "https://www.fsc.go.kr/no010101?srchCtgry=&curPage=&srchKey=&srchText=&sort=&srchBeginDt=&srchEndDt=&srchWriter=&rss=Y",
    ]

    def __init__(
        self,
        feed_urls: Optional[Sequence[str]] = None,
        request_timeout_sec: float = 8.0,
        max_content_chars: int = 2000,
    ):
        self.feed_urls = list(feed_urls) if feed_urls else list(self.DEFAULT_FEED_URLS)
        self.request_timeout_sec = request_timeout_sec
        self.max_content_chars = max_content_chars
        self._http_client = httpx.Client(
            timeout=request_timeout_sec,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            },
        )

    def close(self) -> None:
        """내부 HTTP 클라이언트를 정리한다."""
        self._http_client.close()

    def fetch_relevant_news(
        self,
        keywords: Sequence[str],
        feed_urls: Optional[Sequence[str]] = None,
        within_hours: int = 24,
        max_items: int = 3,
    ) -> List[Dict[str, str]]:
        """키워드와 관련된 최신 뉴스 목록을 반환한다."""
        if feedparser is None:
            logger.warning("feedparser is not installed. RSS collection skipped.")
            return []

        source_urls = list(feed_urls) if feed_urls else self.feed_urls
        if not source_urls:
            return []

        now_utc = self._get_now_utc()
        oldest_allowed = now_utc - timedelta(hours=max(1, within_hours))
        normalized_keywords = self._normalize_keywords(keywords)

        candidates: List[Tuple[datetime, Dict[str, str]]] = []
        seen_links: set[str] = set()

        for feed_url in source_urls:
            try:
                parsed_feed = feedparser.parse(feed_url)
                entries = self._extract_entries(parsed_feed)
            except Exception as exc:
                logger.warning("RSS parse failed (%s): %s", feed_url, exc)
                continue

            for entry in entries:
                published_at = self._extract_published_at(entry)
                if published_at and published_at < oldest_allowed:
                    continue

                title = self._clean_text(str(entry.get("title", "")))
                link = str(entry.get("link", "")).strip()
                summary_text = self._extract_text(str(entry.get("summary", "") or entry.get("description", "")))
                relevance_text = f"{title} {summary_text}"

                if not self._is_relevant(relevance_text, normalized_keywords):
                    continue
                if not title or not link or link in seen_links:
                    continue

                article_text = self._fetch_article_text(link)
                content = self._truncate_text(article_text or summary_text, self.max_content_chars)
                if not content:
                    continue

                candidates.append(
                    (
                        published_at or now_utc,
                        {
                            "title": title,
                            "link": link,
                            "content": content,
                        },
                    )
                )
                seen_links.add(link)

        # 최신 기사 우선 정렬
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in candidates[: max(1, max_items)]]

    def _extract_entries(self, parsed_feed: Any) -> List[Dict[str, Any]]:
        """feedparser 결과에서 entries를 안전하게 추출한다."""
        entries = getattr(parsed_feed, "entries", None)
        if entries is None and isinstance(parsed_feed, dict):
            entries = parsed_feed.get("entries", [])
        return list(entries or [])

    def _extract_published_at(self, entry: Dict[str, Any]) -> Optional[datetime]:
        """기사 발행시각을 UTC datetime으로 변환한다."""
        raw_time = entry.get("published_parsed") or entry.get("updated_parsed")
        if raw_time:
            try:
                return datetime(*raw_time[:6], tzinfo=timezone.utc)
            except Exception:
                pass

        raw_text = str(entry.get("published", "") or entry.get("updated", "")).strip()
        if not raw_text:
            return None

        try:
            parsed = parsedate_to_datetime(raw_text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    def _fetch_article_text(self, url: str) -> str:
        """기사 본문을 가져와 불필요한 태그를 제거한 텍스트를 반환한다."""
        try:
            response = self._http_client.get(url)
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Article fetch failed (%s): %s", url, exc)
            return ""

        html = response.text
        if BeautifulSoup is None:
            # bs4 미설치 환경에서는 간단한 태그 제거로 폴백한다.
            return self._extract_text(html)

        try:
            soup = BeautifulSoup(html, "html.parser")
            for selector in ("script", "style", "nav", "footer", "header", "aside", "form"):
                for node in soup.select(selector):
                    node.decompose()

            article_node = soup.find("article")
            target = article_node if article_node else soup.body
            if target is None:
                return ""

            blocks = [
                self._clean_text(node.get_text(" ", strip=True))
                for node in target.find_all(["p", "li"])
            ]
            text = " ".join(item for item in blocks if item)
            if not text:
                text = self._clean_text(target.get_text(" ", strip=True))
            return self._truncate_text(text, self.max_content_chars)
        except Exception as exc:
            logger.debug("Article parse failed (%s): %s", url, exc)
            return self._truncate_text(self._extract_text(html), self.max_content_chars)

    def _normalize_keywords(self, keywords: Sequence[str]) -> List[str]:
        """키워드 목록을 정규화한다."""
        normalized: List[str] = []
        for keyword in keywords:
            value = self._clean_text(str(keyword)).lower()
            if not value:
                continue
            normalized.append(value)
        return normalized

    def _is_relevant(self, text: str, keywords: Sequence[str]) -> bool:
        """키워드 매칭 여부를 판별한다."""
        if not keywords:
            return True
        haystack = text.lower()
        for keyword in keywords:
            if keyword and keyword in haystack:
                return True
            # 공백 키워드는 토큰 단위로도 검사한다.
            for token in keyword.split():
                if len(token) >= 2 and token in haystack:
                    return True
        return False

    def _extract_text(self, value: str) -> str:
        """HTML/텍스트 값을 평문으로 변환한다."""
        if not value:
            return ""

        if BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(value, "html.parser")
                return self._clean_text(soup.get_text(" ", strip=True))
            except Exception:
                pass

        # bs4 사용 불가 시 단순 태그 제거
        stripped = re.sub(r"<[^>]+>", " ", value)
        return self._clean_text(stripped)

    def _truncate_text(self, text: str, max_chars: int) -> str:
        """문자 수 제한에 맞춰 텍스트를 자른다."""
        cleaned = self._clean_text(text)
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars].rstrip() + "..."

    def _clean_text(self, text: str) -> str:
        """공백을 정리한 안전한 텍스트를 만든다."""
        return re.sub(r"\s+", " ", text).strip()

    def _get_now_utc(self) -> datetime:
        """현재 UTC 시각을 반환한다."""
        return datetime.now(timezone.utc)
