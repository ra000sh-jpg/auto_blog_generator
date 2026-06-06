"""글쓰기 전략 라우터.

경제 글은 투자 판단에 영향을 줄 수 있으므로 고정 템플릿보다
의도, 중심축, 블록 구성을 먼저 정한 뒤 생성 프롬프트에 주입한다.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence


class WritingIntent(str, Enum):
    """검색자가 글에서 기대하는 해결 방식."""

    QUESTION_ANSWER = "question_answer"
    COMPARE_DECIDE = "compare_decide"
    EXPERIENCE_REVIEW = "experience_review"
    POLICY_ACTION = "policy_action"
    NEWS_EXPLAIN = "news_explain"
    RISK_CHECK = "risk_check"


@dataclass(frozen=True)
class WritingAxisMix:
    """글 중심축 비율."""

    experience: int = 0
    evidence: int = 0
    comparison: int = 0
    checklist: int = 0
    action: int = 0
    risk: int = 0

    def summary(self) -> str:
        """사람이 읽는 전략 비율 요약을 반환한다."""

        labels = (
            ("경험", self.experience),
            ("근거", self.evidence),
            ("비교", self.comparison),
            ("체크리스트", self.checklist),
            ("실행", self.action),
            ("리스크", self.risk),
        )
        parts = [f"{label} {value}%" for label, value in labels if int(value or 0) > 0]
        return " + ".join(parts) if parts else "균형형"

    def tag_value(self) -> str:
        """태그에 넣을 ASCII 요약을 반환한다."""

        parts = (
            f"exp{self.experience}",
            f"ev{self.evidence}",
            f"cmp{self.comparison}",
            f"chk{self.checklist}",
            f"act{self.action}",
            f"risk{self.risk}",
        )
        return "_".join(part for part in parts if not part.endswith("0"))


@dataclass(frozen=True)
class WritingBlockPlan:
    """본문 블록 구성."""

    block_id: str
    label: str
    instruction: str


@dataclass(frozen=True)
class WritingStrategyPlan:
    """생성 프롬프트에 넘길 글쓰기 전략."""

    strategy_id: str
    label: str
    intent: WritingIntent
    axis_mix: WritingAxisMix
    blocks: tuple[WritingBlockPlan, ...]
    visual_role: str
    forbidden_patterns: tuple[str, ...] = ()
    safe_terms: tuple[str, ...] = ()
    required_guidelines: tuple[str, ...] = ()

    def to_snapshot(self) -> dict:
        """quality_snapshot에 저장할 수 있는 dict로 변환한다."""

        return {
            "strategy_id": self.strategy_id,
            "label": self.label,
            "intent": self.intent.value,
            "intent_label": _INTENT_LABELS.get(self.intent, self.intent.value),
            "axis_summary": self.axis_mix.summary(),
            "axis_mix": asdict(self.axis_mix),
            "blocks": [asdict(block) for block in self.blocks],
            "visual_role": self.visual_role,
        }


_INTENT_LABELS: Mapping[WritingIntent, str] = {
    WritingIntent.QUESTION_ANSWER: "궁금증 해결",
    WritingIntent.COMPARE_DECIDE: "비교 판단",
    WritingIntent.EXPERIENCE_REVIEW: "경험 후기",
    WritingIntent.POLICY_ACTION: "정책/신청",
    WritingIntent.NEWS_EXPLAIN: "뉴스 해설",
    WritingIntent.RISK_CHECK: "리스크 점검",
}


def _block(block_id: str, label: str, instruction: str) -> WritingBlockPlan:
    return WritingBlockPlan(block_id=block_id, label=label, instruction=instruction)


_MARKET_FORBIDDEN = (
    "매수",
    "매도",
    "목표가 확정",
    "수익 보장",
    "무조건",
    "급등주",
    "상한가 따라잡기",
    "추천 종목",
)

_MARKET_SAFE_TERMS = (
    "확인 대상",
    "공부 포인트",
    "관찰할 변수",
    "틀릴 수 있는 조건",
)

_MARKET_GUIDELINES = (
    "투자 추천이 아니라 시장 공부와 판단 기준 정리로 작성하세요.",
    "수치에는 기준일, 출처, 해석 한계를 함께 적으세요.",
    "반대 신호와 틀릴 수 있는 조건을 반드시 포함하세요.",
    "문단 말미에서 특정 종목 매매를 유도하지 마세요.",
)


MARKET_STRATEGIES: Mapping[str, WritingStrategyPlan] = {
    "market_preopen_scenario": WritingStrategyPlan(
        strategy_id="market_preopen_scenario",
        label="국장전 시나리오 브리핑형",
        intent=WritingIntent.NEWS_EXPLAIN,
        axis_mix=WritingAxisMix(evidence=35, checklist=25, action=15, risk=25),
        blocks=(
            _block("overnight_context", "밤사이 기준", "전일 미국장, 금리, 환율, 주요 뉴스 중 오늘 국장에 연결되는 변수만 정리합니다."),
            _block("today_variables", "오늘 변수", "장 시작 전 확인할 지수, 섹터, 수급 변수를 조건문으로 제시합니다."),
            _block("focus_sectors", "관심 섹터", "관심 섹터는 추천이 아니라 관찰 대상으로 표현합니다."),
            _block("opposite_signal", "반대 신호", "예상과 다르게 움직일 때 확인할 신호를 함께 적습니다."),
            _block("beginner_checklist", "초심자 체크리스트", "초심자가 오늘 무리하지 않기 위한 질문 3개로 마무리합니다."),
        ),
        visual_role="오늘 변수와 반대 신호를 한눈에 보는 시장노트 카드",
        forbidden_patterns=_MARKET_FORBIDDEN,
        safe_terms=_MARKET_SAFE_TERMS,
        required_guidelines=_MARKET_GUIDELINES,
    ),
    "market_issue_explain": WritingStrategyPlan(
        strategy_id="market_issue_explain",
        label="시장 이슈 해설형",
        intent=WritingIntent.NEWS_EXPLAIN,
        axis_mix=WritingAxisMix(evidence=35, experience=15, action=20, risk=30),
        blocks=(
            _block("what_happened", "무슨 일이 있었나", "뉴스나 시장 반응을 한 문단으로 요약합니다."),
            _block("why_market_moves", "왜 시장이 반응하나", "가격보다 원인과 영향 경로를 쉽게 설명합니다."),
            _block("related_area", "관련 업종", "관련 업종과 종목은 관찰 대상으로만 다룹니다."),
            _block("numbers_to_check", "확인할 숫자", "확인해야 할 수치와 기준일을 표로 정리합니다."),
            _block("risk_limit", "주의점", "과열, 단일 뉴스 의존, 해석 한계를 적습니다."),
        ),
        visual_role="이슈 원인-영향-확인 숫자 표",
        forbidden_patterns=_MARKET_FORBIDDEN,
        safe_terms=_MARKET_SAFE_TERMS,
        required_guidelines=_MARKET_GUIDELINES,
    ),
    "macro_policy_note": WritingStrategyPlan(
        strategy_id="macro_policy_note",
        label="지표/정책 노트형",
        intent=WritingIntent.NEWS_EXPLAIN,
        axis_mix=WritingAxisMix(evidence=45, comparison=15, checklist=15, risk=25),
        blocks=(
            _block("indicator_summary", "지표 한 줄 요약", "CPI, FOMC, 한국은행, 환율, 수출입 등 핵심 지표를 한 줄로 정리합니다."),
            _block("before_after", "이전 대비 변화", "직전 수치나 시장 예상과의 차이를 설명합니다."),
            _block("market_path", "시장 영향 경로", "금리, 환율, 성장주, 외국인 수급으로 이어지는 경로를 적습니다."),
            _block("next_date", "다음 일정", "다음 발표나 확인 시점을 남깁니다."),
            _block("limit", "해석 한계", "단일 지표로 결론 내리지 않는 이유를 적습니다."),
        ),
        visual_role="지표 변화와 다음 일정을 정리하는 표",
        forbidden_patterns=_MARKET_FORBIDDEN,
        safe_terms=_MARKET_SAFE_TERMS,
        required_guidelines=_MARKET_GUIDELINES,
    ),
    "sector_fact_check": WritingStrategyPlan(
        strategy_id="sector_fact_check",
        label="테마 검증형",
        intent=WritingIntent.RISK_CHECK,
        axis_mix=WritingAxisMix(evidence=35, comparison=10, checklist=15, risk=40),
        blocks=(
            _block("claim", "시장 주장", "테마나 뉴스에서 퍼지는 주장을 먼저 분리합니다."),
            _block("verified", "확인된 근거", "확인 가능한 근거와 아직 확인되지 않은 부분을 나눕니다."),
            _block("unknown", "아직 모르는 부분", "숫자, 일정, 수급 중 비어 있는 정보를 적습니다."),
            _block("danger_words", "조심할 표현", "급등, 무조건, 목표가 같은 표현을 왜 조심해야 하는지 설명합니다."),
            _block("study_question", "공부 질문", "독자가 스스로 확인할 질문으로 마무리합니다."),
        ),
        visual_role="오해/확인/미확인 구분 카드",
        forbidden_patterns=_MARKET_FORBIDDEN,
        safe_terms=_MARKET_SAFE_TERMS,
        required_guidelines=_MARKET_GUIDELINES,
    ),
    "investor_reflection_note": WritingStrategyPlan(
        strategy_id="investor_reflection_note",
        label="투자 복기/습관 노트형",
        intent=WritingIntent.EXPERIENCE_REVIEW,
        axis_mix=WritingAxisMix(experience=35, evidence=20, checklist=20, risk=25),
        blocks=(
            _block("real_situation", "실제 상황", "시장 뉴스에서 개인 투자자가 흔들릴 만한 지점을 짚습니다."),
            _block("emotion", "흔들린 지점", "초심자의 불안, 시간, 현금흐름 같은 생활 제약을 자연스럽게 넣습니다."),
            _block("standard", "판단 기준", "수익률보다 먼저 볼 기준을 기록/체크리스트로 풀어 씁니다."),
            _block("guardrail", "리스크 장치", "하지 않을 행동과 확인할 조건을 적습니다."),
            _block("next_question", "다음 행동 질문", "오늘 함께 확인할 공부 질문 2~3개로 끝냅니다."),
        ),
        visual_role="복기 질문과 리스크 장치 카드",
        forbidden_patterns=_MARKET_FORBIDDEN,
        safe_terms=_MARKET_SAFE_TERMS,
        required_guidelines=_MARKET_GUIDELINES,
    ),
}


_CATEGORY_STRATEGIES: Mapping[str, WritingStrategyPlan] = {
    "it_news_explain": WritingStrategyPlan(
        "it_news_explain",
        "IT 뉴스 해설 전략",
        WritingIntent.NEWS_EXPLAIN,
        WritingAxisMix(evidence=35, action=25, comparison=15, risk=25),
        (
            _block("what", "무슨 일", "기술 변화나 출시 소식을 쉽게 설명합니다."),
            _block("why", "왜 중요한지", "독자 생활과 운영에 연결되는 이유를 적습니다."),
            _block("apply", "어떻게 볼지", "실사용 또는 관찰 포인트를 제시합니다."),
        ),
        "핵심 변화 요약 카드",
    ),
    "it_practical_apply": WritingStrategyPlan(
        "it_practical_apply",
        "IT 실사용 적용 전략",
        WritingIntent.QUESTION_ANSWER,
        WritingAxisMix(experience=25, evidence=15, checklist=25, action=35),
        (
            _block("problem", "문제 상황", "독자가 겪는 실제 문제에서 시작합니다."),
            _block("workflow", "실행 순서", "앱/툴 적용 순서를 단계별로 정리합니다."),
            _block("fit", "맞는 사람", "잘 맞는 경우와 안 맞는 경우를 나눕니다."),
        ),
        "실행 워크플로 카드",
    ),
    "it_compare_decide": WritingStrategyPlan(
        "it_compare_decide",
        "IT 비교 선택 전략",
        WritingIntent.COMPARE_DECIDE,
        WritingAxisMix(evidence=25, comparison=45, checklist=20, risk=10),
        (
            _block("targets", "비교 대상", "비교 대상을 먼저 명확히 둡니다."),
            _block("criteria", "선택 기준", "비용, 난이도, 활용도를 기준으로 비교합니다."),
            _block("recommend_fit", "추천 대상", "누구에게 무엇이 맞는지 조건부로 정리합니다."),
        ),
        "비교표 카드",
    ),
    "health_evidence_note": WritingStrategyPlan(
        "health_evidence_note",
        "건강 근거 정리 전략",
        WritingIntent.NEWS_EXPLAIN,
        WritingAxisMix(evidence=50, checklist=15, action=10, risk=25),
        (
            _block("claim", "주장", "건강 주장을 먼저 분리합니다."),
            _block("evidence", "근거 수준", "연구/가이드라인/전문가 의견을 구분합니다."),
            _block("care", "주의할 사람", "전문가 상담이 필요한 경우를 밝힙니다."),
        ),
        "근거 수준 표",
        required_guidelines=("진단, 치료, 완치, 보장 표현을 쓰지 마세요.",),
    ),
    "health_daily_habit": WritingStrategyPlan(
        "health_daily_habit",
        "건강 생활 적용 전략",
        WritingIntent.QUESTION_ANSWER,
        WritingAxisMix(experience=20, evidence=25, checklist=25, action=20, risk=10),
        (
            _block("today_problem", "오늘 문제", "생활 속 불편에서 시작합니다."),
            _block("small_action", "작은 실천", "무리 없는 실천 단위를 제시합니다."),
            _block("fail_point", "실패 지점", "실패하기 쉬운 조건과 안전선을 적습니다."),
        ),
        "생활 체크리스트 카드",
    ),
    "health_myth_check": WritingStrategyPlan(
        "health_myth_check",
        "건강 오해 점검 전략",
        WritingIntent.RISK_CHECK,
        WritingAxisMix(evidence=40, checklist=10, action=10, risk=40),
        (
            _block("viral_claim", "유행 주장", "왜 혹하기 쉬운지 설명합니다."),
            _block("fact", "확인된 사실", "확인된 근거와 과장된 부분을 분리합니다."),
            _block("safe_alternative", "안전한 대안", "전문가 상담과 안전한 대안을 적습니다."),
        ),
        "오해/사실 구분 카드",
        required_guidelines=("진단, 치료, 완치, 보장 표현을 쓰지 마세요.",),
    ),
    "parenting_empathy_story": WritingStrategyPlan(
        "parenting_empathy_story",
        "육아 경험 공감 전략",
        WritingIntent.EXPERIENCE_REVIEW,
        WritingAxisMix(experience=45, evidence=20, checklist=20, action=15),
        (
            _block("scene", "실제 상황", "부모가 겪는 장면에서 시작합니다."),
            _block("child_view", "아이 관점", "발달과 감정 관점으로 해석합니다."),
            _block("today_try", "오늘 해볼 대응", "집에서 해볼 대응을 제안합니다."),
        ),
        "상황별 대응 카드",
    ),
    "parenting_home_apply": WritingStrategyPlan(
        "parenting_home_apply",
        "육아 우리 집 적용 전략",
        WritingIntent.QUESTION_ANSWER,
        WritingAxisMix(experience=35, evidence=25, checklist=15, action=25),
        (
            _block("source_point", "자료 핵심", "외부 자료의 핵심을 짧게 정리합니다."),
            _block("home_case", "우리 집 적용", "먹똥맘/우리 집 상황에 맞춘 적용을 적습니다."),
            _block("fit_limit", "맞는 경우/안 맞는 경우", "아이마다 다를 수 있는 지점을 남깁니다."),
        ),
        "우리 집 적용 워크플로 카드",
    ),
    "parenting_checklist": WritingStrategyPlan(
        "parenting_checklist",
        "육아 체크리스트 전략",
        WritingIntent.QUESTION_ANSWER,
        WritingAxisMix(experience=20, evidence=25, checklist=40, action=15),
        (
            _block("define", "상황 정의", "개월수, 발달, 준비 상황을 먼저 정리합니다."),
            _block("checklist", "체크리스트", "바로 확인할 항목을 표나 목록으로 만듭니다."),
            _block("routine", "루틴 조정", "집에서 적용할 순서를 제시합니다."),
        ),
        "체크리스트/루틴표 카드",
    ),
}


_RISK_FORCE_RE = re.compile(r"급등주|상한가|테마\s*과열|작전주|루머|찌라시|목표가\s*확정|수익\s*보장", re.I)
_MACRO_RE = re.compile(r"FOMC|CPI|PCE|GDP|ISM|고용|실업률|한국은행|금통위|기준금리|환율|수출입|물가|금리", re.I)
_REFLECTION_RE = re.compile(r"복기|습관|기록|리스크\s*관리|초심자|공부\s*노트|통찰|기준", re.I)


def select_market_writing_strategy(
    *,
    title: str,
    tags: Sequence[str] | None = None,
    seed_keywords: Sequence[str] | None = None,
) -> WritingStrategyPlan:
    """경제 글의 제목/태그/키워드로 전략을 고른다."""

    tag_strategy = _first_tag_value(tags or (), "writing_strategy:")
    if tag_strategy in MARKET_STRATEGIES:
        return MARKET_STRATEGIES[tag_strategy]

    text = _joined_text([title, *(seed_keywords or ()), *(tags or ())])
    if _RISK_FORCE_RE.search(text):
        return MARKET_STRATEGIES["sector_fact_check"]
    if "market_slot:kr_preopen" in {str(tag).strip().lower() for tag in (tags or ())}:
        return MARKET_STRATEGIES["market_preopen_scenario"]
    if "market_slot:weekly_reflection" in {str(tag).strip().lower() for tag in (tags or ())}:
        return MARKET_STRATEGIES["investor_reflection_note"]
    if "market_slot:evergreen_insight" in {str(tag).strip().lower() for tag in (tags or ())}:
        return MARKET_STRATEGIES["investor_reflection_note"]
    if _MACRO_RE.search(text):
        return MARKET_STRATEGIES["macro_policy_note"]
    if _REFLECTION_RE.search(text):
        return MARKET_STRATEGIES["investor_reflection_note"]
    return MARKET_STRATEGIES["market_issue_explain"]


def select_category_writing_strategy(
    *,
    topic_mode: str,
    template_id: str,
    title: str = "",
    tags: Sequence[str] | None = None,
) -> WritingStrategyPlan | None:
    """확장 카테고리 템플릿을 전략 조합으로 변환한다."""

    del topic_mode, title
    tag_strategy = _first_tag_value(tags or (), "writing_strategy:")
    if tag_strategy in _CATEGORY_STRATEGIES:
        return _CATEGORY_STRATEGIES[tag_strategy]
    return _CATEGORY_STRATEGIES.get(str(template_id or "").strip())


def writing_strategy_tags(plan: WritingStrategyPlan) -> list[str]:
    """스케줄러가 job에 붙일 내부 태그를 만든다."""

    return [
        f"writing_strategy:{plan.strategy_id}",
        f"writing_intent:{plan.intent.value}",
        f"writing_axis:{plan.axis_mix.tag_value()}",
    ]


def render_strategy_prompt(plan: WritingStrategyPlan, *, heading: str = "글쓰기 전략 라우터 지시") -> str:
    """생성 프롬프트에 넣을 전략 지시문을 만든다."""

    blocks = "\n".join(
        f"  {index}. {block.label}: {block.instruction}"
        for index, block in enumerate(plan.blocks, start=1)
    )
    forbidden = ", ".join(plan.forbidden_patterns) if plan.forbidden_patterns else "-"
    safe_terms = ", ".join(plan.safe_terms) if plan.safe_terms else "-"
    guidelines = "\n".join(f"- {item}" for item in plan.required_guidelines)
    if not guidelines:
        guidelines = "- 외부 콘텐츠를 복사하지 말고 독자 문제 해결 흐름으로 재구성하세요."

    return f"""
[{heading}]
- 추천 전략: {plan.label} ({plan.strategy_id})
- 검색 의도: {_INTENT_LABELS.get(plan.intent, plan.intent.value)}
- 전략 비율: {plan.axis_mix.summary()}
- 블록 순서:
{blocks}
- 표/카드 역할: {plan.visual_role}
- 피할 표현: {forbidden}
- 권장 표현: {safe_terms}
{guidelines}
""".strip()


def summarize_strategy_for_message(payload: Mapping[str, object]) -> dict[str, str]:
    """텔레그램 메시지에 넣을 전략 요약을 만든다."""

    quality = payload.get("quality_snapshot", {})
    strategy = quality.get("writing_strategy", {}) if isinstance(quality, Mapping) else {}
    if not isinstance(strategy, Mapping):
        return {}
    label = str(strategy.get("label", "") or "").strip()
    intent = str(strategy.get("intent_label", "") or strategy.get("intent", "") or "").strip()
    axis = str(strategy.get("axis_summary", "") or "").strip()
    if not label and not intent and not axis:
        return {}
    return {"label": label, "intent": intent, "axis": axis}


def _first_tag_value(tags: Iterable[str], prefix: str) -> str:
    normalized_prefix = str(prefix or "").strip().lower()
    for tag in tags:
        raw = str(tag or "").strip()
        if raw.lower().startswith(normalized_prefix):
            return raw.split(":", 1)[1].strip()
    return ""


def _joined_text(values: Iterable[str]) -> str:
    return " ".join(str(value or "") for value in values).strip()
