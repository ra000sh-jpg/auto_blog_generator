from modules.market import KeywordSignal, OpportunityScorer


def test_market_opportunity_score_rewards_gap_and_market_signal() -> None:
    """검색/뉴스/시장 신호가 좋고 블로그 공급이 낮으면 높은 점수를 준다."""

    scorer = OpportunityScorer()
    score = scorer.score(
        KeywordSignal(
            keyword="전력설비",
            entities=("HD현대일렉트릭", "LS ELECTRIC"),
            search_momentum=2.1,
            news_count_24h=8,
            news_baseline_daily=3,
            recent_blog_count=2,
            blog_baseline=10,
            market_signal=86,
            authority_signal=82,
            persona_fit=90,
            evidence=("naver_news_recent", "market_snapshot"),
        )
    )

    assert score.final_score >= 80
    assert score.blog_gap >= 70
    assert score.risk_penalty == 0


def test_market_opportunity_score_penalizes_risky_keyword() -> None:
    """급등주/수익보장성 키워드는 기회 점수에서 감점한다."""

    scorer = OpportunityScorer()
    risky = scorer.score(
        KeywordSignal(
            keyword="무조건 오르는 급등주",
            search_momentum=3.0,
            news_count_24h=10,
            recent_blog_count=1,
            market_signal=95,
            authority_signal=70,
            persona_fit=40,
        )
    )

    assert risky.risk_penalty > 0
    assert risky.final_score < 80
