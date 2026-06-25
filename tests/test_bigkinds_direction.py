from __future__ import annotations

from datetime import datetime, timezone

from modules.automation.job_store import Job
from modules.collectors.bigkinds_public import BigKindsIssue, parse_bigkinds_public_issues
from modules.llm.content_generator import ContentGenerator
from modules.market import (
    BlogSlot,
    DataMode,
    DirectionSignalAggregator,
    MarketDataPoint,
    MarketNewsItem,
    MarketScope,
    MarketSnapshot,
    SourceConfidence,
    evaluate_directional_title,
    plan_directional_topic,
    signals_from_bigkinds_issues,
    signals_from_market_news_items,
    signals_from_naver_items,
)


class DummyClient:
    @property
    def provider_name(self) -> str:
        return "dummy"


def test_parse_bigkinds_public_issues_from_json_fragment():
    """빅카인즈 공개 화면 파서는 기사 본문 없이 이슈 메타만 추출해야 한다."""

    html = """
    <script>
    window.__issues = [
      {"issueTitle":"AI 반도체 투자 확대","categoryName":"경제","newsCount":"42","keywords":["삼성전자","HBM","전력"]},
      {"issueTitle":"정치권 회동","categoryName":"정치","newsCount":"31"}
    ];
    </script>
    """

    issues = parse_bigkinds_public_issues(
        html,
        collected_at="2026-06-23T00:00:00Z",
        max_items=5,
    )

    assert issues[0].issue_title == "AI 반도체 투자 확대"
    assert issues[0].category == "경제"
    assert issues[0].news_count == 42
    assert "HBM" in issues[0].keywords


def test_directional_topic_planner_makes_purpose_first_title():
    """방향성 플래너는 수치명보다 독자 판단 목적이 보이는 제목을 우선해야 한다."""

    intent = plan_directional_topic(
        base_title="2026-06-23 국장 개장 전 브리핑 - 금리와 환율이 남긴 기준",
        issues=[
            BigKindsIssue(
                issue_title="AI 반도체 투자 확대",
                category="경제",
                news_count=42,
                keywords=("삼성전자", "HBM", "전력"),
                confidence=0.9,
            )
        ],
        confirmed_metrics=[
            {"key": "US10Y", "label": "DGS10", "value": 4.2, "source": "FRED CSV"},
            {"key": "USD_JPY_BOJ", "label": "USD/JPY", "value": 145.32, "source": "BOJ"},
            {"key": "KOSPI", "label": "KOSPI", "value": 2870.5, "source": "Stooq"},
        ],
        seed_keywords=["국장", "반도체", "환율"],
        scope="kr",
    )

    assert intent is not None
    assert "AI 반도체" in intent.primary_title
    assert "4.2" not in intent.primary_title
    assert any(role.metric_key == "US10Y" and "성장주" in role.role for role in intent.evidence_roles)
    assert intent.speaker_purpose
    assert intent.why_today
    assert intent.article_type == "이슈 해설형"
    assert "단일 뉴스" in intent.do_not_claim[0]


def test_directional_title_quality_detects_numeric_centered_title():
    """제목 게이트는 지표 나열형 제목을 약한 제목으로 판정해야 한다."""

    weak = evaluate_directional_title("국장 개장 전 브리핑 - 금리와 환율이 남긴 기준")
    strong = evaluate_directional_title("오늘 국장, 반도체를 맞히기보다 실제 수요가 확인되는지를 보자")

    assert weak["passes"] is False
    assert strong["passes"] is True


def test_content_generator_builds_editorial_intent_context_from_direction_tags():
    """스케줄러가 남긴 빅카인즈 방향 태그는 생성 프롬프트 컨텍스트로 복원되어야 한다."""

    collected_at = datetime(2026, 6, 23, tzinfo=timezone.utc)
    snapshot_meta = {
        "scope": "kr",
        "source_pack": {
            "confirmed_metrics": [
                {"key": "US10Y", "label": "DGS10", "value": 4.2, "source": "FRED CSV"},
                {"key": "KOSPI", "label": "KOSPI", "value": 2870.5, "source": "Stooq"},
            ]
        },
    }
    job = Job(
        job_id="direction-job",
        status="running",
        title="오늘 국장, AI 반도체 투자 확대를 맞히기보다 실제 수요가 확인되는지를 보자",
        seed_keywords=["AI 반도체 투자 확대", "국장", "반도체"],
        platform="naver",
        persona_id="P4",
        scheduled_at=collected_at.isoformat(),
        category="경제 브리핑",
        tags=[
            "market_daily",
            "market_slot:kr_preopen",
            "market_scope:kr",
            "direction_source:bigkinds",
            "direction_issue:AI_반도체_투자_확대",
            "direction_angle:산업_흐름형",
        ],
    )
    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    contexts, intent = generator._collect_editorial_intent_context(
        job=job,
        market_snapshot_meta=snapshot_meta,
    )

    assert intent["issue_title"] == "AI 반도체 투자 확대"
    assert "화자 목적" in contexts[0]["content"]
    assert "오늘 다루는 이유" in contexts[0]["content"]
    assert "US10Y" in contexts[0]["content"]


def test_direction_signal_aggregator_scores_layered_sources():
    """빅카인즈, 네이버, RSS/GDELT 신호는 공통 점수판에서 비교되어야 한다."""

    bigkinds_signals = signals_from_bigkinds_issues(
        [
            BigKindsIssue(
                issue_title="AI 반도체 투자 확대",
                category="경제",
                news_count=42,
                keywords=("AI", "반도체", "전력"),
                confidence=0.9,
            )
        ]
    )
    market_signals = signals_from_market_news_items(
        [
            MarketNewsItem(
                title="Fed keeps focus on inflation risks",
                source="GDELT:example.com",
                url="https://example.com/fed",
                summary="Treasury yields and AI chip risk appetite",
            )
        ]
    )
    naver_signals = signals_from_naver_items(
        [
            {
                "title": "AI 반도체 지금 봐야 할 조건",
                "link": "https://blog.naver.com/example",
                "description": "반도체 투자자가 확인할 수요와 리스크",
                "source": "Naver blog",
            }
        ]
    )

    plan = DirectionSignalAggregator().aggregate(
        [*bigkinds_signals, *market_signals, *naver_signals],
        confirmed_metrics=[{"key": "US10Y", "source": "FRED CSV"}],
        seed_keywords=["AI 반도체", "국장"],
        scope="kr",
    )

    assert plan is not None
    assert plan.selected_signal.direction_score > 0
    assert "BigKinds public" in plan.source_mix
    assert any(signal.source == "Naver blog" for signal in plan.ranked_signals)


def test_market_snapshot_fixture_keeps_imports_used():
    """시장 스냅샷 타입 import가 깨지지 않도록 최소 객체를 만든다."""

    snapshot = MarketSnapshot(
        scope=MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        collected_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
        data_points=(MarketDataPoint(symbol="KOSPI", source="Stooq", value=2870.5),),
        news_items=(),
        skipped_sources=(),
        confidence=SourceConfidence(
            score=0.8,
            mode=DataMode.NUMERIC_BRIEFING,
            allow_numeric_claims=True,
            reason="test",
        ),
        fallback_topic_hints=(),
    )

    assert snapshot.scope == MarketScope.KR
