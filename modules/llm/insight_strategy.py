"""승환님 블로그용 통찰 전략과 쉬운 학습 문체 평가."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Sequence


@dataclass(frozen=True)
class PhilosophyFrame:
    """글에 적용할 철학 프레임."""

    frame_id: str
    label: str
    core_sentence: str
    plain_sentence: str
    keywords: List[str]


@dataclass(frozen=True)
class BlogInsightStrategy:
    """글 생성 전에 고정할 관점 전략."""

    topic_mode: str
    primary_frame: PhilosophyFrame
    secondary_frame: PhilosophyFrame
    thesis: str
    audience: str
    tone_name: str = "함께 공부하는 쉬운 기록체"

    def to_dict(self) -> Dict[str, Any]:
        """스냅샷 저장용 dict로 변환한다."""
        return {
            "topic_mode": self.topic_mode,
            "primary_frame": asdict(self.primary_frame),
            "secondary_frame": asdict(self.secondary_frame),
            "thesis": self.thesis,
            "audience": self.audience,
            "tone_name": self.tone_name,
        }

    def to_prompt_block(self) -> str:
        """LLM 프롬프트에 주입할 전략 블록을 만든다."""
        return f"""
[승환님 블로그 고정 전략]
핵심 논지: {self.thesis}

선택 철학 프레임:
- 1순위: {self.primary_frame.label} — {self.primary_frame.plain_sentence}
- 2순위: {self.secondary_frame.label} — {self.secondary_frame.plain_sentence}

문체 기준:
- 고등학생도 이해할 수 있는 쉬운 한국어로 씁니다.
- 누군가를 가르치는 말투가 아니라, 작성자도 함께 공부해나가는 태도로 씁니다.
- "정답은 이것입니다"보다 "저는 이 지점을 같이 확인해보려 합니다"에 가깝게 씁니다.
- 어려운 경제·기술 용어는 처음 나올 때 바로 쉬운 말로 풀어 씁니다.
- 한 문장에는 핵심을 하나만 담고, 문단은 2~4문장 중심으로 짧게 유지합니다.
- 시장/투자 글은 단정적 추천을 피하고, 조건과 한계를 함께 말합니다.
- 철학은 명언처럼 붙이지 말고, 데이터나 경험을 설명한 뒤 후반부에서 자연스럽게 연결합니다.

금지:
- 권위적으로 가르치는 문장
- 성공팔이, 과장 광고, 클릭베이트
- "반드시", "무조건", "이것만 보면 됩니다" 식의 단정
- 혼돈, 질서, 존엄, 문명 같은 추상어를 설명 없이 남발하는 문장

마무리 방식:
- 단순 요약이 아니라 "오늘 함께 확인할 공부 질문" 2~3개를 남깁니다.
""".strip()


@dataclass(frozen=True)
class InsightQualityResult:
    """통찰 품질 평가 결과."""

    overall_score: int
    perspective_score: int
    judgment_criteria_score: int
    learning_tone_score: int
    plain_language_score: int
    real_life_constraint_score: int
    philosophical_naturalness_score: int
    actionability_score: int
    needs_rewrite: bool
    issues: List[str]
    strengths: List[str]
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        """스냅샷 저장용 dict로 변환한다."""
        return asdict(self)


PHILOSOPHY_FRAMES: Dict[str, PhilosophyFrame] = {
    "structure_over_will": PhilosophyFrame(
        frame_id="structure_over_will",
        label="의지보다 구조",
        core_sentence="인간은 의지로 오래 버티지 못하므로 삶은 구조로 설계되어야 한다.",
        plain_sentence="열심히 하겠다는 마음보다, 지친 날에도 덜 무너지게 만드는 구조가 중요합니다.",
        keywords=["의지", "구조", "루틴", "시스템", "무너지"],
    ),
    "order_after_chaos": PhilosophyFrame(
        frame_id="order_after_chaos",
        label="흔들릴 때 돌아갈 기준",
        core_sentence="혼돈을 없애는 것이 아니라, 혼돈 뒤에 돌아갈 질서를 만드는 것이 중요하다.",
        plain_sentence="시장은 흔들리지만, 내 기준까지 같이 흔들릴 필요는 없습니다.",
        keywords=["시장", "흔들", "기준", "질서", "혼돈"],
    ),
    "body_reality": PhilosophyFrame(
        frame_id="body_reality",
        label="몸의 현실",
        core_sentence="몸을 잃은 전략은 오래가지 못한다.",
        plain_sentence="몸과 체력이 버텨주지 않으면 좋은 계획도 오래 이어지기 어렵습니다.",
        keywords=["몸", "체력", "수면", "피로", "지속"],
    ),
    "money_as_options": PhilosophyFrame(
        frame_id="money_as_options",
        label="돈은 선택권",
        core_sentence="돈은 과시가 아니라 삶의 선택지를 회복하는 도구다.",
        plain_sentence="돈은 더 뽐내기 위한 것이 아니라, 원하지 않는 선택만 하지 않게 해주는 힘입니다.",
        keywords=["돈", "선택권", "현금흐름", "리스크", "비중"],
    ),
    "automation_attention": PhilosophyFrame(
        frame_id="automation_attention",
        label="자동화는 주의력 회복",
        core_sentence="자동화는 게으름이 아니라 반복 노동에서 인간의 주의력을 회복하는 기술이다.",
        plain_sentence="자동화는 대충 하려는 기술이 아니라, 중요한 판단에 힘을 남기기 위한 방법입니다.",
        keywords=["자동화", "AI", "반복", "주의력", "판단"],
    ),
    "criteria_over_information": PhilosophyFrame(
        frame_id="criteria_over_information",
        label="정보보다 기준",
        core_sentence="좋은 글은 정보를 더 많이 주는 글이 아니라 판단 기준을 선명하게 만드는 글이다.",
        plain_sentence="정보는 많지만, 결국 필요한 것은 무엇을 기준으로 볼지 정하는 일입니다.",
        keywords=["정보", "기준", "판단", "확인", "관점"],
    ),
    "self_correction": PhilosophyFrame(
        frame_id="self_correction",
        label="자기수정",
        core_sentence="삶은 정답 찾기가 아니라 자기수정의 반복이다.",
        plain_sentence="처음부터 맞히기보다, 틀렸을 때 빨리 알아차리고 고치는 구조가 더 중요합니다.",
        keywords=["수정", "실험", "기록", "다시", "점검"],
    ),
}


TOPIC_FRAME_MAP: Dict[str, tuple[str, str]] = {
    "finance": ("criteria_over_information", "order_after_chaos"),
    "economy": ("criteria_over_information", "order_after_chaos"),
    "it": ("automation_attention", "criteria_over_information"),
    "cafe": ("structure_over_will", "body_reality"),
    "parenting": ("order_after_chaos", "self_correction"),
}


MARKET_KEYWORDS = ("국장", "미장", "증시", "시장", "브리핑", "나스닥", "코스피", "반도체", "금리", "환율")
AUTOMATION_KEYWORDS = ("자동화", "AI", "챗GPT", "블로그", "API", "코딩", "프로그램")
BODY_KEYWORDS = ("다이어트", "수영", "체중", "수면", "건강", "운동")
MONEY_KEYWORDS = ("투자", "재테크", "돈", "현금흐름", "비중", "포트폴리오")


def _normalize_text(items: Sequence[str]) -> str:
    """검색용 텍스트를 하나로 합친다."""
    return " ".join(str(item or "").strip() for item in items if str(item or "").strip()).lower()


def select_philosophy_frames(
    *,
    title: str,
    keywords: Sequence[str],
    topic_mode: str,
) -> tuple[PhilosophyFrame, PhilosophyFrame]:
    """주제와 키워드에 맞는 철학 프레임 2개를 선택한다."""
    normalized_topic = str(topic_mode or "").strip().lower()
    haystack = _normalize_text([title, *keywords])

    if any(word.lower() in haystack for word in MARKET_KEYWORDS):
        frame_ids = ("criteria_over_information", "order_after_chaos")
    elif any(word.lower() in haystack for word in AUTOMATION_KEYWORDS):
        frame_ids = ("automation_attention", "criteria_over_information")
    elif any(word.lower() in haystack for word in BODY_KEYWORDS):
        frame_ids = ("body_reality", "structure_over_will")
    elif any(word.lower() in haystack for word in MONEY_KEYWORDS):
        frame_ids = ("money_as_options", "self_correction")
    else:
        frame_ids = TOPIC_FRAME_MAP.get(normalized_topic, ("structure_over_will", "self_correction"))

    primary = PHILOSOPHY_FRAMES[frame_ids[0]]
    secondary = PHILOSOPHY_FRAMES[frame_ids[1]]
    return primary, secondary


def generate_insight_thesis(
    *,
    title: str,
    keywords: Sequence[str],
    topic_mode: str,
    primary_frame: PhilosophyFrame,
) -> str:
    """글 전체를 관통하는 쉬운 핵심 논지 1문장을 만든다."""
    haystack = _normalize_text([title, *keywords])
    normalized_topic = str(topic_mode or "").strip().lower()

    if any(word.lower() in haystack for word in MARKET_KEYWORDS):
        return "오늘의 목표는 시장 방향을 맞히는 것이 아니라, 내가 흔들릴 지점을 미리 줄이는 기준을 찾는 것입니다."
    if any(word.lower() in haystack for word in AUTOMATION_KEYWORDS) or normalized_topic == "it":
        return "자동화의 핵심은 일을 대충 넘기는 것이 아니라, 반복 작업을 줄여 중요한 판단에 힘을 남기는 것입니다."
    if normalized_topic == "parenting":
        return "육아는 아이를 완벽히 통제하는 일이 아니라, 흔들린 하루 뒤에도 다시 돌아갈 생활 기준을 만드는 일입니다."
    if any(word.lower() in haystack for word in BODY_KEYWORDS):
        return "건강 관리는 강한 의지를 증명하는 일이 아니라, 지친 날에도 이어질 수 있는 환경을 만드는 일입니다."
    if any(word.lower() in haystack for word in MONEY_KEYWORDS) or normalized_topic == "finance":
        return "돈과 투자는 확신을 자랑하는 일이 아니라, 내 삶의 선택지를 지키는 기준을 세우는 일입니다."
    return primary_frame.plain_sentence


def build_insight_strategy(
    *,
    title: str,
    keywords: Sequence[str],
    topic_mode: str,
) -> BlogInsightStrategy:
    """글 생성용 통찰 전략을 구성한다."""
    primary, secondary = select_philosophy_frames(
        title=title,
        keywords=keywords,
        topic_mode=topic_mode,
    )
    thesis = generate_insight_thesis(
        title=title,
        keywords=keywords,
        topic_mode=topic_mode,
        primary_frame=primary,
    )
    return BlogInsightStrategy(
        topic_mode=str(topic_mode or "").strip().lower() or "cafe",
        primary_frame=primary,
        secondary_frame=secondary,
        thesis=thesis,
        audience="투자를 공부하기 시작한 초심자와 자기 개발에 관심 있는 독자",
    )


class InsightQualityEvaluator:
    """쉬운 학습 문체와 통찰 품질을 휴리스틱으로 평가한다."""

    ABSTRACT_TERMS = ("혼돈", "질서", "존엄", "문명", "거시", "책임", "순환")
    LEARNING_PHRASES = ("저도", "같이", "함께", "배워", "공부", "확인해", "생각해", "느낍니다", "보려")
    AUTHORITARIAN_PHRASES = ("반드시", "무조건", "해야 합니다", "명심해야", "정답은", "이것만", "따라야")
    JUDGMENT_WORDS = ("기준", "확인", "점검", "질문", "리스크", "비중", "줄여", "조심", "가능성")
    REALITY_WORDS = ("시간", "돈", "체력", "몸", "피로", "자영업", "카페", "가족", "초심자", "생활")
    TECHNICAL_TERMS = ("DXY", "VIX", "ETF", "선물", "외국인 수급", "금리", "환율", "프리마켓", "변동성")
    EXPLANATION_CUES = ("쉽게 말하면", "이 말은", "뜻은", "라고 보면", "풀어보면", "즉", "쉽게 보면")

    def evaluate(
        self,
        *,
        content: str,
        title: str,
        keywords: Sequence[str],
        topic_mode: str,
        strategy: BlogInsightStrategy,
    ) -> InsightQualityResult:
        """최종 글의 통찰 품질을 평가한다."""
        del title, keywords, topic_mode
        text = str(content or "").strip()
        sentences = self._split_sentences(text)
        paragraph_count = len([p for p in re.split(r"\n\s*\n", text) if p.strip()])

        perspective_score = self._score_perspective(text, strategy)
        judgment_score = self._score_keyword_presence(text, self.JUDGMENT_WORDS, base=62, step=7)
        learning_score = self._score_learning_tone(text)
        plain_score = self._score_plain_language(text, sentences)
        reality_score = self._score_keyword_presence(text, self.REALITY_WORDS, base=58, step=6)
        philosophy_score = self._score_philosophical_naturalness(text, strategy)
        action_score = self._score_actionability(text, paragraph_count)

        scores = [
            perspective_score,
            judgment_score,
            learning_score,
            plain_score,
            reality_score,
            philosophy_score,
            action_score,
        ]
        overall = int(round(sum(scores) / len(scores)))
        issues: List[str] = []
        strengths: List[str] = []

        if plain_score < 80:
            issues.append("고등학생도 바로 이해하기에는 문장이나 용어가 다소 어렵습니다.")
        else:
            strengths.append("쉬운 언어 기준을 대체로 지켰습니다.")
        if learning_score < 80:
            issues.append("함께 공부하는 1인칭 태도가 약하거나 가르치는 말투가 섞였습니다.")
        else:
            strengths.append("함께 공부하는 목소리가 살아 있습니다.")
        if judgment_score < 78:
            issues.append("독자가 가져갈 판단 기준이 더 선명해야 합니다.")
        else:
            strengths.append("판단 기준과 점검 포인트가 드러납니다.")
        if philosophy_score < 78:
            issues.append("철학 문장이 추상적이거나 본문 맥락과 덜 자연스럽습니다.")
        else:
            strengths.append("철학 프레임이 생활 언어로 연결됩니다.")

        if not issues:
            issues.append("큰 결함은 없지만, 마지막 공부 질문을 더 구체화하면 좋습니다.")

        needs_rewrite = overall < 85 or plain_score < 70 or learning_score < 75
        summary = "통찰 품질 통과" if not needs_rewrite else "통찰 품질 보강 필요"
        return InsightQualityResult(
            overall_score=overall,
            perspective_score=perspective_score,
            judgment_criteria_score=judgment_score,
            learning_tone_score=learning_score,
            plain_language_score=plain_score,
            real_life_constraint_score=reality_score,
            philosophical_naturalness_score=philosophy_score,
            actionability_score=action_score,
            needs_rewrite=needs_rewrite,
            issues=issues,
            strengths=strengths,
            summary=summary,
        )

    def _split_sentences(self, text: str) -> List[str]:
        """문장을 대략적으로 분리한다."""
        prose_lines: List[str] = []
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                prose_lines.append("")
                continue
            if (
                stripped.startswith(("#", "|", "-", "*", ">", "```"))
                or bool(re.match(r"^\d+[\.)]\s+", stripped))
                or stripped.startswith("참고 자료:")
            ):
                continue
            prose_lines.append(stripped)

        normalized = re.sub(r"\s+", " ", "\n".join(prose_lines))
        normalized = re.sub(r"([.!?。])\s+", r"\1\n", normalized)
        normalized = normalized.replace("요. ", "요.\n").replace("다. ", "다.\n")
        return [s.strip() for s in normalized.splitlines() if s.strip()]

    def _score_perspective(self, text: str, strategy: BlogInsightStrategy) -> int:
        """핵심 관점 존재 여부를 평가한다."""
        score = 62
        for keyword in strategy.primary_frame.keywords + strategy.secondary_frame.keywords:
            if keyword and keyword in text:
                score += 4
        if "저는" in text or "제가" in text or "저도" in text:
            score += 8
        if "관점" in text or "기준" in text or "생각" in text:
            score += 8
        return self._clamp(score)

    def _score_keyword_presence(self, text: str, words: Sequence[str], *, base: int, step: int) -> int:
        """지정 단어군 출현을 점수화한다."""
        hits = sum(1 for word in words if word in text)
        return self._clamp(base + hits * step)

    def _score_learning_tone(self, text: str) -> int:
        """함께 공부하는 말투인지 평가한다."""
        score = 64 + sum(5 for phrase in self.LEARNING_PHRASES if phrase in text)
        score -= sum(8 for phrase in self.AUTHORITARIAN_PHRASES if phrase in text)
        if "?" in text:
            score += 6
        return self._clamp(score)

    def _score_plain_language(self, text: str, sentences: Sequence[str]) -> int:
        """쉬운 언어 기준을 평가한다."""
        if not text:
            return 0
        score = 88
        if sentences:
            avg_len = sum(len(sentence) for sentence in sentences) / len(sentences)
            long_count = sum(1 for sentence in sentences if len(sentence) >= 95)
            if avg_len > 80:
                score -= 14
            elif avg_len > 65:
                score -= 7
            score -= min(18, long_count * 4)

        unexplained_terms = 0
        for term in self.TECHNICAL_TERMS:
            if term not in text:
                continue
            term_explained = False
            for match in re.finditer(re.escape(term), text):
                term_index = match.start()
                window = text[max(0, term_index - 80): term_index + 140]
                if f"{term}(" in window or any(cue in window for cue in self.EXPLANATION_CUES):
                    term_explained = True
                    break
            if not term_explained:
                unexplained_terms += 1
        score -= min(20, unexplained_terms * 4)

        if any(cue in text for cue in self.EXPLANATION_CUES):
            score += 6
        return self._clamp(score)

    def _score_philosophical_naturalness(self, text: str, strategy: BlogInsightStrategy) -> int:
        """철학 프레임이 자연스러운지 평가한다."""
        score = 68
        if strategy.primary_frame.plain_sentence[:12] in text or strategy.primary_frame.keywords[0] in text:
            score += 8
        if "생활" in text or "현실" in text or "오늘" in text:
            score += 8
        abstract_count = sum(text.count(term) for term in self.ABSTRACT_TERMS)
        if abstract_count > 7:
            score -= 18
        elif abstract_count > 4:
            score -= 8
        if "명언" in text:
            score -= 10
        return self._clamp(score)

    def _score_actionability(self, text: str, paragraph_count: int) -> int:
        """읽고 난 뒤 행동/질문이 남는지 평가한다."""
        score = 60
        if "오늘" in text:
            score += 8
        if "질문" in text or "체크" in text or "점검" in text:
            score += 12
        if re.search(r"^\s*-\s+", text, flags=re.MULTILINE):
            score += 8
        if "?" in text:
            score += 7
        if paragraph_count >= 4:
            score += 5
        return self._clamp(score)

    @staticmethod
    def _clamp(value: int) -> int:
        """0~100 사이로 점수를 제한한다."""
        return max(0, min(100, int(value)))
