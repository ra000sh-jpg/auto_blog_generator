"""RSS 기반 아이디어 금고 자동 수집기.

APScheduler 크론 훅에서 호출되며, RSS 피드에서 기사 제목+요약을
IdeaVaultBatchParser 를 통해 정제한 뒤 idea_vault 테이블에 저장한다.

Track A (Auto Collector) 구현체.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:
    from ..automation.job_store import JobStore
    from ..config import LLMConfig
    from ..llm.idea_vault_parser import IdeaVaultBatchParser

logger = logging.getLogger(__name__)

# RSS 수집 시 사용할 기본 키워드 (카테고리 무관 범용 트렌드 수집)
_DEFAULT_COLLECT_KEYWORDS: List[str] = [
    "경제", "투자", "재테크", "it", "ai", "자동화",
    "육아", "교육", "생활", "건강", "요리", "여행",
]

# 한 번의 수집 사이클에서 RSS 피드당 최대 수집 기사 수
_MAX_ITEMS_PER_FEED = 5
# 한 사이클에서 금고에 저장할 최대 아이템 수 (LLM 비용 절감)
_MAX_VAULT_ITEMS_PER_RUN = 20
# 기사 시간 범위 (최근 N시간)
_WITHIN_HOURS = 24


class IdeaVaultAutoCollector:
    """RSS 피드에서 기사를 수집해 idea_vault 에 자동 저장한다."""

    def __init__(
        self,
        job_store: "JobStore",
        llm_config: Optional["LLMConfig"] = None,
        feed_urls: Optional[Sequence[str]] = None,
        collect_keywords: Optional[Sequence[str]] = None,
        max_items_per_run: int = _MAX_VAULT_ITEMS_PER_RUN,
        within_hours: int = _WITHIN_HOURS,
    ):
        self._job_store = job_store
        self._llm_config = llm_config
        self._feed_urls = list(feed_urls) if feed_urls else None  # None → RssNewsCollector 기본값 사용
        self._collect_keywords = list(collect_keywords) if collect_keywords else list(_DEFAULT_COLLECT_KEYWORDS)
        self._max_items_per_run = max(1, max_items_per_run)
        self._within_hours = max(1, within_hours)

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 메서드
    # ──────────────────────────────────────────────────────────────────────────

    async def run_once(self) -> int:
        """수집 → 파싱 → 저장 전체 사이클을 1회 실행한다.

        Returns:
            새로 저장된 idea_vault 아이템 수.
        """
        articles = self._fetch_articles()
        if not articles:
            logger.info("IdeaVaultAutoCollector: no articles fetched")
            return 0

        logger.info("IdeaVaultAutoCollector: fetched %d articles", len(articles))

        parsed_items = await self._triage_articles(articles)
        if not parsed_items:
            logger.info("IdeaVaultAutoCollector: no items passed triage")
            return 0

        saved = self._job_store.add_idea_vault_items(parsed_items)
        logger.info(
            "IdeaVaultAutoCollector: saved %d / %d items to vault",
            saved,
            len(parsed_items),
        )
        return saved

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_articles(self) -> List[Dict[str, str]]:
        """RssNewsCollector 로 기사 목록을 수집한다."""
        try:
            from .rss_news_collector import RssNewsCollector

            collector_kwargs: Dict[str, Any] = {}
            if self._feed_urls:
                collector_kwargs["feed_urls"] = self._feed_urls

            collector = RssNewsCollector(**collector_kwargs)
            try:
                articles = collector.fetch_relevant_news(
                    keywords=self._collect_keywords,
                    within_hours=self._within_hours,
                    max_items=self._max_items_per_run,
                )
            finally:
                collector.close()
            return articles
        except Exception as exc:
            logger.warning("IdeaVaultAutoCollector: article fetch failed: %s", exc)
            return []

    async def _triage_articles(
        self, articles: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """기사 제목+요약을 IdeaVaultBatchParser 로 정제해 저장 가능한 형식으로 반환한다.

        기사 제목과 짧은 요약을 1줄 입력으로 조합 → 파서 투입.
        source_url 은 별도 필드로 보존해 중복 차단에 활용한다.
        """
        # 파서 입력 구성: "제목 — 요약(150자 이하)" 형태의 1줄 문자열 리스트
        lines_with_url: List[tuple[str, str]] = []  # (triage_line, source_url)
        for article in articles[: self._max_items_per_run]:
            title = str(article.get("title", "")).strip()
            content = str(article.get("content", "")).strip()
            link = str(article.get("link", "")).strip()
            if not title:
                continue
            # 요약: 제목과 내용 앞 150자를 결합
            summary_snippet = content[:150].rstrip() if content else ""
            line = f"{title} — {summary_snippet}" if summary_snippet else title
            lines_with_url.append((line, link))

        if not lines_with_url:
            return []

        raw_text_block = "\n".join(line for line, _ in lines_with_url)
        url_by_line = {line: url for line, url in lines_with_url}

        categories = self._get_allowed_categories()

        try:
            from ..llm.idea_vault_parser import IdeaVaultBatchParser
            from ..config import LLMConfig

            parser = IdeaVaultBatchParser(llm_config=self._llm_config or LLMConfig())
            result = await parser.parse_bulk(
                raw_text_block,
                categories=categories,
                batch_size=20,
            )
        except Exception as exc:
            logger.warning("IdeaVaultAutoCollector: triage parser failed: %s", exc)
            # 파서 실패 시 heuristic fallback — 제목 그대로 저장
            return self._heuristic_fallback(lines_with_url, categories)

        # 파서 결과를 저장 형식으로 변환 (source_url 매핑)
        vault_items: List[Dict[str, str]] = []
        for item in result.accepted_items:
            # 파서가 반환한 raw_text 와 원본 라인을 매칭해 URL 복원
            source_url = ""
            for original_line, url in lines_with_url:
                if item.raw_text and item.raw_text in original_line:
                    source_url = url
                    break
                if original_line and original_line.startswith(item.raw_text[:20]):
                    source_url = url
                    break

            vault_items.append(
                {
                    "raw_text": item.raw_text,
                    "mapped_category": item.mapped_category,
                    "topic_mode": item.topic_mode,
                    "parser_used": result.parser_used,
                    "source_url": source_url,
                }
            )
        return vault_items

    def _heuristic_fallback(
        self,
        lines_with_url: List[tuple[str, str]],
        categories: List[str],
    ) -> List[Dict[str, str]]:
        """파서 실패 시 제목을 그대로 pending 아이템으로 저장한다."""
        from ..constants import DEFAULT_FALLBACK_CATEGORY

        fallback_category = categories[0] if categories else DEFAULT_FALLBACK_CATEGORY
        items = []
        for line, url in lines_with_url:
            if not line.strip():
                continue
            items.append(
                {
                    "raw_text": line.strip(),
                    "mapped_category": fallback_category,
                    "topic_mode": "cafe",
                    "parser_used": "heuristic_fallback",
                    "source_url": url,
                }
            )
        return items

    def _get_allowed_categories(self) -> List[str]:
        """DB 에서 허용 카테고리 목록을 조회한다."""
        from ..constants import DEFAULT_FALLBACK_CATEGORY
        import json

        try:
            raw = self._job_store.get_system_setting("custom_categories", "[]")
            decoded = json.loads(raw)
            categories = [str(c).strip() for c in decoded if str(c).strip()]
            if categories:
                return categories
        except Exception:
            pass

        saved_fallback = self._job_store.get_system_setting("fallback_category", "").strip()
        return [saved_fallback if saved_fallback else DEFAULT_FALLBACK_CATEGORY]
