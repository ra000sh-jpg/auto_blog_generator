from scripts.publish_once import resolve_topic_and_persona


def test_category_takes_priority_over_persona():
    """--category가 지정되면 --persona보다 우선 적용되어야 한다."""
    topic_mode, persona_id = resolve_topic_and_persona(persona_id="P2", category="economy")
    assert topic_mode == "finance"
    assert persona_id == "P4"


def test_persona_logic_kept_when_category_missing():
    """--category가 없으면 기존 persona 로직을 유지해야 한다."""
    topic_mode, persona_id = resolve_topic_and_persona(persona_id="P2", category=None)
    assert topic_mode == "it"
    assert persona_id == "P2"
