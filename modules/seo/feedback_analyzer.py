"""AI 기반 발행 성과 피드백 분석기.

발행 결과(조회수·노출·CTR) + 키워드 리서치 데이터를
DeepSeek/Gemini 같은 분석 특화 LLM에 넣어 유입 전략을
점진적으로 검증·개선한다.

피드백 루프 흐름:
  발행 완료 → post_metrics 테이블 축적
  → StrategyFeedbackLoop.run_analysis() (주기적 실행)
  → LLM이 상위/하위 포스트 패턴 분석
  → StrategySnapshot 저장
  → platform_strategy.update_strategy_field() 으로 전략 업데이트
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..llm.base_client import BaseLLMClient

from .platform_strategy import update_strategy_field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 분석 프롬프트
# ─────────────────────────────────────────────────────────────────────────────

_FEEDBACK_SYSTEM = (
    "You are a Korean blog SEO strategist specializing in Naver and Tistory platforms. "
    "Analyze blog post performance data and provide actionable strategy improvements. "
    "Always respond in valid JSON format."
)

_FEEDBACK_ANALYSIS_TEMPLATE = """최근 블로그 포스트 성과를 분석하고 유입 전략을 개선해주세요.

## 플랫폼: {platform}

## 상위 성과 포스트 (조회수 상위 {top_n}개)
{top_posts}

## 하위 성과 포스트 (조회수 하위 {bottom_n}개)
{bottom_posts}

## 분석 요청
1. 상위 포스트 공통 패턴 (키워드, 제목 구조, 주제, 톤, 태그)
2. 하위 포스트 실패 원인 (키워드 경쟁, 주제 관심도, 구조)
3. 다음 30일 콘텐츠 전략 권고안
4. 태그 전략 개선안

## 응답 형식 (JSON만 출력)
{{
  "top_patterns": {{
    "keywords": ["키워드1", "키워드2"],
    "title_structures": ["패턴1", "패턴2"],
    "topics": ["주제1"],
    "tones": ["톤1"],
    "effective_tags": ["태그1", "태그2"]
  }},
  "failure_patterns": ["실패원인1", "실패원인2"],
  "strategy_updates": {{
    "priority_keywords": ["키워드1", "키워드2"],
    "deprioritize_keywords": ["키워드3"],
    "recommended_topics": ["주제1"],
    "recommended_tone": "conversational",
    "tag_improvements": ["개선사항1"],
    "keyword_density_target": 0.015
  }},
  "rewrite_candidates": ["job_id_1"],
  "confidence_score": 0.7,
  "analysis_summary": "한 줄 요약"
}}"""


@dataclass
class PostPerformanceData:
    """포스트 성과 데이터."""

    job_id: str
    title: str
    url: str
    platform: str
    published_at: str
    views: int = 0
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    topic_mode: str = ""

    def to_summary(self) -> str:
        """분석 프롬프트용 요약 문자열."""
        kws = ", ".join(self.keywords[:5]) if self.keywords else "-"
        tgs = ", ".join(self.tags[:5]) if self.tags else "-"
        return (
            f"[조회수: {self.views}] 제목: {self.title}\n"
            f"  키워드: {kws} | 태그: {tgs} | 주제: {self.topic_mode or '-'}"
        )


@dataclass
class StrategySnapshot:
    """LLM 분석 결과 스냅샷."""

    platform: str
    trigger: str  # "scheduled" | "manual" | "threshold"
    top_patterns: Dict[str, Any] = field(default_factory=dict)
    failure_patterns: List[str] = field(default_factory=list)
    strategy_updates: Dict[str, Any] = field(default_factory=dict)
    rewrite_candidates: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    analysis_summary: str = ""
    created_at: str = ""
    applied: bool = False


class FeedbackAnalyzer:
    """발행 성과 데이터를 기반으로 유입 전략을 분석·개선한다.

    Args:
        db_path:      automation.db 경로
        llm_client:   분석 전용 LLM (DeepSeek/Gemini 권장)
        min_posts:    분석에 필요한 최소 포스트 수
        top_ratio:    상위 포스트 비율 (기본 20%)
    """

    def __init__(
        self,
        db_path: str = "data/automation.db",
        llm_client: Optional["BaseLLMClient"] = None,
        min_posts: int = 5,
        top_ratio: float = 0.2,
    ):
        self.db_path = db_path
        self.llm_client = llm_client
        self.min_posts = min_posts
        self.top_ratio = top_ratio
        self._ensure_tables()

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

    def _ensure_tables(self) -> None:
        """피드백 분석용 테이블을 초기화한다."""
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS strategy_snapshots (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform     TEXT NOT NULL,
                    trigger      TEXT NOT NULL DEFAULT 'scheduled',
                    analysis_json TEXT NOT NULL DEFAULT '{}',
                    confidence   REAL DEFAULT 0.0,
                    summary      TEXT DEFAULT '',
                    applied      INTEGER DEFAULT 0,
                    created_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tag_performance (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag                 TEXT NOT NULL,
                    platform            TEXT NOT NULL,
                    topic_mode          TEXT DEFAULT '',
                    total_views         INTEGER DEFAULT 0,
                    post_count          INTEGER DEFAULT 0,
                    avg_views_per_post  REAL DEFAULT 0.0,
                    last_used           TEXT,
                    UNIQUE(tag, platform)
                );
            """)

    def record_tag_performance(
        self,
        tags: List[str],
        platform: str,
        views: int,
        topic_mode: str = "",
        published_at: str = "",
    ) -> None:
        """발행 후 조회수를 태그별로 기록한다."""
        if not tags or views <= 0:
            return

        per_tag = views / len(tags)
        with self._connection() as conn:
            for tag in tags:
                conn.execute("""
                    INSERT INTO tag_performance
                        (tag, platform, topic_mode, total_views, post_count, avg_views_per_post, last_used)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(tag, platform) DO UPDATE SET
                        total_views        = total_views + excluded.total_views,
                        post_count         = post_count + 1,
                        avg_views_per_post = CAST(
                            (total_views + excluded.total_views) AS REAL
                        ) / (post_count + 1),
                        last_used          = excluded.last_used
                """, (tag, platform, topic_mode, int(per_tag), per_tag, published_at or ""))

    def get_top_tags(
        self,
        platform: str,
        topic_mode: str = "",
        limit: int = 30,
    ) -> List[str]:
        """성과 높은 태그 목록을 반환한다."""
        with self._connection() as conn:
            if topic_mode:
                cursor = conn.execute("""
                    SELECT tag FROM tag_performance
                    WHERE platform = ? AND topic_mode = ?
                    ORDER BY avg_views_per_post DESC
                    LIMIT ?
                """, (platform, topic_mode, limit))
            else:
                cursor = conn.execute("""
                    SELECT tag FROM tag_performance
                    WHERE platform = ?
                    ORDER BY avg_views_per_post DESC
                    LIMIT ?
                """, (platform, limit))
            return [row["tag"] for row in cursor.fetchall()]

    def _load_post_metrics(self, platform: str, days: int = 30) -> List[PostPerformanceData]:
        """최근 N일 포스트 성과를 DB에서 로드한다."""
        with self._connection() as conn:
            master_channel_id = ""
            try:
                master_row = conn.execute(
                    """
                    SELECT channel_id
                    FROM channels
                    WHERE is_master = 1
                    AND active = 1
                    LIMIT 1
                    """
                ).fetchone()
                if master_row and master_row["channel_id"]:
                    master_channel_id = str(master_row["channel_id"])
            except sqlite3.OperationalError:
                master_channel_id = ""

            # channel_filter_sql은 코드 내부에서만 선택되는 고정 SQL 조각(외부 입력 미포함)이다.
            channel_filter_sql = "AND (j.channel_id IS NULL OR j.channel_id = '' OR j.job_kind = 'master')"
            params: List[str] = [platform, f"-{days} days"]
            if master_channel_id:
                channel_filter_sql = "AND (j.channel_id IS NULL OR j.channel_id = ?)"
                params.append(master_channel_id)

            # post_metrics + jobs 조인
            try:
                query = """
                    SELECT
                        pm.job_id, pm.title, pm.url, pm.views,
                        pm.published_at,
                        j.seed_keywords, j.platform, j.persona_id,
                        j.seo_snapshot
                    FROM post_metrics pm
                    JOIN jobs j ON pm.job_id = j.job_id
                    WHERE j.platform = ?
                    AND pm.published_at >= datetime('now', ?)
                """
                query = query + "\n" + channel_filter_sql + "\nORDER BY pm.views DESC"
                cursor = conn.execute(query, tuple(params))

                results = []
                for row in cursor.fetchall():
                    kws = json.loads(row["seed_keywords"] or "[]")
                    seo = json.loads(row["seo_snapshot"] or "{}")
                    topic = seo.get("topic_mode", "")
                    results.append(PostPerformanceData(
                        job_id=row["job_id"],
                        title=row["title"],
                        url=row["url"],
                        platform=row["platform"],
                        published_at=row["published_at"],
                        views=row["views"],
                        keywords=kws,
                        topic_mode=topic,
                    ))
                return results
            except sqlite3.OperationalError:
                # post_metrics 테이블이 없거나 컬럼 불일치 시 빈 리스트
                return []

    def _save_snapshot(self, snapshot: StrategySnapshot) -> None:
        """분석 스냅샷을 DB에 저장한다."""
        from ..automation.time_utils import now_utc
        now = now_utc()
        analysis_data = {
            "top_patterns": snapshot.top_patterns,
            "failure_patterns": snapshot.failure_patterns,
            "strategy_updates": snapshot.strategy_updates,
            "rewrite_candidates": snapshot.rewrite_candidates,
        }
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO strategy_snapshots
                    (platform, trigger, analysis_json, confidence, summary, applied, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.platform,
                snapshot.trigger,
                json.dumps(analysis_data, ensure_ascii=False),
                snapshot.confidence_score,
                snapshot.analysis_summary,
                int(snapshot.applied),
                now,
            ))
        snapshot.created_at = now

    async def run_analysis(
        self,
        platform: str,
        trigger: str = "scheduled",
        apply_updates: bool = False,
        days: int = 30,
    ) -> Optional[StrategySnapshot]:
        """플랫폼 유입 전략 분석을 실행한다.

        Args:
            platform:      분석 대상 플랫폼 (naver|tistory)
            trigger:       실행 트리거 (scheduled|manual|threshold)
            apply_updates: True면 분석 결과를 전략에 즉시 반영
            days:          분석 기간 (일)

        Returns:
            StrategySnapshot or None (데이터 부족 시)
        """
        posts = self._load_post_metrics(platform, days)

        if len(posts) < self.min_posts:
            logger.info(
                "Insufficient data for strategy analysis",
                extra={"platform": platform, "post_count": len(posts), "min_posts": self.min_posts},
            )
            return None

        if not self.llm_client:
            logger.warning("No LLM client configured for feedback analysis")
            return None

        # 상위/하위 분류
        total = len(posts)
        top_n = max(1, int(total * self.top_ratio))
        bottom_n = max(1, int(total * self.top_ratio))

        sorted_posts = sorted(posts, key=lambda p: p.views, reverse=True)
        top_posts = sorted_posts[:top_n]
        bottom_posts = sorted_posts[-bottom_n:]

        top_summary = "\n".join(p.to_summary() for p in top_posts)
        bottom_summary = "\n".join(p.to_summary() for p in bottom_posts)

        prompt = _FEEDBACK_ANALYSIS_TEMPLATE.format(
            platform=platform,
            top_n=top_n,
            bottom_n=bottom_n,
            top_posts=top_summary,
            bottom_posts=bottom_summary,
        )

        try:
            response = await self.llm_client.generate(
                system_prompt=_FEEDBACK_SYSTEM,
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=800,
            )
            snapshot = self._parse_analysis(response.content, platform, trigger)
        except Exception as exc:
            logger.error("Feedback analysis LLM call failed: %s", exc)
            return None

        self._save_snapshot(snapshot)

        if apply_updates:
            self._apply_strategy_updates(platform, snapshot)

        logger.info(
            "Strategy analysis complete",
            extra={
                "platform": platform,
                "confidence": snapshot.confidence_score,
                "summary": snapshot.analysis_summary,
                "rewrite_count": len(snapshot.rewrite_candidates),
            },
        )
        return snapshot

    def _parse_analysis(
        self, raw: str, platform: str, trigger: str
    ) -> StrategySnapshot:
        """LLM 응답을 StrategySnapshot으로 변환한다."""
        import re
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)

        snapshot = StrategySnapshot(platform=platform, trigger=trigger)

        if not json_match:
            logger.warning("No JSON in feedback analysis response")
            snapshot.analysis_summary = "파싱 실패: JSON 없음"
            return snapshot

        try:
            data = json.loads(json_match.group())
            snapshot.top_patterns = data.get("top_patterns", {})
            snapshot.failure_patterns = data.get("failure_patterns", [])
            snapshot.strategy_updates = data.get("strategy_updates", {})
            snapshot.rewrite_candidates = data.get("rewrite_candidates", [])
            snapshot.confidence_score = float(data.get("confidence_score", 0.0))
            snapshot.analysis_summary = data.get("analysis_summary", "")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Feedback parse error: %s", exc)
            snapshot.analysis_summary = f"파싱 실패: {exc}"

        return snapshot

    def _apply_strategy_updates(
        self, platform: str, snapshot: StrategySnapshot
    ) -> None:
        """분석 결과를 플랫폼 전략에 반영한다.

        신뢰도(confidence_score)가 낮으면 반영을 건너뛴다.
        """
        if snapshot.confidence_score < 0.5:
            logger.info(
                "Strategy update skipped: low confidence",
                extra={"confidence": snapshot.confidence_score},
            )
            return

        updates = snapshot.strategy_updates
        if not updates:
            return

        # keyword_density_target 업데이트
        density = updates.get("keyword_density_target")
        if density and 0.005 <= float(density) <= 0.05:
            update_strategy_field(platform, "keyword_density_target", float(density))
            logger.info(
                "Strategy updated: keyword_density_target",
                extra={"platform": platform, "value": density},
            )

        snapshot.applied = True

    def get_latest_snapshot(self, platform: str) -> Optional[StrategySnapshot]:
        """가장 최근 전략 스냅샷을 반환한다."""
        with self._connection() as conn:
            row = conn.execute("""
                SELECT * FROM strategy_snapshots
                WHERE platform = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (platform,)).fetchone()

            if not row:
                return None

            data = json.loads(row["analysis_json"] or "{}")
            return StrategySnapshot(
                platform=row["platform"],
                trigger=row["trigger"],
                top_patterns=data.get("top_patterns", {}),
                failure_patterns=data.get("failure_patterns", []),
                strategy_updates=data.get("strategy_updates", {}),
                rewrite_candidates=data.get("rewrite_candidates", []),
                confidence_score=row["confidence"],
                analysis_summary=row["summary"],
                created_at=row["created_at"],
                applied=bool(row["applied"]),
            )
