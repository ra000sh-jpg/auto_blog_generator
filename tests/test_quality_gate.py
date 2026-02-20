from __future__ import annotations

from modules.seo.quality_gate import QualityGate


def test_quality_gate_fails_on_banned_word():
    gate = QualityGate(min_content_chars=300)
    content = (
        "이 글은 테스트 본문입니다. " * 40
        + "여기에 불법 대출 관련 단어가 포함됩니다."
    )

    result = gate.evaluate(
        title="테스트 제목",
        content=content,
        seed_keywords=["테스트", "본문"],
        topic_mode="cafe",
        rag_context=[],
    )

    assert result.passed is False
    assert result.error_code == "QUALITY_FAILED"
    codes = [item.code for item in result.issues]
    assert "illegal_loan_term" in codes


def test_quality_gate_requires_rag_sources_for_finance_topic():
    gate = QualityGate(min_content_chars=300)
    content = "경제 기사 요약입니다. " * 30

    result = gate.evaluate(
        title="금리 전망",
        content=content,
        seed_keywords=["금리", "전망"],
        topic_mode="finance",
        rag_context=[],
    )

    assert result.passed is False
    codes = [item.code for item in result.issues]
    assert "rag_source_missing" in codes


def test_quality_gate_repair_masks_and_extends_content():
    gate = QualityGate(min_content_chars=350)
    raw_content = "짧은 글입니다. 불법 대출 문구 포함."
    result = gate.evaluate(
        title="수정 테스트",
        content=raw_content,
        seed_keywords=["수정", "테스트"],
        topic_mode="cafe",
        rag_context=[],
    )

    repaired = gate.repair_content(
        content=raw_content,
        issues=result.issues,
        title="수정 테스트",
        seed_keywords=["수정", "테스트"],
    )
    assert "[민감표현 제거]" in repaired
    assert len(repaired) > len(raw_content)
