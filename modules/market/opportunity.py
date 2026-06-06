"""시장 글감 기회 점수화와 브리프 생성."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .slots import BlogSlot
from .sources import MarketScope

logger = logging.getLogger(__name__)


KR_PREOPEN_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "keyword": "반도체",
        "entities": ("삼성전자", "SK하이닉스", "한미반도체"),
        "themes": ("미국 증시", "AI 반도체", "수출"),
    },
    {
        "keyword": "전력설비",
        "entities": ("HD현대일렉트릭", "LS ELECTRIC", "효성중공업"),
        "themes": ("AI 데이터센터", "변압기", "전력망"),
    },
    {
        "keyword": "환율",
        "entities": ("USD/KRW", "외국인 수급"),
        "themes": ("달러", "금리", "국장 수급"),
    },
    {
        "keyword": "미국 금리",
        "entities": ("US10Y", "US2Y", "FOMC"),
        "themes": ("나스닥", "성장주", "외국인 수급"),
    },
    {
        "keyword": "AI 데이터센터",
        "entities": ("NVIDIA", "전력설비", "반도체"),
        "themes": ("전력 수요", "AI 투자", "한국 수출"),
    },
)


@dataclass(frozen=True)
class OpportunityScore:
    """글감 기회 점수 세부 항목."""

    search_momentum: float = 0.0
    news_velocity: float = 0.0
    blog_gap: float = 0.0
    market_signal: float = 0.0
    authority_signal: float = 0.0
    persona_fit: float = 0.0
    risk_penalty: float = 0.0
    final_score: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """직렬화 가능한 dict로 변환한다."""

        return asdict(self)


@dataclass(frozen=True)
class OpportunityCandidate:
    """블로그 글감 후보."""

    domain: str
    keyword: str
    entities: tuple[str, ...] = ()
    opportunity_score: float = 0.0
    trend_reason: str = ""
    blog_gap: str = ""
    recommended_article: str = ""
    content_type: str = "시장 해설형"
    risk_flags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    score_detail: OpportunityScore = field(default_factory=OpportunityScore)
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """직렬화 가능한 dict로 변환한다."""

        data = asdict(self)
        data["score_detail"] = self.score_detail.to_dict()
        return data


@dataclass(frozen=True)
class ContentBrief:
    """글 생성기로 넘길 간단한 콘텐츠 브리프."""

    title: str
    angle: str
    target_reader: str
    outline: tuple[str, ...]
    evidence: tuple[str, ...]
    risk_flags: tuple[str, ...]
    source_candidate: OpportunityCandidate

    def to_dict(self) -> dict[str, Any]:
        """직렬화 가능한 dict로 변환한다."""

        return {
            "title": self.title,
            "angle": self.angle,
            "target_reader": self.target_reader,
            "outline": list(self.outline),
            "evidence": list(self.evidence),
            "risk_flags": list(self.risk_flags),
            "source_candidate": self.source_candidate.to_dict(),
        }


@dataclass(frozen=True)
class KeywordSignal:
    """점수화에 필요한 키워드별 원천 신호."""

    keyword: str
    domain: str = "KR_STOCK"
    entities: tuple[str, ...] = ()
    search_momentum: float = 0.0
    news_count_24h: int = 0
    news_baseline_daily: float = 3.0
    recent_blog_count: int = 0
    blog_baseline: float = 10.0
    market_signal: float = 0.0
    authority_signal: float = 0.0
    persona_fit: float = 85.0
    evidence: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()


class OpportunityScorer:
    """시장 글감 후보를 0~100점으로 평가한다."""

    RISK_PATTERNS: tuple[tuple[str, str, float], ...] = (
        (r"급등주|상한가|작전주", "작전주/급등주성 키워드", 18.0),
        (r"무조건|수익\s*보장|목표가\s*확정", "투자 권유성 표현 위험", 22.0),
        (r"루머|찌라시|미확인", "출처 불확실 키워드", 16.0),
        (r"정치|논란|사건|사고", "시장 해설과 무관한 이슈성 키워드", 14.0),
    )

    def score(self, signal: KeywordSignal) -> OpportunityScore:
        """단일 키워드 신호를 점수화한다."""

        search_score = _clamp(float(signal.search_momentum) * 34.0, 0.0, 100.0)
        news_velocity = _ratio_score(signal.news_count_24h, signal.news_baseline_daily)
        blog_gap = self._blog_gap_score(
            recent_blog_count=signal.recent_blog_count,
            blog_baseline=signal.blog_baseline,
            search_score=search_score,
        )
        market_signal = _clamp(signal.market_signal, 0.0, 100.0)
        authority_signal = _clamp(signal.authority_signal, 0.0, 100.0)
        persona_fit = _clamp(signal.persona_fit, 0.0, 100.0)
        risk_penalty = self._risk_penalty(signal)

        raw_score = (
            search_score * 0.20
            + news_velocity * 0.20
            + blog_gap * 0.20
            + market_signal * 0.25
            + authority_signal * 0.10
            + persona_fit * 0.05
        )
        final_score = _clamp(raw_score - risk_penalty, 0.0, 100.0)
        return OpportunityScore(
            search_momentum=round(search_score, 2),
            news_velocity=round(news_velocity, 2),
            blog_gap=round(blog_gap, 2),
            market_signal=round(market_signal, 2),
            authority_signal=round(authority_signal, 2),
            persona_fit=round(persona_fit, 2),
            risk_penalty=round(risk_penalty, 2),
            final_score=round(final_score, 2),
        )

    def to_candidate(self, signal: KeywordSignal) -> OpportunityCandidate:
        """신호와 점수를 글감 후보 모델로 변환한다."""

        detail = self.score(signal)
        title = _recommended_title(signal.keyword, signal.entities)
        risk_flags = tuple(dict.fromkeys((*signal.risk_flags, *_risk_labels(signal.keyword))))
        trend_parts = []
        if detail.search_momentum >= 55:
            trend_parts.append("검색 관심도 상승")
        if detail.news_velocity >= 55:
            trend_parts.append("뉴스 발생량 증가")
        if detail.market_signal >= 55:
            trend_parts.append("시장 지표 변화")
        if not trend_parts:
            trend_parts.append("시장 브리핑 후보")

        blog_gap = (
            "검색/뉴스 신호 대비 최근 블로그 공급이 낮음"
            if detail.blog_gap >= 65
            else "블로그 경쟁도는 보통 수준"
        )
        return OpportunityCandidate(
            domain=signal.domain,
            keyword=signal.keyword,
            entities=signal.entities,
            opportunity_score=detail.final_score,
            trend_reason=", ".join(trend_parts),
            blog_gap=blog_gap,
            recommended_article=title,
            risk_flags=risk_flags,
            evidence=signal.evidence,
            score_detail=detail,
            collected_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _blog_gap_score(
        self,
        *,
        recent_blog_count: int,
        blog_baseline: float,
        search_score: float,
    ) -> float:
        supply_ratio = max(0.0, float(recent_blog_count)) / max(1.0, float(blog_baseline))
        scarcity = 100.0 - min(100.0, supply_ratio * 100.0)
        return _clamp((scarcity * 0.65) + (search_score * 0.35), 0.0, 100.0)

    def _risk_penalty(self, signal: KeywordSignal) -> float:
        text = " ".join([signal.keyword, *signal.entities, *signal.risk_flags])
        penalty = 0.0
        for pattern, _label, amount in self.RISK_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                penalty += amount
        return _clamp(penalty, 0.0, 45.0)


class MarketOpportunityEngine:
    """네이버 검색/블로그와 시장 스냅샷을 이용해 국장전 글감을 고른다."""

    def __init__(
        self,
        *,
        naver_search_collector: Any = None,
        market_data_collector: Any = None,
        scorer: OpportunityScorer | None = None,
    ) -> None:
        self.naver_search_collector = naver_search_collector
        self.market_data_collector = market_data_collector
        self.scorer = scorer or OpportunityScorer()

    def discover_kr_preopen(
        self,
        *,
        top_k: int = 5,
        now: datetime | None = None,
    ) -> list[OpportunityCandidate]:
        """국장전 글감 후보를 반환한다. 수집 불가 시 빈 목록으로 후퇴한다."""

        collector = self.naver_search_collector
        if collector is None or not bool(getattr(collector, "enabled", False)):
            return []

        snapshot = self._collect_market_snapshot(now=now)
        signals: list[KeywordSignal] = []
        for seed in KR_PREOPEN_SEEDS:
            keyword = str(seed.get("keyword", "")).strip()
            if not keyword:
                continue
            try:
                signals.append(self._build_signal(keyword=keyword, seed=seed, snapshot=snapshot))
            except Exception as exc:
                logger.debug("Opportunity signal skipped: %s", exc)

        candidates = [self.scorer.to_candidate(signal) for signal in signals]
        candidates = [item for item in candidates if item.opportunity_score > 0]
        candidates.sort(key=lambda item: item.opportunity_score, reverse=True)
        return candidates[: max(1, min(20, int(top_k or 5)))]

    def build_brief(self, candidate: OpportunityCandidate) -> ContentBrief:
        """선택 후보를 글 생성용 브리프로 변환한다."""

        return ContentBrief(
            title=candidate.recommended_article,
            angle=(
                f"{candidate.keyword}를 단순 이슈가 아니라 국장 전 확인할 조건과 "
                "리스크 기준으로 풀어낸다."
            ),
            target_reader="아침에 국장 흐름을 공부하는 투자 초심자",
            outline=(
                "오늘 이 키워드를 먼저 보는 이유",
                "밤사이 해외 지표와 연결되는 부분",
                "국장에서 확인할 섹터와 수급 조건",
                "초심자가 피해야 할 단정",
                "오늘의 체크 질문",
            ),
            evidence=candidate.evidence,
            risk_flags=candidate.risk_flags,
            source_candidate=candidate,
        )

    def _build_signal(
        self,
        *,
        keyword: str,
        seed: Mapping[str, Any],
        snapshot: Any,
    ) -> KeywordSignal:
        news_items = self._search(keyword, service="news", display=10, sort="date")
        blog_items = self._search(keyword, service="blog", display=10, sort="date")
        news_count = len(news_items)
        blog_count = len(blog_items)
        evidence = ["naver_news_recent", "naver_blog_recent"]

        if snapshot is not None:
            evidence.append("market_snapshot")

        search_momentum = 1.0 + min(1.6, news_count / 10.0)
        market_signal = self._market_signal(keyword=keyword, seed=seed, snapshot=snapshot)
        authority_signal = 45.0 + min(45.0, news_count * 6.0)
        if _has_official_like_source(news_items):
            authority_signal += 10.0

        return KeywordSignal(
            keyword=keyword,
            entities=tuple(str(item) for item in seed.get("entities", ()) if str(item).strip()),
            search_momentum=search_momentum,
            news_count_24h=news_count,
            recent_blog_count=blog_count,
            market_signal=market_signal,
            authority_signal=authority_signal,
            persona_fit=_persona_fit(keyword, seed.get("themes", ())),
            evidence=tuple(evidence),
            risk_flags=tuple(_risk_labels(keyword)),
        )

    def _search(self, keyword: str, *, service: str, display: int, sort: str) -> list[Any]:
        try:
            return list(
                self.naver_search_collector.search(
                    keyword,
                    service=service,
                    display=display,
                    sort=sort,
                )
            )
        except Exception as exc:
            logger.debug("Naver opportunity search failed: %s", exc)
            return []

    def _collect_market_snapshot(self, *, now: datetime | None) -> Any:
        collector = self.market_data_collector
        if collector is None:
            return None
        try:
            return collector.collect(MarketScope.KR, slot=BlogSlot.KR_PREOPEN, now=now, max_news_items=4)
        except Exception as exc:
            logger.debug("Market opportunity snapshot skipped: %s", exc)
            return None

    def _market_signal(self, *, keyword: str, seed: Mapping[str, Any], snapshot: Any) -> float:
        if snapshot is None:
            return 45.0
        text_parts: list[str] = [keyword]
        text_parts.extend(str(item) for item in seed.get("themes", ()) if str(item).strip())
        for point in list(getattr(snapshot, "data_points", ()) or ()):
            text_parts.append(str(getattr(point, "symbol", "") or ""))
            text_parts.append(str(getattr(point, "label", "") or ""))
        for item in list(getattr(snapshot, "news_items", ()) or ()):
            text_parts.append(str(getattr(item, "title", "") or ""))
        haystack = " ".join(text_parts).lower()
        score = 48.0
        for token in _keyword_tokens(keyword, seed.get("themes", ())):
            if token and token.lower() in haystack:
                score += 10.0
        data_count = len(getattr(snapshot, "data_points", ()) or ())
        news_count = len(getattr(snapshot, "news_items", ()) or ())
        score += min(20.0, data_count * 3.0 + news_count * 2.0)
        return _clamp(score, 0.0, 100.0)


def select_best_kr_preopen_opportunity(
    *,
    naver_search_collector: Any = None,
    market_data_collector: Any = None,
    now: datetime | None = None,
) -> tuple[OpportunityCandidate, ContentBrief] | None:
    """국장전 최상위 글감과 브리프를 반환한다."""

    engine = MarketOpportunityEngine(
        naver_search_collector=naver_search_collector,
        market_data_collector=market_data_collector,
    )
    candidates = engine.discover_kr_preopen(top_k=1, now=now)
    if not candidates:
        return None
    candidate = candidates[0]
    return candidate, engine.build_brief(candidate)


def _ratio_score(current: float, baseline: float) -> float:
    ratio = max(0.0, float(current)) / max(1.0, float(baseline))
    return _clamp(ratio * 55.0, 0.0, 100.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _risk_labels(keyword: str) -> tuple[str, ...]:
    labels = []
    for pattern, label, _amount in OpportunityScorer.RISK_PATTERNS:
        if re.search(pattern, keyword, flags=re.IGNORECASE):
            labels.append(label)
    return tuple(labels)


def _recommended_title(keyword: str, entities: Sequence[str]) -> str:
    if keyword == "전력설비":
        return "전력설비주가 다시 주목받는 이유: AI 데이터센터가 만든 전력 병목"
    if keyword == "반도체":
        return "국장 전 반도체를 볼 때 주가보다 먼저 확인할 3가지"
    if keyword == "환율":
        return "환율이 흔들릴 때 국장 수급을 읽는 초심자 기준"
    if keyword == "미국 금리":
        return "미국 금리가 국장 분위기에 남기는 신호와 확인 기준"
    if keyword == "AI 데이터센터":
        return "AI 데이터센터 이슈가 한국 증시에 번지는 경로"
    entity_hint = entities[0] if entities else keyword
    return f"{entity_hint} 이슈를 국장 전 체크리스트로 읽는 법"


def _persona_fit(keyword: str, themes: Iterable[Any]) -> float:
    text = " ".join([keyword, *(str(item) for item in themes)])
    score = 72.0
    for token in ("AI", "반도체", "수출", "환율", "금리", "전력", "초심자", "데이터센터"):
        if token.lower() in text.lower():
            score += 5.0
    return _clamp(score, 0.0, 100.0)


def _keyword_tokens(keyword: str, themes: Iterable[Any]) -> tuple[str, ...]:
    raw = [keyword, *(str(item) for item in themes)]
    tokens: list[str] = []
    for value in raw:
        tokens.extend(re.findall(r"[가-힣A-Za-z0-9/]{2,}", str(value)))
    return tuple(dict.fromkeys(tokens))


def _has_official_like_source(items: Sequence[Any]) -> bool:
    trusted = ("거래소", "한국은행", "연준", "Fed", "금융위", "공시", "IR", "실적")
    for item in items:
        text = f"{getattr(item, 'title', '')} {getattr(item, 'description', '')}"
        if any(token.lower() in text.lower() for token in trusted):
            return True
    return False
