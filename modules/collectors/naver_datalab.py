import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests  # type: ignore[import-untyped]

# 로거 설정
logger = logging.getLogger(__name__)


class NaverDataLabCollector:
    """네이버 데이터랩(쇼핑인사이트) 트렌드 키워드 수집기."""

    API_URL = "https://datalab.naver.com/shoppingInsight/getCategoryKeywordRank.naver"

    # 주요 카테고리 ID (블로그 주제로 적합한 것 위주)
    CATEGORIES = {
        "디지털/가전": "50000003",
        "생활/건강": "50000008",
        "식품": "50000006",
        "스포츠/레저": "50000007",
        "화장품/미용": "50000002",
        "출산/육아": "50000005",
        "패션의류": "50000000",
        "패션잡화": "50000001",
        "가구/인테리어": "50000004",
        "여가/생활편의": "50000009",
        "면세점": "50000010",
    }

    def __init__(
        self,
        cache_file: str = "data/trend_cache/naver_datalab_cache.json",
        timeout_sec: float = 10.0,
        max_retries: int = 3,
        retry_backoff_sec: float = 1.0,
        cache_ttl_hours: int = 24,
        session: Optional[requests.Session] = None,
    ):
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.cache_ttl_hours = cache_ttl_hours
        self.cache_file = Path(cache_file)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_data = self._load_cache_data()

        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://datalab.naver.com/shoppingInsight/sCategory.naver",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def fetch_trending_keywords(
        self,
        category_name: str = "디지털/가전",
        count: int = 20,
        use_cache_on_error: bool = True,
    ) -> List[str]:
        """특정 카테고리의 트렌드 키워드(인기 검색어) 수집."""
        category_id = self.CATEGORIES.get(category_name)
        if not category_id:
            logger.error("Unknown category: %s", category_name)
            return []

        target_count = max(1, min(count, 100))
        payload = self._build_payload(category_id, target_count)
        last_error: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    self.API_URL,
                    data=payload,
                    timeout=(3.0, self.timeout_sec),
                )
                response.raise_for_status()
                response_data = response.json()
                keywords = self._extract_keywords(response_data, target_count)
                if keywords:
                    self._set_cache(category_name, keywords)
                    logger.info(
                        "Fetched %d keywords for %s (attempt=%d)",
                        len(keywords),
                        category_name,
                        attempt,
                    )
                    return keywords

                last_error = "invalid_response_schema"
                logger.warning(
                    "DataLab schema mismatch for %s (attempt=%d)",
                    category_name,
                    attempt,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                logger.warning(
                    "DataLab request failed for %s (attempt=%d/%d): %s",
                    category_name,
                    attempt,
                    self.max_retries,
                    exc,
                )
            except ValueError as exc:
                last_error = str(exc)
                logger.warning(
                    "DataLab json parse failed for %s (attempt=%d/%d): %s",
                    category_name,
                    attempt,
                    self.max_retries,
                    exc,
                )

            if attempt < self.max_retries:
                # 재시도 폭주를 피하기 위한 지수 백오프 + 지터
                sleep_sec = (self.retry_backoff_sec * (2 ** (attempt - 1))) + random.uniform(0.1, 0.5)
                time.sleep(sleep_sec)

        if use_cache_on_error:
            cached = self.get_cached_keywords(category_name)
            if cached:
                logger.warning(
                    "Using cached keywords for %s after failure: %s",
                    category_name,
                    last_error,
                )
                return cached

        logger.error("Failed to fetch trends for %s: %s", category_name, last_error)
        return []

    def fetch_all_categories(self, top_n: int = 5) -> Dict[str, List[str]]:
        """모든 카테고리에서 상위 N개 키워드 수집."""
        result: Dict[str, List[str]] = {}
        for category_name in self.CATEGORIES:
            result[category_name] = self.fetch_trending_keywords(category_name, count=top_n)
        return result

    def get_cached_keywords(self, category_name: str) -> List[str]:
        """TTL 내 캐시된 키워드를 반환."""
        cache_entry = self._cache_data.get(category_name)
        if not cache_entry:
            return []

        fetched_at_raw = cache_entry.get("fetched_at", "")
        try:
            fetched_at = datetime.fromisoformat(fetched_at_raw)
        except ValueError:
            return []

        if datetime.utcnow() - fetched_at > timedelta(hours=self.cache_ttl_hours):
            return []

        keywords = cache_entry.get("keywords", [])
        if isinstance(keywords, list):
            return [item for item in keywords if isinstance(item, str) and item.strip()]
        return []

    def _build_payload(self, category_id: str, count: int) -> Dict[str, Any]:
        """요청 payload를 생성."""
        # 데이터랩은 보통 하루 전 데이터까지 안정적으로 제공된다.
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        return {
            "cid": category_id,
            "timeUnit": "date",
            "startDate": start_date,
            "endDate": end_date,
            "age": "",  # 전체 연령
            "gender": "",  # 전체 성별
            "device": "",  # 전체 기기
            "page": 1,
            "count": count,
        }

    def _extract_keywords(self, response_data: Dict[str, Any], count: int) -> List[str]:
        """응답 JSON에서 키워드 리스트를 추출하고 검증."""
        ranks = response_data.get("ranks", [])
        if not isinstance(ranks, list):
            return []

        keywords: List[str] = []
        for item in ranks:
            if not isinstance(item, dict):
                continue
            keyword = item.get("keyword")
            if isinstance(keyword, str) and keyword.strip():
                keywords.append(keyword.strip())

        return keywords[:count]

    def _load_cache_data(self) -> Dict[str, Dict[str, Any]]:
        """캐시 파일을 로드한다."""
        if not self.cache_file.exists():
            return {}
        try:
            with self.cache_file.open("r", encoding="utf-8") as file:
                raw_data = json.load(file)
                if isinstance(raw_data, dict):
                    return raw_data
        except Exception as exc:
            logger.warning("Failed to load cache file %s: %s", self.cache_file, exc)
        return {}

    def _set_cache(self, category_name: str, keywords: List[str]) -> None:
        """성공 응답을 캐시에 기록한다."""
        self._cache_data[category_name] = {
            "keywords": keywords,
            "fetched_at": datetime.utcnow().isoformat(),
        }
        self._persist_cache_data()

    def _persist_cache_data(self) -> None:
        """캐시 파일을 안전하게 저장한다."""
        tmp_path = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(self._cache_data, file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.cache_file)
        except Exception as exc:
            logger.warning("Failed to persist cache file %s: %s", self.cache_file, exc)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collector = NaverDataLabCollector()

    print("=== 디지털/가전 인기 검색어 ===")
    print(collector.fetch_trending_keywords("디지털/가전", count=10))

    print("\n=== 전체 카테고리 TOP 3 ===")
    for category_name, keywords in collector.fetch_all_categories(top_n=3).items():
        print(f"{category_name}: {keywords}")
