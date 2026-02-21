"""발행된 포스트의 조회수/성과 지표 수집기.

피드백 루프를 위해 네이버 블로그에서 조회수를 가져와
post_metrics 테이블에 저장한다.

사용 방법:
    collector = MetricsCollector(db_path="data/automation.db")
    await collector.collect_all_pending()  # 주기적으로 실행
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PostMetric:
    """포스트 성과 지표."""

    post_id: str
    job_id: str
    title: str
    url: str
    published_at: str
    views: int = 0
    likes: int = 0
    comments: int = 0


class MetricsCollector:
    """발행된 포스트의 조회수를 수집한다.

    네이버 블로그 통계 API 또는 페이지 스크래핑으로 조회수를 가져온다.
    """

    def __init__(
        self,
        db_path: str = "data/automation.db",
        min_age_hours: int = 24,  # 발행 후 최소 대기 시간
        max_age_days: int = 30,   # 수집 대상 최대 기간
    ):
        self.db_path = db_path
        self.min_age_hours = min_age_hours
        self.max_age_days = max_age_days

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_pending_posts(self) -> List[dict]:
        """조회수 수집이 필요한 포스트 목록을 반환한다.

        조건:
        - status = 'completed'
        - 발행 후 min_age_hours 이상 경과
        - 발행 후 max_age_days 이내
        - 아직 post_metrics에 없거나 오래된 스냅샷
        """
        with self._connection() as conn:
            cursor = conn.execute("""
                SELECT
                    j.job_id,
                    j.title,
                    j.result_url,
                    j.updated_at as published_at,
                    j.seed_keywords,
                    j.seo_snapshot,
                    j.tags,
                    pm.snapshot_at
                FROM jobs j
                LEFT JOIN post_metrics pm ON j.job_id = pm.job_id
                WHERE j.status = 'completed'
                AND j.result_url != ''
                AND j.updated_at >= datetime('now', ?)
                AND j.updated_at <= datetime('now', ?)
                AND (
                    pm.snapshot_at IS NULL
                    OR pm.snapshot_at < datetime('now', '-7 days')
                )
                ORDER BY j.updated_at DESC
                LIMIT 50
            """, (f"-{self.max_age_days} days", f"-{self.min_age_hours} hours"))

            return [dict(row) for row in cursor.fetchall()]

    async def fetch_naver_views(self, url: str) -> Optional[int]:
        """네이버 블로그 포스트의 조회수를 가져온다.

        현재는 간단한 HTML 파싱 방식 사용.
        실제 운영에서는 네이버 블로그 통계 API나 Playwright 사용 권장.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not installed, skipping metrics collection")
            return None

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    )
                }
                async with session.get(url, headers=headers, timeout=15) as resp:
                    if resp.status != 200:
                        logger.warning("Failed to fetch %s: status %d", url, resp.status)
                        return None

                    html = await resp.text()

                    # 조회수 패턴 매칭 (네이버 블로그 HTML 구조에 따라 조정 필요)
                    patterns = [
                        r'"viewCount"\s*:\s*(\d+)',
                        r'조회\s*[:\s]*(\d+)',
                        r'class="[^"]*view[^"]*"[^>]*>(\d+)',
                        r'data-view-count="(\d+)"',
                    ]

                    for pattern in patterns:
                        match = re.search(pattern, html)
                        if match:
                            views = int(match.group(1))
                            logger.debug("Views found for %s: %d", url, views)
                            return views

                    logger.warning("Could not parse views from %s", url)
                    return None

        except Exception as exc:
            logger.warning("Error fetching views for %s: %s", url, exc)
            return None

    def save_metric(self, metric: PostMetric) -> None:
        """조회수 지표를 DB에 저장한다."""
        from ..automation.time_utils import now_utc
        now = now_utc()

        with self._connection() as conn:
            conn.execute("""
                INSERT INTO post_metrics
                    (post_id, job_id, title, url, published_at, views, likes, comments, snapshot_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    views = excluded.views,
                    likes = excluded.likes,
                    comments = excluded.comments,
                    snapshot_at = excluded.snapshot_at
            """, (
                metric.post_id,
                metric.job_id,
                metric.title,
                metric.url,
                metric.published_at,
                metric.views,
                metric.likes,
                metric.comments,
                now,
            ))

    async def collect_one(self, post: dict) -> Optional[PostMetric]:
        """단일 포스트의 조회수를 수집한다."""
        url = post.get("result_url", "")
        if not url:
            return None

        views = await self.fetch_naver_views(url)
        if views is None:
            return None

        # post_id는 URL에서 추출 (blog.naver.com/{id}/{post_no})
        post_id = url.split("/")[-1] if "/" in url else post["job_id"]

        metric = PostMetric(
            post_id=post_id,
            job_id=post["job_id"],
            title=post["title"],
            url=url,
            published_at=post["published_at"],
            views=views,
        )

        self.save_metric(metric)

        # 태그 성과 기록 (피드백 분석용)
        tags = json.loads(post.get("tags") or "[]")
        seo = json.loads(post.get("seo_snapshot") or "{}")
        if tags:
            from ..seo.feedback_analyzer import FeedbackAnalyzer
            analyzer = FeedbackAnalyzer(db_path=self.db_path)
            analyzer.record_tag_performance(
                tags=tags,
                platform=seo.get("platform", "naver"),
                views=views,
                topic_mode=seo.get("topic_mode", ""),
                published_at=post["published_at"],
            )

        return metric

    async def collect_all_pending(self) -> int:
        """수집 대기 중인 모든 포스트의 조회수를 수집한다.

        Returns:
            수집 성공한 포스트 수
        """
        pending = self.get_pending_posts()
        if not pending:
            logger.info("No pending posts to collect metrics")
            return 0

        logger.info("Collecting metrics for %d posts", len(pending))

        success_count = 0
        for post in pending:
            try:
                metric = await self.collect_one(post)
                if metric:
                    success_count += 1
                    logger.info(
                        "Collected views",
                        extra={"job_id": post["job_id"], "views": metric.views},
                    )
                # 요청 간 딜레이 (rate limiting 방지)
                await asyncio.sleep(2.0)
            except Exception as exc:
                logger.warning(
                    "Failed to collect metrics for %s: %s",
                    post["job_id"], exc
                )

        logger.info("Metrics collection complete: %d/%d", success_count, len(pending))
        return success_count
