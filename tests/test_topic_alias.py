from modules.llm.prompts import get_persona_profile, get_topic_mode, normalize_topic_mode


def test_economy_alias_resolves_to_finance_topic():
    """economy 별칭은 내부적으로 finance 토픽으로 매핑되어야 한다."""
    assert normalize_topic_mode("economy") == "finance"
    assert get_topic_mode("economy").id == "finance"
    assert get_persona_profile("economy").topic_mode == "finance"
