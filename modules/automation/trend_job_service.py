"""트렌드 키워드 기반 자동 Job 생성 서비스."""

from __future__ import annotations

import logging
import random
import uuid
from typing import Any, List, Optional

from ..collectors.naver_datalab import NaverDataLabCollector
from .job_store import JobStore
from .time_utils import now_utc

logger = logging.getLogger(__name__)


# NaverDataLab 카테고리 -> TopicMode 매핑
CATEGORY_TO_TOPIC = {
    "디지털/가전": "it",
    "생활/건강": "cafe",
    "식품": "cafe",
    "스포츠/레저": "cafe",
    "화장품/미용": "parenting",
    "출산/육아": "parenting",
    "패션의류": "parenting",
    "패션잡화": "parenting",
    "가구/인테리어": "cafe",
    "여가/생활편의": "cafe",
}


class TrendJobService:
    """트렌드 키워드를 수집해 예약 Job을 생성한다."""

    def __init__(
        self,
        job_store: JobStore,
        collector: Optional[NaverDataLabCollector] = None,
        max_jobs_per_run: int = 3,
        platform: str = "naver",
        persona_id: str = "default",
        memory_store: Optional[Any] = None,
    ):
        self.job_store = job_store
        self.collector = collector or NaverDataLabCollector()
        self.max_jobs_per_run = max(1, max_jobs_per_run)
        self.platform = platform
        self.persona_id = persona_id
        self.memory_store = memory_store

    def fetch_and_create_jobs(
        self,
        categories: Optional[List[str]] = None,
        keywords_per_category: int = 5,
    ) -> List[str]:
        """카테고리별 트렌드 키워드에서 Job을 생성한다."""
        if categories is None:
            categories = list(CATEGORY_TO_TOPIC.keys())

        created_job_ids: List[str] = []
        target_keywords = max(1, keywords_per_category)

        for category in categories:
            if len(created_job_ids) >= self.max_jobs_per_run:
                break

            keywords = self.collector.fetch_trending_keywords(
                category_name=category,
                count=target_keywords,
            )
            if not keywords:
                logger.warning("No trend keywords for category: %s", category)
                continue

            # 카테고리당 최대 2개까지 생성한다.
            for keyword in keywords[:2]:
                if len(created_job_ids) >= self.max_jobs_per_run:
                    break

                job_id = self._create_job_from_keyword(keyword=keyword, category=category)
                if job_id:
                    created_job_ids.append(job_id)
                    logger.info(
                        "Trend job created",
                        extra={"job_id": job_id, "keyword": keyword, "category": category},
                    )

        return created_job_ids

    def _create_job_from_keyword(self, keyword: str, category: str) -> Optional[str]:
        """키워드 1개를 즉시 실행 Job으로 변환한다."""
        topic_mode = CATEGORY_TO_TOPIC.get(category, "cafe")
        if self._has_recent_job(keyword, topic_mode=topic_mode):
            logger.debug("Skipping duplicate keyword: %s", keyword)
            return None

        title = self._generate_title(keyword, topic_mode)
        seed_keywords = self._build_seed_keywords(keyword)
        job_id = str(uuid.uuid4())

        success = self.job_store.schedule_job(
            job_id=job_id,
            title=title,
            seed_keywords=seed_keywords,
            platform=self.platform,
            persona_id=self.persona_id,
            scheduled_at=now_utc(),
            max_retries=3,
        )
        return job_id if success else None

    def _has_recent_job(self, keyword: str, days: int = 7, topic_mode: str = "") -> bool:
        """최근 중복 키워드 여부를 확인한다.

        1) 활성 jobs(queued/running/ready 등)와 중복 검사
        2) memory_store가 있으면 topic_memory 유사도 검사
        """
        normalized_keyword = str(keyword).strip()
        if not normalized_keyword:
            return False

        try:
            active_check = getattr(self.job_store, "has_recent_similar_active_job", None)
            if callable(active_check):
                if active_check(
                    keyword=normalized_keyword,
                    topic_mode=topic_mode,
                    platform=self.platform,
                    lookback_days=max(1, int(days)),
                ):
                    return True
        except Exception as exc:
            logger.debug("Active job duplicate check failed (non-critical): %s", exc)

        if self.memory_store is None:
            return False

        try:
            lookback_weeks = max(1, (max(1, int(days)) + 6) // 7)
            memory_config = getattr(self.memory_store, "_config", None)
            threshold = 0.50
            if memory_config is not None:
                threshold = float(
                    getattr(
                        memory_config,
                        "precheck_duplicate_threshold",
                        getattr(memory_config, "duplicate_threshold", 0.50),
                    )
                )

            return bool(
                self.memory_store.is_duplicate_before_job(
                    title=normalized_keyword,
                    keywords=[normalized_keyword],
                    topic_mode=topic_mode,
                    similarity_threshold=threshold,
                    platform=self.platform,
                    lookback_weeks=lookback_weeks,
                )
            )
        except Exception as exc:
            logger.debug("Topic memory duplicate check failed (non-critical): %s", exc)
            return False

    def _build_seed_keywords(self, keyword: str) -> List[str]:
        """seed_keywords를 구성한다."""
        seed_keywords = [keyword.strip()]
        if " " in keyword:
            for token in keyword.split():
                token = token.strip()
                if token and token not in seed_keywords:
                    seed_keywords.append(token)
                if len(seed_keywords) >= 3:
                    break
        return seed_keywords

    def _generate_title(self, keyword: str, topic_mode: str) -> str:
        """토픽 템플릿 기반 제목을 만든다."""
        templates = {
            "cafe": [
                f"{keyword} 완벽 가이드",
                f"{keyword}, 이것만 알면 끝!",
                f"{keyword} 꿀팁 총정리",
            ],
            "parenting": [
                f"{keyword} 육아맘 후기",
                f"{keyword} 추천 TOP 5",
                f"{keyword} 구매 전 필독!",
            ],
            "it": [
                f"{keyword} 비교 분석",
                f"{keyword} 실사용 리뷰",
                f"2026 {keyword} 추천",
            ],
            "finance": [
                f"{keyword} 절약 꿀팁",
                f"{keyword} 비용 총정리",
                f"{keyword} 가성비 분석",
            ],
        }
        return random.choice(templates.get(topic_mode, templates["cafe"]))
