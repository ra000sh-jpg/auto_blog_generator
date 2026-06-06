import urllib.parse

from modules.collectors.naver_search import NaverSearchCollector


class FakeNaverFetcher:
    """네트워크 없이 네이버 검색 응답을 검증하는 fetcher."""

    def __init__(self):
        self.calls = []

    def get_json(self, url, *, headers, timeout_sec=6.0):
        self.calls.append({"url": url, "headers": dict(headers), "timeout_sec": timeout_sec})
        return {
            "items": [
                {
                    "title": "<b>국장</b> 브리핑",
                    "link": "https://example.com/a",
                    "description": "환율 &amp; 반도체 흐름",
                }
            ]
        }


def test_naver_search_collector_requires_official_keys():
    """공식 키가 없으면 네이버 검색 호출을 하지 않는다."""

    fetcher = FakeNaverFetcher()
    collector = NaverSearchCollector(fetcher=fetcher, env={})

    assert collector.enabled is False
    assert collector.search("국장 브리핑") == []
    assert fetcher.calls == []


def test_naver_search_collector_cleans_html_and_sets_headers():
    """네이버 검색 결과의 HTML을 정리하고 인증 헤더를 넣는다."""

    fetcher = FakeNaverFetcher()
    collector = NaverSearchCollector(
        fetcher=fetcher,
        env={
            "NAVER_CLIENT_ID": "client-id",
            "NAVER_CLIENT_SECRET": "client-secret",
        },
    )

    items = collector.search("국장 브리핑", service="news", display=1)

    assert len(items) == 1
    assert items[0].title == "국장 브리핑"
    assert items[0].description == "환율 & 반도체 흐름"
    assert fetcher.calls[0]["headers"]["X-Naver-Client-Id"] == "client-id"
    assert fetcher.calls[0]["headers"]["X-Naver-Client-Secret"] == "client-secret"
    parsed = urllib.parse.urlparse(fetcher.calls[0]["url"])
    assert parsed.path.endswith("/news.json")
    assert "query=" in parsed.query
