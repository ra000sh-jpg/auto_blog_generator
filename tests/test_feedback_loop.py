from __future__ import annotations

from pathlib import Path

from modules.automation.job_store import JobStore
from modules.llm.content_generator import ContentGenerator


def _build_store(tmp_path: Path) -> JobStore:
    db_path = tmp_path / "feedback_loop.db"
    return JobStore(db_path=str(db_path))


def test_feedback_candidate_promotes_after_min_observations(tmp_path: Path):
    store = _build_store(tmp_path)

    latest = None
    for _ in range(4):
        latest = store.record_feedback_suggestion_observation(
            suggestion_text="이미지 사이 여백을 조금 더 넓히세요",
            visual_score=74.0,
        )
        assert latest is not None
        assert latest["status"] == "observing"
        assert latest["promoted"] is False

    latest = store.record_feedback_suggestion_observation(
        suggestion_text="이미지 사이 여백을 조금 더 넓히세요",
        visual_score=76.0,
    )
    assert latest is not None
    assert latest["mention_count"] == 5
    assert latest["status"] == "pending_approval"
    assert latest["promoted"] is True


def test_feedback_candidate_approve_creates_active_rule_and_is_idempotent(tmp_path: Path):
    store = _build_store(tmp_path)

    candidate = None
    for _ in range(5):
        candidate = store.record_feedback_suggestion_observation(
            suggestion_text="문단 길이를 짧게 유지하고 핵심 문장을 앞에 배치하세요",
            visual_score=82.0,
        )
    assert candidate is not None
    assert candidate["status"] == "pending_approval"

    prepared = store.prepare_feedback_candidate_notification(candidate["id"], callback_ttl_hours=24)
    assert prepared is not None
    token = prepared["callback_token"]
    assert token

    approved = store.apply_feedback_candidate_action(
        candidate_id=candidate["id"],
        action="approve",
        callback_token=token,
    )
    assert approved["ok"] is True
    assert approved["status"] == "approved"

    active_rules = store.list_active_feedback_rules(limit=3)
    assert len(active_rules) == 1
    assert "문단 길이를 짧게" in active_rules[0]["rule_text"]

    duplicate = store.apply_feedback_candidate_action(
        candidate_id=candidate["id"],
        action="approve",
        callback_token=token,
    )
    assert duplicate["ok"] is False
    assert duplicate["reason"] == "already_handled"


def test_feedback_rule_rolls_back_on_negative_delta(tmp_path: Path):
    store = _build_store(tmp_path)

    candidate = None
    for _ in range(5):
        candidate = store.record_feedback_suggestion_observation(
            suggestion_text="썸네일과 본문 톤을 동일한 색상 계열로 통일하세요",
            visual_score=88.0,
        )
    assert candidate is not None

    prepared = store.prepare_feedback_candidate_notification(candidate["id"], callback_ttl_hours=24)
    assert prepared is not None

    approved = store.apply_feedback_candidate_action(
        candidate_id=candidate["id"],
        action="approve",
        callback_token=prepared["callback_token"],
    )
    assert approved["ok"] is True

    # baseline(약 88) 대비 낮은 점수를 누적해 롤백을 유도한다.
    updated = store.record_feedback_rule_application(
        applied_rules=["썸네일과 본문 톤을 동일한 색상 계열로 통일하세요"],
        visual_score=70.0,
    )
    assert updated == 1
    updated = store.record_feedback_rule_application(
        applied_rules=["썸네일과 본문 톤을 동일한 색상 계열로 통일하세요"],
        visual_score=68.0,
    )
    assert updated == 1
    updated = store.record_feedback_rule_application(
        applied_rules=["썸네일과 본문 톤을 동일한 색상 계열로 통일하세요"],
        visual_score=66.0,
    )
    assert updated == 1

    result = store.evaluate_feedback_rule_rollbacks(
        min_posts=3,
        noise_floor=5.0,
        keep_threshold=3.0,
    )
    assert result["rolled_back"] == 1

    active_rules = store.list_active_feedback_rules(limit=3)
    assert active_rules == []


# ---------------------------------------------------------------------------
# P1 회귀 방지: 피드백 규칙 스코프 필터링
# ---------------------------------------------------------------------------

def test_image_scope_rule_is_detected():
    """이미지/레이아웃 관련 규칙이 이미지 스코프로 올바르게 분류되는지 확인한다."""
    image_rules = [
        "이미지 사이 여백을 조금 더 넓히세요",
        "썸네일 크기를 일정하게 유지하세요",
        "사진과 텍스트 배치 간격을 조정하세요",
        "thumbnail resolution should be higher",
        "이미지 삽입 위치를 섹션 중간으로 옮기세요",
    ]
    for rule in image_rules:
        assert ContentGenerator._is_image_scope_rule(rule), (
            f"이미지 규칙이 필터링되지 않음: {rule!r}"
        )


def test_text_scope_rule_is_not_filtered():
    """텍스트 품질 규칙이 이미지 스코프로 오분류되지 않는지 확인한다."""
    text_rules = [
        "문단 길이를 짧게 유지하고 핵심 문장을 앞에 배치하세요",
        "결론 단락에서 본문 내용을 반복하지 마세요",
        "키워드를 3회 이상 반복하지 마세요",
        "독자에게 말을 거는 문장을 섹션당 1회 포함하세요",
        "동일한 접속사를 연속으로 사용하지 마세요",
    ]
    for rule in text_rules:
        assert not ContentGenerator._is_image_scope_rule(rule), (
            f"텍스트 규칙이 이미지 스코프로 오분류됨: {rule!r}"
        )


def test_filter_rules_for_text_stage_removes_image_rules():
    """텍스트 단계 필터링이 이미지 규칙만 제거하고 텍스트 규칙은 유지하는지 확인한다."""
    mixed_rules = [
        "이미지 사이 여백을 조금 더 넓히세요",     # 이미지 스코프(여백, 이미지) → 제거
        "문단 길이를 짧게 유지하세요",              # 텍스트 스코프 → 유지
        "사진 해상도를 높여주세요",                 # 이미지 스코프(사진, 해상도) → 제거
        "키워드를 자연스럽게 배치하세요",           # 텍스트 스코프 → 유지
    ]
    filtered = ContentGenerator._filter_rules_for_text_stage(mixed_rules)
    assert len(filtered) == 2, f"예상 2개, 실제 {len(filtered)}개: {filtered}"
    assert "문단" in filtered[0]
    assert "키워드" in filtered[1]


# ---------------------------------------------------------------------------
# P2 회귀 방지: Quality/Voice 레이어 분리 확인
# ---------------------------------------------------------------------------

def test_quality_layer_system_prompt_has_no_tone_rules():
    """QUALITY_LAYER_SYSTEM_PROMPT에 말투 처방 규칙이 없는지 확인한다 (레이어 분리 보장).

    프롬프트는 "말투 연기는 하지 않습니다" 같은 메타 설명을 포함할 수 있으나,
    "~해요 체로 작성", "반말로 작성" 등 실제 말투를 처방하는 패턴은 없어야 한다.
    """
    from modules.llm.prompts import QUALITY_LAYER_SYSTEM_PROMPT

    # 실제로 특정 말투를 처방하는 패턴만 검사 (단순 메타 언급은 허용)
    tone_prescription_patterns = [
        "~해요 체",
        "~거든요 체",
        "반말로",
        "반말을 사용",
        "어미를 사용",
        "친근한 말투로 작성",
    ]
    for pattern in tone_prescription_patterns:
        assert pattern not in QUALITY_LAYER_SYSTEM_PROMPT, (
            f"QUALITY_LAYER_SYSTEM_PROMPT에 말투 처방 패턴 발견: {pattern!r}"
        )

    # Quality/Voice 레이어 분리 메타 설명이 존재하는지 검증
    assert "Voice" in QUALITY_LAYER_SYSTEM_PROMPT, (
        "QUALITY_LAYER_SYSTEM_PROMPT에 Voice 단계 분리 설명이 없습니다"
    )


def test_voice_length_ratio_constants_are_sane():
    """Voice Rewrite 길이 허용치 상수가 합리적인 범위인지 확인한다."""
    from modules import constants
    assert 0.70 <= constants.VOICE_REWRITE_MIN_LENGTH_RATIO < 1.0, (
        "하한 비율이 비정상적입니다"
    )
    assert 1.0 < constants.VOICE_REWRITE_MAX_LENGTH_RATIO <= 2.0, (
        "상한 비율이 비정상적입니다"
    )
    # 이전에 Voice Rewrite 무효화를 유발한 1.15 임계값으로 회귀하지 않았는지 확인
    assert constants.VOICE_REWRITE_MAX_LENGTH_RATIO > 1.15, (
        "1.15 이하로 회귀하면 Voice Rewrite가 적용되지 않는 문제 재발 (이전 버그 #voice-ratio)"
    )
