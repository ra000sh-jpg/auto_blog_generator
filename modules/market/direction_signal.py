"""여러 뉴스/검색 소스를 같은 기준으로 비교하는 방향성 신호 모델."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


MARKET_TERMS = (
    "ai",
    "반도체",
    "삼성",
    "하이닉스",
    "엔비디아",
    "전력",
    "데이터센터",
    "배터리",
    "수출",
    "소비",
    "금리",
    "환율",
    "증시",
    "코스피",
    "코스닥",
    "나스닥",
    "달러",
    "엔화",
    "비트코인",
    "유가",
    "중국",
    "일본",
    "중동",
)

OFFICIAL_SOURCE_TERMS = (
    "federal reserve",
    "bls",
    "bea",
    "census",
    "sec",
    "treasury",
    "ecos",
    "kosis",
    "opendart",
    "dart",
    "boj",
    "bank of japan",
    "china nbs",
    "national bureau of statistics",
)


@dataclass(frozen=True)
class DirectionSignal:
    """글 방향성 후보를 표현하는 공통 신호."""

    title: str
    source: str
    source_tier: str
    keywords: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    category: str = ""
    count: int | None = None
    trend_score: float = 0.0
    audience_score: float = 0.0
    authority_score: float = 0.0
    market_relevance: float = 0.0
    confidence: float = 0.0
    url: str = ""
    collected_at: str = ""
    summary: str = ""
    risk_flags: tuple[str, ...] = ()
    direction_score: float = 0.0
    score_reason: str = ""

    @property
    def issue_title(self) -> str:
        """기존 방향성 플래너와 호환되는 이슈명."""

        return self.title

    @property
    def news_count(self) -> int | None:
        """기존 방향성 플래너와 호환되는 기사 수."""

        return self.count

    @property
    def source_url(self) -> str:
        """기존 방향성 플래너와 호환되는 출처 URL."""

        return self.url or self.source

    def with_score(self, score: float, reason: str) -> "DirectionSignal":
        """점수와 이유를 채운 새 신호를 반환한다."""

        return DirectionSignal(
            title=self.title,
            source=self.source,
            source_tier=self.source_tier,
            keywords=self.keywords,
            entities=self.entities,
            category=self.category,
            count=self.count,
            trend_score=self.trend_score,
            audience_score=self.audience_score,
            authority_score=self.authority_score,
            market_relevance=self.market_relevance,
            confidence=self.confidence,
            url=self.url,
            collected_at=self.collected_at,
            summary=self.summary,
            risk_flags=self.risk_flags,
            direction_score=round(float(score), 4),
            score_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        payload: dict[str, Any] = {
            "title": self.title,
            "issue_title": self.title,
            "source": self.source,
            "source_tier": self.source_tier,
            "keywords": list(self.keywords),
            "entities": list(self.entities),
            "category": self.category,
            "trend_score": round(float(self.trend_score), 4),
            "audience_score": round(float(self.audience_score), 4),
            "authority_score": round(float(self.authority_score), 4),
            "market_relevance": round(float(self.market_relevance), 4),
            "confidence": round(float(self.confidence), 4),
            "source_url": self.url,
            "url": self.url,
            "collected_at": self.collected_at,
            "summary": self.summary,
            "risk_flags": list(self.risk_flags),
            "direction_score": round(float(self.direction_score), 4),
            "score_reason": self.score_reason,
        }
        if self.count is not None:
            payload["count"] = self.count
            payload["news_count"] = self.count
        return payload


@dataclass(frozen=True)
class DirectionSignalPlan:
    """여러 방향성 신호를 평가한 결과."""

    selected_signal: DirectionSignal
    ranked_signals: tuple[DirectionSignal, ...]
    score: float
    source_mix: tuple[str, ...]
    fallback_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "selected_signal": self.selected_signal.to_dict(),
            "ranked_signals": [signal.to_dict() for signal in self.ranked_signals],
            "score": round(float(self.score), 4),
            "source_mix": list(self.source_mix),
            "fallback_sources": list(self.fallback_sources),
        }


class DirectionSignalAggregator:
    """방향성 신호를 점수화하고 대표 후보를 고른다."""

    def aggregate(
        self,
        signals: Sequence[DirectionSignal],
        *,
        confirmed_metrics: Sequence[Any] = (),
        seed_keywords: Sequence[str] = (),
        scope: str = "",
    ) -> DirectionSignalPlan | None:
        """신호 목록에서 글 방향성으로 가장 적합한 후보를 선택한다."""

        deduped = _dedupe_signals(signals)
        scored = [
            self.score_signal(
                signal,
                confirmed_metrics=confirmed_metrics,
                seed_keywords=seed_keywords,
                scope=scope,
            )
            for signal in deduped
            if _clean_text(signal.title)
        ]
        if not scored:
            return None
        scored.sort(key=lambda item: item.direction_score, reverse=True)
        source_mix = tuple(dict.fromkeys(signal.source for signal in scored if signal.source))[:6]
        fallback_sources = tuple(
            dict.fromkeys(signal.source for signal in scored[1:] if signal.source and signal.source != scored[0].source)
        )[:5]
        return DirectionSignalPlan(
            selected_signal=scored[0],
            ranked_signals=tuple(scored[:8]),
            score=scored[0].direction_score,
            source_mix=source_mix,
            fallback_sources=fallback_sources,
        )

    def score_signal(
        self,
        signal: DirectionSignal,
        *,
        confirmed_metrics: Sequence[Any] = (),
        seed_keywords: Sequence[str] = (),
        scope: str = "",
    ) -> DirectionSignal:
        """단일 신호의 방향성 점수를 계산한다."""

        tier_defaults = _tier_defaults(signal.source_tier, signal.source)
        trend = _bounded(signal.trend_score or _count_to_score(signal.count) or tier_defaults["trend"])
        audience = _bounded(signal.audience_score or tier_defaults["audience"])
        authority = _bounded(signal.authority_score or tier_defaults["authority"])
        market = _bounded(
            signal.market_relevance
            or _market_relevance_score(signal, confirmed_metrics=confirmed_metrics, scope=scope)
        )
        blog_fit = _bounded(_blog_fit_score(signal))
        user_fit = _bounded(_seed_keyword_score(signal, seed_keywords))
        confidence = _bounded(signal.confidence or tier_defaults["confidence"])

        score = (
            trend * 25.0
            + audience * 20.0
            + market * 20.0
            + authority * 15.0
            + blog_fit * 10.0
            + user_fit * 10.0
        )
        score *= 0.78 + confidence * 0.22
        if signal.risk_flags:
            score -= min(len(signal.risk_flags) * 5.0, 15.0)
        reason = (
            f"의제 {trend:.2f}, 관심 {audience:.2f}, 시장연결 {market:.2f}, "
            f"공식성 {authority:.2f}, 블로그적합 {blog_fit:.2f}, 관심키워드 {user_fit:.2f}"
        )
        return signal.with_score(max(0.0, min(100.0, score)), reason)


def signals_from_bigkinds_issues(issues: Sequence[Any]) -> list[DirectionSignal]:
    """빅카인즈 이슈를 공통 방향성 신호로 변환한다."""

    signals: list[DirectionSignal] = []
    for issue in issues:
        title = _value(issue, "issue_title") or _value(issue, "title")
        title = _clean_text(title)
        if not title:
            continue
        count = _int_value(_value(issue, "news_count") or _value(issue, "count"))
        confidence = _float_value(_value(issue, "confidence"), default=0.72)
        keywords = _sequence_value(issue, "keywords") or tuple(_extract_keywords(title))
        source = _value(issue, "source") or "BigKinds public"
        source_tier = _value(issue, "source_tier") or "agenda"
        signals.append(
            DirectionSignal(
                title=title,
                source=source,
                source_tier=source_tier,
                keywords=keywords,
                category=_value(issue, "category"),
                count=count,
                trend_score=_count_to_score(count),
                authority_score=0.72,
                market_relevance=0.0,
                confidence=confidence,
                url=_value(issue, "source_url") or _value(issue, "url"),
                collected_at=_value(issue, "collected_at"),
            )
        )
    return signals


def signals_from_market_news_items(news_items: Sequence[Any]) -> list[DirectionSignal]:
    """시장 스냅샷의 RSS/GDELT/공식 뉴스 항목을 방향성 신호로 변환한다."""

    signals: list[DirectionSignal] = []
    for item in news_items:
        title = _clean_text(_value(item, "title"))
        if not title:
            continue
        source = _clean_text(_value(item, "source")) or "Market news"
        tier = _infer_source_tier(source)
        published_at = _value(item, "published_at")
        if hasattr(published_at, "isoformat"):
            published_at = published_at.isoformat()
        signals.append(
            DirectionSignal(
                title=title,
                source=source,
                source_tier=tier,
                keywords=tuple(_extract_keywords(" ".join([title, _value(item, "summary")]))),
                category="뉴스/공시",
                count=1,
                trend_score=0.42 if tier != "official" else 0.52,
                audience_score=0.35 if tier != "global_news" else 0.45,
                authority_score=_authority_default(tier, source),
                confidence=0.68 if tier != "global_news" else 0.62,
                url=_value(item, "url"),
                collected_at=_clean_text(str(published_at or "")),
                summary=_clean_text(_value(item, "summary"))[:240],
            )
        )
    return signals


def signals_from_naver_items(items: Sequence[Any]) -> list[DirectionSignal]:
    """네이버 뉴스/블로그 검색 결과를 방향성 신호로 변환한다."""

    signals: list[DirectionSignal] = []
    for item in items:
        title = _clean_text(_value(item, "title"))
        if not title:
            continue
        source = _clean_text(_value(item, "source")) or "Naver Search"
        is_blog = "blog" in source.lower()
        signals.append(
            DirectionSignal(
                title=title,
                source=source,
                source_tier="audience" if is_blog else "news_api",
                keywords=tuple(_extract_keywords(" ".join([title, _value(item, "description"), _value(item, "content")]))),
                category="네이버 블로그" if is_blog else "네이버 뉴스",
                count=1,
                trend_score=0.46,
                audience_score=0.78 if is_blog else 0.58,
                authority_score=0.42 if is_blog else 0.56,
                confidence=0.68,
                url=_value(item, "link") or _value(item, "url"),
                collected_at=_iso_now(),
                summary=_clean_text(_value(item, "description") or _value(item, "content"))[:240],
            )
        )
    return signals


def collect_naver_direction_signals(
    query: str,
    *,
    max_per_service: int = 3,
    collector: Any | None = None,
) -> list[DirectionSignal]:
    """네이버 검색 API가 설정된 경우 뉴스/블로그 방향성 신호를 수집한다."""

    normalized_query = _clean_text(query)
    if not normalized_query:
        return []
    if collector is None:
        try:
            from ..collectors.naver_search import NaverSearchCollector

            collector = NaverSearchCollector()
        except Exception:
            return []
    if not bool(getattr(collector, "enabled", False)):
        return []

    items: list[Any] = []
    for service in ("news", "blog"):
        try:
            items.extend(collector.search(normalized_query, service=service, display=max_per_service))
        except Exception:
            continue
    return signals_from_naver_items(items)


def direction_signal_to_issue_dict(signal: DirectionSignal) -> dict[str, Any]:
    """방향성 플래너 입력으로 쓰기 쉬운 issue dict를 만든다."""

    return signal.to_dict()


def _dedupe_signals(signals: Sequence[DirectionSignal]) -> list[DirectionSignal]:
    deduped: list[DirectionSignal] = []
    seen: set[str] = set()
    for signal in signals:
        key = re.sub(r"\W+", "", signal.title.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(signal)
    return deduped


def _tier_defaults(source_tier: str, source: str) -> dict[str, float]:
    tier = str(source_tier or "").strip().lower()
    if tier == "agenda":
        return {"trend": 0.72, "audience": 0.48, "authority": 0.72, "confidence": 0.72}
    if tier == "audience":
        return {"trend": 0.48, "audience": 0.80, "authority": 0.42, "confidence": 0.66}
    if tier == "news_api":
        return {"trend": 0.55, "audience": 0.58, "authority": 0.56, "confidence": 0.66}
    if tier == "official":
        return {"trend": 0.50, "audience": 0.32, "authority": 0.92, "confidence": 0.82}
    if tier == "global_news":
        return {"trend": 0.56, "audience": 0.44, "authority": 0.54, "confidence": 0.62}
    if "gdelt" in source.lower():
        return {"trend": 0.54, "audience": 0.42, "authority": 0.50, "confidence": 0.60}
    return {"trend": 0.44, "audience": 0.36, "authority": 0.50, "confidence": 0.58}


def _infer_source_tier(source: str) -> str:
    lowered = source.lower()
    if any(term in lowered for term in OFFICIAL_SOURCE_TERMS):
        return "official"
    if lowered.startswith("gdelt") or "google news" in lowered:
        return "global_news"
    return "rss"


def _authority_default(source_tier: str, source: str) -> float:
    return _tier_defaults(source_tier, source)["authority"]


def _market_relevance_score(
    signal: DirectionSignal,
    *,
    confirmed_metrics: Sequence[Any],
    scope: str,
) -> float:
    text = " ".join([signal.title, signal.summary, *signal.keywords]).lower()
    score = 0.0
    score += min(sum(1 for term in MARKET_TERMS if term.lower() in text) * 0.14, 0.70)
    metric_keys = [_clean_text(_value(metric, "key") or _value(metric, "symbol")).lower() for metric in confirmed_metrics]
    if any(key and key.replace("_", " ") in text for key in metric_keys):
        score += 0.18
    if str(scope).lower() in {"kr", "global"} and any(term in text for term in ("반도체", "ai", "수출", "중국", "일본")):
        score += 0.18
    if str(scope).lower() == "us" and any(term in text for term in ("fed", "nasdaq", "treasury", "ai", "semiconductor")):
        score += 0.18
    return max(0.20 if _clean_text(signal.title) else 0.0, min(score, 1.0))


def _blog_fit_score(signal: DirectionSignal) -> float:
    text = " ".join([signal.title, signal.summary, *signal.keywords])
    fit_terms = ("왜", "확인", "조건", "리스크", "투자", "시장", "전망", "대응", "흐름", "기준")
    score = 0.38 + min(sum(1 for term in fit_terms if term.lower() in text.lower()) * 0.08, 0.42)
    if len(signal.title) <= 55:
        score += 0.10
    return min(score, 1.0)


def _seed_keyword_score(signal: DirectionSignal, seed_keywords: Sequence[str]) -> float:
    normalized = [_clean_text(str(item)).lower() for item in seed_keywords if _clean_text(str(item))]
    if not normalized:
        return 0.45
    text = " ".join([signal.title, signal.summary, *signal.keywords]).lower()
    hits = sum(1 for keyword in normalized if keyword and (keyword in text or any(token in text for token in keyword.split())))
    return min(0.30 + hits * 0.22, 1.0)


def _count_to_score(count: int | None) -> float:
    if count is None or count <= 0:
        return 0.0
    return min(math.log(float(count) + 1.0, 80.0), 1.0)


def _extract_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", str(text or ""))
    blocked = {"오늘", "뉴스", "이슈", "관련", "기준", "브리핑", "시장", "속보", "영상", "사진"}
    return [token for token in dict.fromkeys(tokens) if token not in blocked][:8]


def _sequence_value(item: Any, key: str) -> tuple[str, ...]:
    raw = item.get(key, ()) if isinstance(item, Mapping) else getattr(item, key, ())
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(_clean_text(str(value)) for value in raw if _clean_text(str(value)))
    text = _clean_text(str(raw or ""))
    if not text:
        return ()
    return tuple(_clean_text(part) for part in re.split(r"[,;/|·ㆍ]+", text) if _clean_text(part))


def _value(item: Any, key: str) -> str:
    if isinstance(item, Mapping):
        value = item.get(key, "")
    else:
        value = getattr(item, key, "")
    return _clean_text(str(value or ""))


def _int_value(raw: Any) -> int | None:
    try:
        if raw is None or raw == "":
            return None
        return int(float(str(raw).replace(",", "").strip()))
    except Exception:
        return None


def _float_value(raw: Any, *, default: float = 0.0) -> float:
    try:
        if raw is None or raw == "":
            return default
        return float(str(raw).strip())
    except Exception:
        return default


def _bounded(value: float) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except Exception:
        return 0.0


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
