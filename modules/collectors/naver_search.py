"""네이버 OpenAPI 검색용 경량 collector."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class NaverSearchItem:
    """네이버 검색 결과 1건."""

    title: str
    link: str
    description: str
    source: str
    thumbnail: str = ""


class NaverSearchFetcher(Protocol):
    """네이버 검색 HTTP fetcher 프로토콜."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_sec: float = 6.0,
    ) -> dict:
        """URL의 JSON 응답을 반환한다."""


class UrllibNaverSearchFetcher:
    """추가 의존성 없이 동작하는 기본 fetcher."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_sec: float = 6.0,
    ) -> dict:
        request = urllib.request.Request(url, headers=dict(headers))
        ssl_context = _build_ssl_context()
        if ssl_context is None:
            response_cm = urllib.request.urlopen(request, timeout=timeout_sec)  # nosec B310
        else:
            response_cm = urllib.request.urlopen(
                request,
                timeout=timeout_sec,
                context=ssl_context,
            )  # nosec B310
        with response_cm as response:
            raw = response.read(1_000_000).decode("utf-8", errors="replace")
        payload = json.loads(raw or "{}")
        return payload if isinstance(payload, dict) else {}


class NaverSearchCollector:
    """네이버 블로그/뉴스/이미지 검색 컨텍스트를 수집한다."""

    API_BASE = "https://openapi.naver.com/v1/search"
    SERVICES = {"blog", "news", "image", "webkr"}

    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str = "",
        fetcher: NaverSearchFetcher | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float = 6.0,
    ) -> None:
        resolved_env = env if env is not None else os.environ
        self.client_id = str(client_id or resolved_env.get("NAVER_CLIENT_ID", "")).strip()
        self.client_secret = str(client_secret or resolved_env.get("NAVER_CLIENT_SECRET", "")).strip()
        self.fetcher = fetcher or UrllibNaverSearchFetcher()
        self.timeout_sec = timeout_sec

    @property
    def enabled(self) -> bool:
        """네이버 검색 API 호출 가능 여부."""

        return bool(self.client_id and self.client_secret)

    def search(
        self,
        query: str,
        *,
        service: str = "blog",
        display: int = 5,
        sort: str = "sim",
    ) -> list[NaverSearchItem]:
        """지정 서비스에서 검색 결과를 반환한다."""

        if not self.enabled:
            return []

        normalized_service = str(service or "blog").strip().lower()
        if normalized_service not in self.SERVICES:
            normalized_service = "blog"

        params = urllib.parse.urlencode(
            {
                "query": str(query or "").strip(),
                "display": max(1, min(20, int(display or 5))),
                "sort": str(sort or "sim").strip(),
            }
        )
        if not str(query or "").strip():
            return []

        url = f"{self.API_BASE}/{normalized_service}.json?{params}"
        payload = self.fetcher.get_json(
            url,
            headers={
                "X-Naver-Client-Id": self.client_id,
                "X-Naver-Client-Secret": self.client_secret,
            },
            timeout_sec=self.timeout_sec,
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            return []
        return [
            _parse_search_item(item, source=f"Naver {normalized_service}")
            for item in items
            if isinstance(item, dict)
        ]

    def collect_context(
        self,
        query: str,
        *,
        services: tuple[str, ...] = ("news", "blog"),
        per_service: int = 3,
    ) -> list[dict[str, str]]:
        """LLM 컨텍스트에 넣기 쉬운 dict 목록으로 검색 결과를 반환한다."""

        contexts: list[dict[str, str]] = []
        seen_links: set[str] = set()
        for service in services:
            for item in self.search(query, service=service, display=per_service):
                if item.link in seen_links:
                    continue
                seen_links.add(item.link)
                contexts.append(
                    {
                        "title": item.title,
                        "link": item.link,
                        "content": item.description,
                        "source": item.source,
                    }
                )
        return contexts


def _parse_search_item(item: dict, *, source: str) -> NaverSearchItem:
    """네이버 검색 응답 item을 내부 모델로 정리한다."""

    return NaverSearchItem(
        title=_clean_html(str(item.get("title", "") or "")),
        link=str(item.get("link", "") or "").strip(),
        description=_clean_html(str(item.get("description", "") or "")),
        source=source,
        thumbnail=str(item.get("thumbnail", "") or "").strip(),
    )


def _clean_html(text: str) -> str:
    """네이버 검색 결과의 간단한 HTML 태그/엔티티를 제거한다."""

    cleaned = re.sub(r"<[^>]+>", "", str(text or ""))
    return (
        cleaned.replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .strip()
    )


def _build_ssl_context() -> ssl.SSLContext | None:
    """macOS/Python 인증서 체인 문제를 피하기 위해 certifi CA를 우선 사용한다."""

    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None
