from modules.llm.prompts import get_persona_profile, get_topic_mode, normalize_topic_mode


def test_economy_alias_resolves_to_finance_topic():
    """economy 별칭은 내부적으로 finance 토픽으로 매핑되어야 한다."""
    assert normalize_topic_mode("economy") == "finance"
    assert get_topic_mode("economy").id == "finance"
    assert get_persona_profile("economy").topic_mode == "finance"


def test_health_topic_is_supported():
    """건강 토픽은 카페 토픽으로 폴백되지 않아야 한다."""

    assert normalize_topic_mode("health") == "health"
    assert get_topic_mode("health").id == "health"
    assert get_persona_profile("health").topic_mode == "health"
