"""웹 검색/본문 추출 클라이언트 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SearchResult:
    """단일 웹 검색 결과."""

    title: str
    url: str
    snippet: str
    source: str = ""
    rank: int = 0
    score: float = 0.0  # Phase 1.5: 품질 점수 (0.0 ~ 1.0)


class BaseWebSearchClient(ABC):
    """웹 검색 프로바이더 인터페이스."""

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[SearchResult]:
        """키워드 검색을 수행하고 결과 목록을 반환한다."""

    @abstractmethod
    async def close(self) -> None:
        """내부 클라이언트를 정리한다."""


class BaseWebFetchClient(ABC):
    """URL 본문 추출 인터페이스."""

    @abstractmethod
    async def fetch_content(
        self,
        url: str,
        max_chars: int = 3000,
    ) -> Optional[Dict[str, str]]:
        """URL의 본문 텍스트를 추출한다."""

    @abstractmethod
    async def close(self) -> None:
        """내부 클라이언트를 정리한다."""
