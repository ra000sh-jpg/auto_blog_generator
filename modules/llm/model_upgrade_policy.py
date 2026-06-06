"""LLM 모델 자동 업그레이드 후보 판정 정책."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCandidateEvaluation:
    """신규 모델 후보 평가 결과."""

    provider: str
    model: str
    current_avg_cost_per_1m_usd: float
    candidate_avg_cost_per_1m_usd: float
    current_quality_score: float
    candidate_quality_score: float
    json_supported: bool
    tool_calls_supported: bool
    numeric_hallucination_count: int
    persona_score_delta: float
    consecutive_successes: int


@dataclass(frozen=True)
class ModelUpgradeDecision:
    """자동 전환 가능 여부."""

    action: str
    approved_for_auto_switch: bool
    reason: str


def decide_model_upgrade(candidate: ModelCandidateEvaluation) -> ModelUpgradeDecision:
    """새 모델을 자동 전환할지, 승인 대기 후보로 둘지 결정한다."""

    if candidate.candidate_avg_cost_per_1m_usd > candidate.current_avg_cost_per_1m_usd:
        return ModelUpgradeDecision(
            action="premium_candidate",
            approved_for_auto_switch=False,
            reason="후보 모델이 현재 모델보다 비싸므로 자동 전환하지 않는다.",
        )
    if not candidate.json_supported or not candidate.tool_calls_supported:
        return ModelUpgradeDecision(
            action="reject",
            approved_for_auto_switch=False,
            reason="JSON 출력 또는 tool calls 지원이 부족하다.",
        )
    if candidate.numeric_hallucination_count > 0:
        return ModelUpgradeDecision(
            action="reject",
            approved_for_auto_switch=False,
            reason="시장 브리핑 샘플에서 숫자 조작 위험이 발견됐다.",
        )
    if candidate.persona_score_delta < 0:
        return ModelUpgradeDecision(
            action="needs_review",
            approved_for_auto_switch=False,
            reason="윤서재 페르소나 문체 점수가 기존 모델보다 낮다.",
        )
    if candidate.consecutive_successes < 3:
        return ModelUpgradeDecision(
            action="needs_review",
            approved_for_auto_switch=False,
            reason="연속 API 성공 횟수가 부족하다.",
        )
    if candidate.candidate_quality_score < candidate.current_quality_score:
        return ModelUpgradeDecision(
            action="needs_review",
            approved_for_auto_switch=False,
            reason="품질 점수가 기존 모델보다 낮아 텔레그램 승인 후 전환한다.",
        )

    return ModelUpgradeDecision(
        action="auto_switch_candidate",
        approved_for_auto_switch=True,
        reason="비용, 품질, 안정성 조건을 모두 통과했다.",
    )
