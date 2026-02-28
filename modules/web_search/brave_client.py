import hashlib
import json
import logging
import os
import sqlite3
import time
from urllib.parse import urlparse
from typing import List, Optional

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
        db_path: str = "data/search_cache.db",
        cache_ttl_sec: int = 86400 * 7,  # 기본 7일 캐시
        allowed_domains: List[str] = None,
        blocked_domains: List[str] = None,
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
        self._db_path = db_path
        self._cache_ttl_sec = cache_ttl_sec
        self._allowed_domains = [d.strip().lower() for d in (allowed_domains or []) if d.strip()]
        self._blocked_domains = [d.strip().lower() for d in (blocked_domains or []) if d.strip()]
        self._init_cache_db()

    def _init_cache_db(self) -> None:
        """SQLite 캐시 테이블 초기화."""
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS search_cache (
                        query_hash TEXT PRIMARY KEY,
                        results_json TEXT,
                        created_at INTEGER
                    )
                """)
        except Exception as exc:
            logger.warning("Search cache DB init failed: %s", exc)

    def _get_cached_results(self, query: str) -> Optional[List[SearchResult]]:
        """캐시된 검색 결과 조회."""
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        now = int(time.time())
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT results_json, created_at FROM search_cache WHERE query_hash = ?",
                    (query_hash,)
                ).fetchone()
                if row:
                    if now - row["created_at"] < self._cache_ttl_sec:
                        data = json.loads(row["results_json"])
                        return [SearchResult(**item) for item in data]
                    else:
                        conn.execute("DELETE FROM search_cache WHERE query_hash = ?", (query_hash,))
        except Exception as exc:
            logger.warning("Search cache read failed: %s", exc)
        return None

    def _save_cache(self, query: str, results: List[SearchResult]) -> None:
        """검색 결과 캐시 저장."""
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        now = int(time.time())
        try:
            results_data = [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "source": r.source,
                    "rank": r.rank,
                    "score": r.score
                } for r in results
            ]
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO search_cache (query_hash, results_json, created_at) VALUES (?, ?, ?)",
                    (query_hash, json.dumps(results_data, ensure_ascii=False), now)
                )
        except Exception as exc:
            logger.warning("Search cache save failed: %s", exc)

    def _calculate_score(self, item: dict, query: str) -> float:
        """검색 결과의 품질 점수 계산 (0.0 ~ 1.0)."""
        score = 0.5  # 기본 점수
        title = item.get("title", "").lower()
        snippet = item.get("description", "").lower()
        url = item.get("url", "").lower()
        query_terms = query.lower().split()

        # 1. 키워드 포함도 (제목/설명)
        matches = sum(1 for term in query_terms if term in title or term in snippet)
        if query_terms:
            score += (matches / len(query_terms)) * 0.3

        # 2. 도메인 신뢰도 및 필터
        domain = urlparse(url).netloc.lower()
        
        # 허용 도메인 가중치
        if any(ad in domain for ad in self._allowed_domains):
            score += 0.2
        
        # 차단 도메인 감점
        if any(bd in domain for bd in self._blocked_domains):
            score -= 0.5

        # 3. 최신성 보너스 (데이터에 날짜 정보가 있는 경우 - Brave API 확장 시 가능)
        # 현재는 기본값 유지

        return min(max(score, 0.0), 1.0)

    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[SearchResult]:
        """Brave Search API로 웹 검색을 수행한다 (캐시 적용)."""
        if not query.strip():
            return []

        # 1. 캐시 확인
        cached = self._get_cached_results(query)
        if cached:
            logger.info("Search cache hit for query: %s", query)
            return cached[:max_results]

        params = {
            "q": query.strip(),
            "count": min(max_results + 5, 20),  # 필터링을 고려해 조금 더 많이 가져옴
            "country": self._country,
            "search_lang": self._search_lang,
            "text_decorations": "false",
        }

        try:
            response = await self._client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("Brave Search request failed: %s", exc)
            return []

        results: List[SearchResult] = []
        raw_results = data.get("web", {}).get("results", [])
        
        for rank, item in enumerate(raw_results, start=1):
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("description", "")).strip()
            
            if not title or not url:
                continue
                
            # 스코어링 및 필터
            score = self._calculate_score(item, query)
            if score < 0.2:  # 낮은 점수 결과는 제외
                continue

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source="brave",
                    rank=rank,
                    score=score
                )
            )

        # 점수 순으로 정렬 후 상위 결과 반환
        sorted_results = sorted(results, key=lambda x: x.score, reverse=True)
        final_results = sorted_results[:max_results]
        
        # 캐시 저장
        if final_results:
            self._save_cache(query, final_results)
            
        return final_results

    async def close(self) -> None:
        """내부 클라이언트를 정리한다."""
        await self._client.aclose()
