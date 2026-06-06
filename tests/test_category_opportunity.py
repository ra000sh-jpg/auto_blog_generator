from modules.content_sources import CategoryOpportunityEngine, SourceItem


def test_category_template_selects_it_compare():
    """IT 비교 글감은 비교 선택형 템플릿을 우선 사용한다."""

    engine = CategoryOpportunityEngine(
        source_items=[
            SourceItem(
                topic_mode="it",
                title="ChatGPT vs Gemini vs Claude 업무 자동화 비교",
                source_name="manual",
                platform="manual",
                keywords=("AI", "비교"),
            )
        ]
    )

    brief = engine.build_brief(topic_mode="it")

    assert brief.template_id == "it_compare_decide"
    assert "비교" in brief.title


def test_category_template_selects_health_myth_check():
    """과장된 건강 주장은 오해 점검형 템플릿으로 강제한다."""

    engine = CategoryOpportunityEngine(
        source_items=[
            SourceItem(
                topic_mode="health",
                title="디톡스 루틴으로 피로가 완치된다는 주장",
                source_name="manual",
                platform="manual",
                keywords=("디톡스", "완치"),
            )
        ]
    )

    brief = engine.build_brief(topic_mode="health")

    assert brief.template_id == "health_myth_check"
    assert brief.safety_issues
    assert brief.score.risk_penalty > 0


def test_category_template_rotates_ambiguous_items():
    """명확한 트리거가 없으면 최근 덜 사용한 양식을 선택한다."""

    engine = CategoryOpportunityEngine()

    brief = engine.build_brief(
        topic_mode="parenting",
        recent_template_ids=[
            "parenting_empathy_story",
            "parenting_home_apply",
            "parenting_empathy_story",
            "parenting_home_apply",
        ],
    )

    assert brief.template_id == "parenting_checklist"
