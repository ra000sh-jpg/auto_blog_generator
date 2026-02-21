from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from modules.collectors.rss_news_collector import RssNewsCollector
import modules.collectors.rss_news_collector as rss_module


class FakeFeedParser:
    """feedparser.parse 응답을 대체하는 테스트 더블."""

    def __init__(self, by_url: Dict[str, List[Dict[str, Any]]]):
        self.by_url = by_url

    def parse(self, url: str) -> Dict[str, Any]:
        return {"entries": self.by_url.get(url, [])}


def test_fetch_relevant_news_filters_and_sorts(monkeypatch: pytest.MonkeyPatch):
    """키워드/시간 조건을 적용해 최신순으로 반환해야 한다."""
    feed_urls = ["https://feed-a", "https://feed-b"]
    collector = RssNewsCollector(feed_urls=feed_urls, max_content_chars=500)

    # 테스트 기준 시각 고정
    fixed_now = datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(collector, "_get_now_utc", lambda: fixed_now)

    fake_entries = {
        "https://feed-a": [
            {
                "title": "기준금리 동결 전망",
                "link": "https://news/1",
                "summary": "<p>금리와 환율 이슈</p>",
                "published_parsed": (2026, 2, 20, 10, 0, 0, 0, 0, 0),
            },
            {
                "title": "오래된 기사",
                "link": "https://news/old",
                "summary": "<p>지난주 기사</p>",
                "published_parsed": (2026, 2, 17, 10, 0, 0, 0, 0, 0),
            },
        ],
        "https://feed-b": [
            {
                "title": "환율 변동성 확대",
                "link": "https://news/2",
                "summary": "<p>달러 환율 상승</p>",
                "published_parsed": (2026, 2, 20, 11, 0, 0, 0, 0, 0),
            },
            {
                "title": "중복 링크 기사",
                "link": "https://news/2",
                "summary": "<p>중복</p>",
                "published_parsed": (2026, 2, 20, 11, 30, 0, 0, 0, 0),
            },
        ],
    }

    monkeypatch.setattr(rss_module, "feedparser", FakeFeedParser(fake_entries))
    monkeypatch.setattr(collector, "_fetch_article_text", lambda _url: "기사 본문 텍스트")

    items = collector.fetch_relevant_news(
        keywords=["금리", "환율"],
        within_hours=24,
        max_items=3,
    )

    assert len(items) == 2
    assert items[0]["link"] == "https://news/2"  # 최신 기사 우선
    assert items[1]["link"] == "https://news/1"
    assert items[0]["content"] == "기사 본문 텍스트"

    collector.close()


def test_fetch_relevant_news_returns_empty_when_feedparser_missing(monkeypatch: pytest.MonkeyPatch):
    """feedparser 미설치 환경에서도 안전하게 빈 결과를 반환해야 한다."""
    collector = RssNewsCollector(feed_urls=["https://feed-a"])
    monkeypatch.setattr(rss_module, "feedparser", None)

    items = collector.fetch_relevant_news(keywords=["경제"])
    assert items == []

    collector.close()
