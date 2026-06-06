"""카테고리 확장 글감 추천과 글 양식 선택."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class CreatorSource:
    """트래킹할 외부 채널 정보."""

    topic_mode: str
    platform: str
    channel_name: str
    url: str
    priority: int = 50


@dataclass(frozen=True)
class SourceItem:
    """외부 채널의 최신 글/영상/스레드 후보."""

    topic_mode: str
    title: str
    source_name: str
    platform: str
    url: str = ""
    summary: str = ""
    published_at: str = ""
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class CategoryPostTemplate:
    """카테고리별 글 양식."""

    template_id: str
    topic_mode: str
    label: str
    description: str
    structure_hint: str
    card_role: str
    trigger_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class CategoryOpportunityScore:
    """카테고리 글감 점수."""

    total: float
    freshness: float
    practical_fit: float
    source_reliability: float
    risk_penalty: float = 0.0


@dataclass(frozen=True)
class CategoryContentBrief:
    """스케줄러가 job으로 변환할 카테고리 글감 요약."""

    topic_mode: str
    category: str
    title: str
    angle: str
    template_id: str
    seed_keywords: tuple[str, ...]
    sources: tuple[CreatorSource, ...] = ()
    source_items: tuple[SourceItem, ...] = ()
    tags: tuple[str, ...] = ()
    score: CategoryOpportunityScore = field(
        default_factory=lambda: CategoryOpportunityScore(
            total=60.0,
            freshness=20.0,
            practical_fit=25.0,
            source_reliability=15.0,
        )
    )
    safety_issues: tuple[str, ...] = ()


_CATEGORY_BY_TOPIC: Mapping[str, str] = {
    "it": "IT/테크",
    "health": "건강/의학",
    "parenting": "육아",
}


_DEFAULT_SOURCES: tuple[CreatorSource, ...] = (
    CreatorSource("it", "threads", "choi.openai", "https://www.threads.net/@choi.openai", 95),
    CreatorSource("it", "youtube", "Matt Wolfe", "https://www.youtube.com/@mreflow", 90),
    CreatorSource("it", "youtube", "Two Minute Papers", "https://www.youtube.com/@TwoMinutePapers", 85),
    CreatorSource("it", "youtube", "MKBHD", "https://www.youtube.com/@mkbhd", 80),
    CreatorSource("health", "youtube", "Doctor Mike", "https://www.youtube.com/@DoctorMike", 85),
    CreatorSource("health", "youtube", "Nutrition Made Simple", "https://www.youtube.com/@NutritionMadeSimple", 90),
    CreatorSource("health", "web", "Nourished by Science", "https://nourishedbyscience.com/about/", 90),
    CreatorSource("parenting", "naver_blog", "mukttong2", "https://blog.naver.com/mukttong2", 95),
    CreatorSource("parenting", "web", "Good Inside", "https://www.goodinside.com/", 90),
    CreatorSource("parenting", "web", "Big Little Feelings", "https://biglittlefeelings.com/", 85),
    CreatorSource("parenting", "web", "ParentData", "https://parentdata.org/about-us/", 90),
)


_TEMPLATES: Mapping[str, tuple[CategoryPostTemplate, ...]] = {
    "it": (
        CategoryPostTemplate(
            "it_news_explain",
            "it",
            "IT 뉴스 해설형",
            "새 AI/기술 이슈를 쉽게 풀어 설명한다.",
            "무슨 일이 있었는지, 왜 중요한지, 내 일상/블로그/카페 운영에서 어떻게 봐야 하는지 순서로 작성하세요.",
            "핵심 변화 요약 카드",
            ("출시", "공개", "발표", "업데이트", "모델", "release", "launch", "announce", "update"),
        ),
        CategoryPostTemplate(
            "it_practical_apply",
            "it",
            "IT 실사용 적용형",
            "툴·앱·자동화를 실제 생활과 운영에 적용한다.",
            "문제 상황, 적용 방법, 실제 사용 순서, 주의할 점, 추천 독자 순서로 작성하세요.",
            "실행 워크플로 카드",
            ("사용법", "활용", "자동화", "워크플로", "앱", "툴", "how to", "workflow", "setup"),
        ),
        CategoryPostTemplate(
            "it_compare_decide",
            "it",
            "IT 비교 선택형",
            "여러 도구·기능을 비교해 선택 기준을 제시한다.",
            "비교 대상, 핵심 차이, 비용/난이도/활용도, 추천 대상, 선택 기준 순서로 작성하세요.",
            "비교표 카드",
            ("비교", "대안", "추천", "vs", "versus", "alternatives", "best"),
        ),
    ),
    "health": (
        CategoryPostTemplate(
            "health_evidence_note",
            "health",
            "건강 근거 정리형",
            "건강 주장이나 연구를 근거 수준 중심으로 정리한다.",
            "주장, 근거 수준, 일상 적용 가능성, 주의할 사람, 전문가 상담이 필요한 경우 순서로 작성하세요.",
            "근거 수준 표",
            ("연구", "논문", "근거", "가이드라인", "study", "research", "evidence"),
        ),
        CategoryPostTemplate(
            "health_daily_habit",
            "health",
            "건강 생활 적용형",
            "수면·식단·운동 같은 습관을 일상에 적용한다.",
            "오늘 겪는 문제, 작은 실천, 1주일 적용법, 실패하기 쉬운 지점, 안전한 마무리 순서로 작성하세요.",
            "생활 체크리스트 카드",
            ("수면", "식단", "운동", "스트레스", "습관", "루틴", "routine", "habit"),
        ),
        CategoryPostTemplate(
            "health_myth_check",
            "health",
            "건강 오해 점검형",
            "유행 건강 정보와 과장 표현을 조심스럽게 바로잡는다.",
            "유행 주장, 왜 혹하기 쉬운지, 확인된 근거, 과장된 부분, 안전한 대안 순서로 작성하세요.",
            "오해/사실 구분 카드",
            ("완치", "기적", "디톡스", "독소", "무조건", "치료", "cure", "detox", "miracle"),
        ),
    ),
    "parenting": (
        CategoryPostTemplate(
            "parenting_empathy_story",
            "parenting",
            "육아 상황 공감형",
            "부모가 겪는 실제 상황에서 시작해 해결 흐름으로 전개한다.",
            "상황 공감, 부모 마음 정리, 아이 관점, 오늘 해볼 대응, 다음에 확인할 신호 순서로 작성하세요.",
            "상황별 대응 카드",
            ("거부", "울음", "잠", "떼", "불안", "등원", "감정", "tantrum"),
        ),
        CategoryPostTemplate(
            "parenting_home_apply",
            "parenting",
            "육아 집 적용형",
            "전문가 콘텐츠나 가족 경험을 우리 집 상황에 맞게 적용한다.",
            "외부 자료의 핵심, 우리 집 상황, 바로 해본 적용법, 잘 맞는 경우/안 맞는 경우 순서로 작성하세요.",
            "우리 집 적용 워크플로 카드",
            ("적용", "놀이", "대화", "훈육", "부모", "전문가", "home", "apply"),
        ),
        CategoryPostTemplate(
            "parenting_checklist",
            "parenting",
            "육아 체크리스트형",
            "발달·식단·놀이·준비물을 실행표로 정리한다.",
            "상황 정의, 체크리스트, 루틴표, 흔한 실수, 연령별 조정 포인트 순서로 작성하세요.",
            "체크리스트/루틴표 카드",
            ("체크리스트", "준비물", "식단", "발달", "루틴", "개월", "schedule", "checklist"),
        ),
    ),
}


_RISK_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "health": (
        "완치",
        "치료된다",
        "수익",
        "기적",
        "무조건 낫",
        "약 없이",
        "detox",
        "miracle",
        "cure",
    ),
    "parenting": (
        "무조건 훈육",
        "때려",
        "방치",
    ),
}


def default_creator_sources(topic_mode: str = "") -> tuple[CreatorSource, ...]:
    """기본 watchlist를 반환한다."""

    topic = _normalize_topic(topic_mode)
    if not topic:
        return _DEFAULT_SOURCES
    return tuple(source for source in _DEFAULT_SOURCES if source.topic_mode == topic)


def get_templates_for_topic(topic_mode: str) -> tuple[CategoryPostTemplate, ...]:
    """토픽의 3종 글 양식을 반환한다."""

    return _TEMPLATES.get(_normalize_topic(topic_mode), ())


class CategoryOpportunityEngine:
    """API 없이 시작하는 카테고리 확장 글감 엔진."""

    def __init__(
        self,
        *,
        sources: Sequence[CreatorSource] | None = None,
        source_items: Sequence[SourceItem] | None = None,
    ) -> None:
        self.sources = tuple(sources or _DEFAULT_SOURCES)
        self.source_items = tuple(source_items or ())

    def build_brief(
        self,
        *,
        topic_mode: str,
        template_mode: str = "auto",
        recent_template_ids: Sequence[str] | None = None,
    ) -> CategoryContentBrief:
        """토픽별 글감 요약을 만든다."""

        topic = _normalize_topic(topic_mode) or "it"
        sources = tuple(source for source in self.sources if source.topic_mode == topic)
        items = tuple(item for item in self.source_items if _normalize_topic(item.topic_mode) == topic)
        if not sources:
            sources = default_creator_sources(topic)
        if not items:
            items = self._fallback_items(topic, sources)

        template = self.select_template(
            topic_mode=topic,
            source_items=items,
            template_mode=template_mode,
            recent_template_ids=recent_template_ids or (),
        )
        title = self._build_title(topic, template, items)
        angle = self._build_angle(topic, template, items, sources)
        safety_issues = self._safety_issues(topic, items)
        score = self._score(topic, items, sources, safety_issues)
        seed_keywords = self._seed_keywords(topic, template, items, sources)
        public_tags = self._public_tags(topic, template, items)

        return CategoryContentBrief(
            topic_mode=topic,
            category=_CATEGORY_BY_TOPIC.get(topic, topic),
            title=title,
            angle=angle,
            template_id=template.template_id,
            seed_keywords=tuple(seed_keywords),
            sources=sources[:4],
            source_items=items[:4],
            tags=tuple(public_tags),
            score=score,
            safety_issues=tuple(safety_issues),
        )

    def select_template(
        self,
        *,
        topic_mode: str,
        source_items: Sequence[SourceItem],
        template_mode: str = "auto",
        recent_template_ids: Sequence[str] | None = None,
    ) -> CategoryPostTemplate:
        """글감 성격에 맞는 글 양식을 선택한다."""

        topic = _normalize_topic(topic_mode) or "it"
        templates = _TEMPLATES.get(topic, ())
        if not templates:
            raise ValueError(f"unsupported category template topic: {topic}")

        requested = str(template_mode or "auto").strip().lower()
        for template in templates:
            if requested == template.template_id:
                return template

        joined = _join_item_text(source_items)
        if topic == "health" and _has_any(joined, _TEMPLATES["health"][2].trigger_keywords):
            return _TEMPLATES["health"][2]
        if topic == "it" and _looks_like_comparison(joined):
            return _TEMPLATES["it"][2]
        if topic == "parenting" and _has_any(joined, _TEMPLATES["parenting"][2].trigger_keywords):
            return _TEMPLATES["parenting"][2]

        scored = []
        for template in templates:
            score = sum(1 for keyword in template.trigger_keywords if keyword.lower() in joined)
            scored.append((score, template))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if scored and scored[0][0] > 0:
            return scored[0][1]

        recent = [str(item) for item in (recent_template_ids or ()) if str(item).strip()]
        counts = {template.template_id: recent.count(template.template_id) for template in templates}
        return sorted(templates, key=lambda template: (counts.get(template.template_id, 0), template.template_id))[0]

    def _fallback_items(self, topic: str, sources: Sequence[CreatorSource]) -> tuple[SourceItem, ...]:
        names = ", ".join(source.channel_name for source in sources[:3]) or topic
        title = {
            "it": f"{names}에서 오늘 확인할 AI/테크 변화",
            "health": f"{names}에서 오늘 확인할 건강 습관 근거",
            "parenting": f"{names}에서 오늘 확인할 육아 적용 아이디어",
        }.get(topic, f"{names} 최신 글감 확인")
        keywords = {
            "it": ("AI", "생산성", "자동화"),
            "health": ("건강", "습관", "근거"),
            "parenting": ("육아", "부모", "체크리스트"),
        }.get(topic, (topic,))
        return (
            SourceItem(
                topic_mode=topic,
                title=title,
                source_name=sources[0].channel_name if sources else topic,
                platform=sources[0].platform if sources else "manual",
                url=sources[0].url if sources else "",
                summary="기본 watchlist의 최신 내용을 확인해 블로그형 해설로 재구성합니다.",
                keywords=keywords,
            ),
        )

    def _build_title(
        self,
        topic: str,
        template: CategoryPostTemplate,
        items: Sequence[SourceItem],
    ) -> str:
        primary = _clean_title(items[0].title if items else "")
        if topic == "it":
            if template.template_id == "it_compare_decide":
                return f"{primary} 비교 정리 - 나에게 맞는 선택 기준"
            if template.template_id == "it_practical_apply":
                return f"{primary} 실사용 적용법 - 바쁜 사람 기준으로 정리"
            return f"{primary} 해설 - 지금 봐야 할 변화와 활용 포인트"
        if topic == "health":
            if template.template_id == "health_myth_check":
                return f"{primary} 진짜일까? 근거와 주의점 정리"
            if template.template_id == "health_daily_habit":
                return f"{primary} 생활 적용법 - 무리 없이 바꾸는 습관"
            return f"{primary} 근거 정리 - 어디까지 믿어도 될까"
        if topic == "parenting":
            if template.template_id == "parenting_checklist":
                return f"{primary} 체크리스트 - 오늘 바로 확인할 기준"
            if template.template_id == "parenting_home_apply":
                return f"{primary} 우리 집 적용법 - 현실적으로 바꿔보기"
            return f"{primary} 상황 공감 노트 - 부모 마음부터 정리하기"
        return primary

    def _build_angle(
        self,
        topic: str,
        template: CategoryPostTemplate,
        items: Sequence[SourceItem],
        sources: Sequence[CreatorSource],
    ) -> str:
        source_names = ", ".join(source.channel_name for source in sources[:4])
        item_titles = " / ".join(_clean_title(item.title) for item in items[:3])
        base = item_titles or source_names
        return (
            f"{template.label}: {base}를 단순 요약하지 않고, "
            f"승환님 블로그 독자가 바로 판단하거나 적용할 수 있는 관점으로 재구성합니다."
        )

    def _score(
        self,
        topic: str,
        items: Sequence[SourceItem],
        sources: Sequence[CreatorSource],
        safety_issues: Sequence[str],
    ) -> CategoryOpportunityScore:
        freshness = 25.0 if items else 10.0
        practical_fit = 25.0
        source_reliability = min(30.0, sum(max(1, source.priority) for source in sources[:4]) / 12.0)
        risk_penalty = 20.0 if safety_issues else 0.0
        if topic == "health" and len(sources) >= 2:
            source_reliability = min(35.0, source_reliability + 5.0)
        total = max(0.0, min(100.0, freshness + practical_fit + source_reliability - risk_penalty))
        return CategoryOpportunityScore(
            total=total,
            freshness=freshness,
            practical_fit=practical_fit,
            source_reliability=source_reliability,
            risk_penalty=risk_penalty,
        )

    def _seed_keywords(
        self,
        topic: str,
        template: CategoryPostTemplate,
        items: Sequence[SourceItem],
        sources: Sequence[CreatorSource],
    ) -> list[str]:
        base = {
            "it": ["AI", "IT", "생산성"],
            "health": ["건강", "습관", "근거"],
            "parenting": ["육아", "부모", "체크리스트"],
        }.get(topic, [topic])
        values: list[str] = [*base, template.label]
        for item in items[:3]:
            values.extend(item.keywords)
            values.append(item.source_name)
        for source in sources[:2]:
            values.append(source.channel_name)
        return _unique(values)[:5]

    def _public_tags(
        self,
        topic: str,
        template: CategoryPostTemplate,
        items: Sequence[SourceItem],
    ) -> list[str]:
        base = {
            "it": ["AI", "IT트렌드", "생산성", "자동화"],
            "health": ["건강정보", "생활습관", "건강공부", "근거기반"],
            "parenting": ["육아", "육아정보", "아빠육아", "육아체크리스트"],
        }.get(topic, [topic])
        values = [*base, template.label.replace(" ", "")]
        for item in items[:2]:
            values.extend(item.keywords[:2])
        return _unique(values)[:8]

    def _safety_issues(self, topic: str, items: Sequence[SourceItem]) -> list[str]:
        text = _join_item_text(items)
        issues = []
        for pattern in _RISK_PATTERNS.get(topic, ()):
            if pattern.lower() in text:
                issues.append(f"위험 표현 감지: {pattern}")
        if topic == "health" and len(items) <= 1:
            issues.append("건강 글은 단일 소스 의존이므로 승인 검토 필요")
        return issues


def _normalize_topic(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw == "economy":
        return "finance"
    return raw


def _join_item_text(items: Sequence[SourceItem]) -> str:
    return "\n".join(
        f"{item.title} {item.summary} {' '.join(item.keywords)}".lower()
        for item in items
    )


def _has_any(text: str, keywords: Iterable[str]) -> bool:
    lowered = str(text or "").lower()
    return any(str(keyword).lower() in lowered for keyword in keywords)


def _looks_like_comparison(text: str) -> bool:
    lowered = str(text or "").lower()
    if _has_any(lowered, ("비교", "대안", "추천", " vs ", "versus", "alternatives", "best")):
        return True
    candidates = re.findall(r"\b(chatgpt|claude|gemini|perplexity|notion|figma|cursor|copilot)\b", lowered)
    return len(set(candidates)) >= 2


def _clean_title(value: str) -> str:
    title = re.sub(r"\s+", " ", str(value or "").strip())
    return title[:72] or "오늘 확인할 블로그 글감"


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
