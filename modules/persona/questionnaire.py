"""온보딩 페르소나 질문지 뱅크와 점수화 유틸리티."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

QUESTIONNAIRE_VERSION = "v1"
QUESTIONNAIRE_DIMENSIONS: Tuple[str, ...] = (
    "structure",
    "evidence",
    "distance",
    "criticism",
    "density",
)


@dataclass(frozen=True)
class QuestionnaireOption:
    """단일 질문 선택지."""

    option_id: str
    label: str
    description: str
    effects: Mapping[str, int]


@dataclass(frozen=True)
class QuestionnaireQuestion:
    """상황형 질문 정의."""

    question_id: str
    title: str
    scenario: str
    target_dimension: str
    weight: int
    options: Tuple[QuestionnaireOption, ...]


QUESTION_BANK: Tuple[QuestionnaireQuestion, ...] = (
    QuestionnaireQuestion(
        question_id="q1_opening_flow",
        title="글 첫 문단 시작 방식",
        scenario="새 카페 리뷰를 쓸 때, 첫 문단을 어떻게 여나요?",
        target_dimension="structure",
        weight=2,
        options=(
            QuestionnaireOption(
                option_id="a_scan_then_map",
                label="한 줄 요약 후 목차를 먼저 제시한다",
                description="독자가 전체 구조를 바로 파악하도록 안내",
                effects={"structure": 2, "evidence": 1, "density": 1},
            ),
            QuestionnaireOption(
                option_id="b_story_then_point",
                label="짧은 에피소드 후 핵심 포인트를 꺼낸다",
                description="흐름은 자연스럽되 구조는 중간 강도",
                effects={"structure": 0, "distance": -1},
            ),
            QuestionnaireOption(
                option_id="c_emotion_first",
                label="느낌과 감정을 먼저 길게 풀어낸다",
                description="감성 몰입형 전개",
                effects={"structure": -2, "evidence": -1, "distance": -1, "density": -1},
            ),
        ),
    ),
    QuestionnaireQuestion(
        question_id="q2_evidence_conflict",
        title="근거가 엇갈릴 때 선택",
        scenario="두 자료가 서로 다른 결론을 말할 때 당신의 기본 행동은?",
        target_dimension="evidence",
        weight=3,
        options=(
            QuestionnaireOption(
                option_id="a_add_sources",
                label="추가 출처를 더 찾아 수치 비교표를 만든다",
                description="검증 우선, 출처 중심",
                effects={"evidence": 2, "structure": 1, "distance": 1, "density": 1},
            ),
            QuestionnaireOption(
                option_id="b_use_experience",
                label="내 경험과 사례를 중심으로 설명한다",
                description="경험 기반 설득",
                effects={"evidence": -1, "distance": -1},
            ),
            QuestionnaireOption(
                option_id="c_intuition_pick",
                label="직감에 맞는 쪽 하나를 택해 밀어붙인다",
                description="속도 우선, 검증 약함",
                effects={"evidence": -2, "distance": -2, "density": -1},
            ),
        ),
    ),
    QuestionnaireQuestion(
        question_id="q3_reader_distance",
        title="독자 반박 댓글 대응",
        scenario="독자가 강하게 반박 댓글을 달았을 때 어떻게 답하나요?",
        target_dimension="distance",
        weight=2,
        options=(
            QuestionnaireOption(
                option_id="a_calm_data_reply",
                label="데이터와 근거를 붙여 차분히 반박한다",
                description="전문가 거리 유지",
                effects={"distance": 2, "criticism": 1, "evidence": 1},
            ),
            QuestionnaireOption(
                option_id="b_empathy_then_reply",
                label="공감 문장 후 내 의견을 부드럽게 덧붙인다",
                description="친구 같은 거리",
                effects={"distance": 0, "criticism": -1},
            ),
            QuestionnaireOption(
                option_id="c_direct_counter",
                label="오해라고 직설적으로 바로 잡는다",
                description="강한 반박 톤",
                effects={"distance": -2, "criticism": 2},
            ),
        ),
    ),
    QuestionnaireQuestion(
        question_id="q4_critique_level",
        title="단점 리뷰 표현 방식",
        scenario="추천하지 않는 제품을 리뷰할 때 어떤 문장을 쓰나요?",
        target_dimension="criticism",
        weight=2,
        options=(
            QuestionnaireOption(
                option_id="a_direct_with_fix",
                label="문제점을 명확히 지적하고 개선안을 제시한다",
                description="직설 + 책임형 비판",
                effects={"criticism": 2, "structure": 1},
            ),
            QuestionnaireOption(
                option_id="b_soften_phrase",
                label="아쉬운 점을 완곡하게 표현한다",
                description="관계 손상 최소화",
                effects={"criticism": -1, "distance": 1},
            ),
            QuestionnaireOption(
                option_id="c_skip_negative",
                label="부정 요소는 최대한 생략한다",
                description="안전하지만 정보 손실",
                effects={"criticism": -2, "distance": 2, "evidence": -1},
            ),
        ),
    ),
    QuestionnaireQuestion(
        question_id="q5_density_tradeoff",
        title="정보량 vs 읽기 편의",
        scenario="같은 길이 제한에서 무엇을 우선하나요?",
        target_dimension="density",
        weight=2,
        options=(
            QuestionnaireOption(
                option_id="a_checklist_numbers",
                label="숫자, 체크리스트, 요약표를 최대한 넣는다",
                description="정보 밀도 우선",
                effects={"density": 2, "structure": 1, "evidence": 1},
            ),
            QuestionnaireOption(
                option_id="b_key3_then_detail",
                label="핵심 3줄 요약 후 필요한 부분만 상세히 푼다",
                description="균형형",
                effects={"density": 0, "structure": 1},
            ),
            QuestionnaireOption(
                option_id="c_light_story",
                label="느낌 중심으로 가볍게 읽히는 흐름을 만든다",
                description="가독성 우선",
                effects={"density": -2, "evidence": -1},
            ),
        ),
    ),
    QuestionnaireQuestion(
        question_id="q6_story_structure",
        title="문단 전개 패턴",
        scenario="실전 팁 글을 쓸 때 가장 익숙한 흐름은?",
        target_dimension="structure",
        weight=1,
        options=(
            QuestionnaireOption(
                option_id="a_problem_cause_solution",
                label="문제 정의 → 원인 → 해결책 순서",
                description="논리 전개형",
                effects={"structure": 2, "distance": 1},
            ),
            QuestionnaireOption(
                option_id="b_case_then_lesson",
                label="사례 제시 → 배운 점 정리",
                description="경험+정리형",
                effects={"structure": 1, "distance": 0},
            ),
            QuestionnaireOption(
                option_id="c_hook_conversation",
                label="대화체 훅으로 시작해 즉흥적으로 전개",
                description="캐주얼 몰입형",
                effects={"structure": -1, "distance": -2, "density": -1},
            ),
        ),
    ),
    QuestionnaireQuestion(
        question_id="q7_uncertain_fact",
        title="불확실 정보 처리",
        scenario="핵심 수치가 불확실할 때 어떻게 처리하나요?",
        target_dimension="evidence",
        weight=3,
        options=(
            QuestionnaireOption(
                option_id="a_mark_unknown",
                label="불확실하다고 명시하고 확인 전엔 단정하지 않는다",
                description="정확도 우선",
                effects={"evidence": 2, "distance": 2, "criticism": 1},
            ),
            QuestionnaireOption(
                option_id="b_probabilistic_write",
                label="가능성 표현을 붙여 글을 이어간다",
                description="실무형 타협",
                effects={"evidence": 0, "distance": 0},
            ),
            QuestionnaireOption(
                option_id="c_assertive_guess",
                label="독자를 위해 확정형 문장으로 정리한다",
                description="속도 우선, 리스크 증가",
                effects={"evidence": -2, "distance": -2, "criticism": 1},
            ),
        ),
    ),
)

_QUESTION_INDEX: Dict[str, QuestionnaireQuestion] = {item.question_id: item for item in QUESTION_BANK}
_OPTION_INDEX: Dict[Tuple[str, str], QuestionnaireOption] = {}
for _question in QUESTION_BANK:
    for _option in _question.options:
        _OPTION_INDEX[(_question.question_id, _option.option_id)] = _option


def _clamp_score(value: int) -> int:
    """점수를 0~100 범위로 고정한다."""
    return max(0, min(100, int(value)))


def get_question_bank_payload(required_count: int = 5) -> Dict[str, object]:
    """프론트엔드에 전달할 질문지 스키마를 반환한다."""
    questions: List[Dict[str, object]] = []
    for question in QUESTION_BANK:
        questions.append(
            {
                "question_id": question.question_id,
                "title": question.title,
                "scenario": question.scenario,
                "target_dimension": question.target_dimension,
                "weight": question.weight,
                "options": [
                    {
                        "option_id": option.option_id,
                        "label": option.label,
                        "description": option.description,
                        "effects": dict(option.effects),
                    }
                    for option in question.options
                ],
            }
        )
    return {
        "version": QUESTIONNAIRE_VERSION,
        "required_count": max(1, min(len(QUESTION_BANK), int(required_count))),
        "dimensions": list(QUESTIONNAIRE_DIMENSIONS),
        "questions": questions,
    }


def _ideal_dimension_caps() -> Dict[str, int]:
    """차원별 최대 분모(정규화 기준)를 계산한다."""
    caps = {dimension: 0 for dimension in QUESTIONNAIRE_DIMENSIONS}
    for question in QUESTION_BANK:
        for dimension in QUESTIONNAIRE_DIMENSIONS:
            if any(int(option.effects.get(dimension, 0)) != 0 for option in question.options):
                caps[dimension] += 2 * question.weight
    return caps


def score_questionnaire_answers(answer_pairs: Sequence[Tuple[str, str]]) -> Dict[str, object]:
    """질문지 답변을 5차원 점수로 환산한다."""
    normalized_answers: Dict[str, str] = {}
    for question_id, option_id in answer_pairs:
        resolved_question_id = str(question_id or "").strip()
        resolved_option_id = str(option_id or "").strip()
        if not resolved_question_id or not resolved_option_id:
            continue
        if (resolved_question_id, resolved_option_id) not in _OPTION_INDEX:
            continue
        normalized_answers[resolved_question_id] = resolved_option_id

    answered_items: List[Tuple[QuestionnaireQuestion, QuestionnaireOption]] = []
    for question_id, option_id in normalized_answers.items():
        question = _QUESTION_INDEX.get(question_id)
        option = _OPTION_INDEX.get((question_id, option_id))
        if question and option:
            answered_items.append((question, option))

    dimension_scores = {dimension: 50 for dimension in QUESTIONNAIRE_DIMENSIONS}
    confidence_scores = {dimension: 0.0 for dimension in QUESTIONNAIRE_DIMENSIONS}
    ideal_caps = _ideal_dimension_caps()

    for dimension in QUESTIONNAIRE_DIMENSIONS:
        weighted_sum = 0
        weighted_cap = 0
        for question, option in answered_items:
            effect = int(option.effects.get(dimension, 0))
            if effect == 0:
                continue
            weighted_sum += effect * question.weight
            weighted_cap += 2 * question.weight

        if weighted_cap > 0:
            normalized = weighted_sum / float(weighted_cap)
            dimension_scores[dimension] = _clamp_score(round(50 + (normalized * 35)))
        else:
            dimension_scores[dimension] = 50

        ideal_cap = ideal_caps.get(dimension, 0)
        confidence = 0.0 if ideal_cap <= 0 else min(1.0, weighted_cap / float(ideal_cap))
        confidence_scores[dimension] = round(confidence, 3)

    missing_question_ids = [item.question_id for item in QUESTION_BANK if item.question_id not in normalized_answers]
    total_questions = len(QUESTION_BANK)
    answered_count = len(answered_items)
    completion_ratio = 0.0 if total_questions <= 0 else round(answered_count / float(total_questions), 3)

    return {
        "version": QUESTIONNAIRE_VERSION,
        "scores": dimension_scores,
        "answered_count": answered_count,
        "total_questions": total_questions,
        "completion_ratio": completion_ratio,
        "dimension_confidence": confidence_scores,
        "resolved_answers": [
            {"question_id": question.question_id, "option_id": option.option_id}
            for question, option in answered_items
        ],
        "missing_question_ids": missing_question_ids,
    }

