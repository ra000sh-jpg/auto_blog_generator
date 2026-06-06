from types import SimpleNamespace

from modules.automation.job_store import Job
from modules.content_sources import (
    render_strategy_prompt,
    select_category_writing_strategy,
    select_market_writing_strategy,
    writing_strategy_tags,
)
from modules.llm.content_generator import ContentGenerator


def test_market_strategy_selects_kr_preopen_scenario():
    """국장전 슬롯은 시나리오 브리핑 전략을 기본으로 사용한다."""

    plan = select_market_writing_strategy(
        title="2026-06-08 국장 개장 전 브리핑",
        tags=["market_slot:kr_preopen"],
        seed_keywords=["국장", "환율"],
    )

    assert plan.strategy_id == "market_preopen_scenario"
    assert "반대 신호" in render_strategy_prompt(plan)
    assert "writing_strategy:market_preopen_scenario" in writing_strategy_tags(plan)


def test_market_strategy_forces_sector_fact_check_for_hot_theme():
    """급등주/테마 과열 표현은 검증형 전략으로 강제한다."""

    plan = select_market_writing_strategy(
        title="상한가 따라잡기 급등주 테마 점검",
        tags=["market_slot:opportunity"],
        seed_keywords=["급등주"],
    )

    assert plan.strategy_id == "sector_fact_check"
    assert plan.intent.value == "risk_check"


def test_market_strategy_selects_macro_policy_note():
    """FOMC/CPI/환율 같은 지표 글은 정책 노트형으로 보낸다."""

    plan = select_market_writing_strategy(
        title="FOMC 이후 환율과 금리 경로 정리",
        tags=["market_slot:opportunity"],
        seed_keywords=["FOMC", "환율"],
    )

    assert plan.strategy_id == "macro_policy_note"
    assert "다음 일정" in render_strategy_prompt(plan)


def test_category_template_becomes_strategy_mix():
    """확장 카테고리 템플릿은 전략 비율과 블록 계획으로 변환된다."""

    plan = select_category_writing_strategy(
        topic_mode="parenting",
        template_id="parenting_home_apply",
        title="아기 식단을 우리 집에 적용하기",
        tags=["category_topic:parenting"],
    )

    assert plan is not None
    assert plan.strategy_id == "parenting_home_apply"
    assert "우리 집 적용" in render_strategy_prompt(plan)


def test_content_generator_injects_market_strategy_prompt():
    """경제 생성 프롬프트에는 전략, 반대 신호, 투자권유 회피 지시가 들어간다."""

    fake_client = SimpleNamespace(provider_name="fake")
    generator = ContentGenerator(
        primary_client=fake_client,
        secondary_client=fake_client,
        voice_client=fake_client,
        parser_client=fake_client,
        rss_news_collector=object(),
        rag_search_engine=object(),
    )
    job = Job(
        job_id="strategy-prompt-job",
        status="queued",
        title="국장 개장 전 브리핑",
        seed_keywords=["국장", "환율"],
        platform="naver",
        persona_id="P4",
        scheduled_at="2026-06-08T00:00:00Z",
        tags=["market_slot:kr_preopen", "writing_strategy:market_preopen_scenario"],
    )

    prompt = generator._build_market_slot_writing_injection(job)

    assert "경제 글쓰기 전략 라우터 지시" in prompt
    assert "국장전 시나리오 브리핑형" in prompt
    assert "반대 신호" in prompt
    assert "투자권유 회피 블록" in prompt
