"""무료 시장 데이터 소스 계층과 데이터 신뢰도 계산."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Sequence


class MarketScope(str, Enum):
    """시장 브리핑 범위."""

    KR = "kr"
    US = "us"
    GLOBAL = "global"
    EVERGREEN = "evergreen"


class DataMode(str, Enum):
    """데이터 확보 수준에 따른 글 작성 모드."""

    NUMERIC_BRIEFING = "numeric_briefing"
    CONDITIONAL_BRIEFING = "conditional_briefing"
    INSIGHT_FALLBACK = "insight_fallback"


@dataclass(frozen=True)
class DataSourceTier:
    """무료 데이터 소스 계층."""

    tier: int
    purpose: str
    sources: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class SourceConfidence:
    """수집 데이터 신뢰도 결과."""

    score: float
    mode: DataMode
    allow_numeric_claims: bool
    reason: str


FREE_DATA_SOURCE_TIERS: tuple[DataSourceTier, ...] = (
    DataSourceTier(
        tier=1,
        purpose="가격/지수/섹터 방향",
        sources=("yfinance", "Stooq", "exchange_public_cache"),
        notes="동일 지표가 2개 소스에서 크게 다르면 방향성만 사용한다.",
    ),
    DataSourceTier(
        tier=2,
        purpose="금리/달러/매크로",
        sources=(
            "FRED public CSV",
            "BLS Public Data API",
            "BEA NIPA API",
            "U.S. Treasury FiscalData API",
            "ECOS StatisticSearch API",
            "KOSIS statisticsParameterData API",
            "BOJ Time-Series Data Search",
            "Census Economic Indicators RSS",
        ),
        notes="키 없이 열리는 공식 API/CSV/RSS/HTML 시계열을 먼저 쓰고, BEA/ECOS/KOSIS처럼 키가 필요한 API는 선택 검증으로만 사용한다.",
    ),
    DataSourceTier(
        tier=3,
        purpose="미국 공시/실적 맥락",
        sources=("SEC EDGAR", "OpenDART", "company IR RSS", "Nasdaq earnings calendar"),
        notes="원문 재배포 없이 공시 이벤트와 원문 링크만 보관한다.",
    ),
    DataSourceTier(
        tier=4,
        purpose="글로벌 뉴스/섹터 이슈",
        sources=("official RSS", "China NBS National Data", "GDELT DOC API", "Google News RSS", "trusted outlet RSS"),
        notes="본문 복제 없이 제목, 출처, 시각, 링크 중심으로 수집한다.",
    ),
    DataSourceTier(
        tier=5,
        purpose="코인 위험 선호 proxy",
        sources=("CoinGecko Demo/Public API", "Binance market data"),
        notes="BTC/ETH 중심으로만 쓰고 주식시장 보조지표로 제한한다.",
    ),
    DataSourceTier(
        tier=6,
        purpose="소셜/커뮤니티 반응",
        sources=("xAI X Search", "Groq search", "Gemini grounding", "Reddit RSS"),
        notes="기본 비활성화하고 월 예산 cap 안에서만 사용한다.",
    ),
)


US_MARKET_UNIVERSE: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "DIA",
    "IWM",
    "VIX_PROXY",
    "XLK",
    "XLY",
    "XLF",
    "XLE",
    "XLU",
    "XLV",
    "SMH",
    "SOXX",
    "DXY",
    "US10Y",
    "US2Y",
    "WTI",
    "GOLD",
    "EWY",
    "FXI",
    "KWEB",
)

KR_MARKET_UNIVERSE: tuple[str, ...] = (
    "KOSPI",
    "KOSDAQ",
    "KOSPI200",
    "USD_KRW",
    "EWY",
    "SOXX",
    "SMH",
    "US10Y",
    "DXY",
    "BTC",
    "ETH",
)


def build_free_source_plan(scope: MarketScope | str) -> Dict[str, object]:
    """시장 범위별 무료 데이터 수집 계획을 반환한다."""

    raw_scope = scope.value if isinstance(scope, MarketScope) else str(scope)
    try:
        normalized = MarketScope(raw_scope.strip().lower())
    except ValueError:
        normalized = MarketScope.EVERGREEN
    if normalized == MarketScope.US:
        universe: Sequence[str] = US_MARKET_UNIVERSE
        priority_keywords = (
            "Nasdaq",
            "Federal Reserve",
            "semiconductor",
            "AI chip",
            "Treasury yield",
            "inflation",
        )
    elif normalized == MarketScope.KR:
        universe = KR_MARKET_UNIVERSE
        priority_keywords = (
            "KOSPI",
            "USD/KRW",
            "semiconductor",
            "foreign investor",
            "battery",
            "Treasury yield",
        )
    elif normalized == MarketScope.GLOBAL:
        universe = tuple(dict.fromkeys((*KR_MARKET_UNIVERSE, *US_MARKET_UNIVERSE)))
        priority_keywords = ("global market", "risk on", "risk off", "dollar", "yield")
    else:
        universe = ()
        priority_keywords = ("투자 기준", "기록", "판단", "습관", "자동화")

    return {
        "scope": normalized.value,
        "tiers": [tier.__dict__ for tier in FREE_DATA_SOURCE_TIERS],
        "universe": list(universe),
        "priority_keywords": list(priority_keywords),
    }


def compute_source_confidence(
    *,
    official_source_count: int = 0,
    cross_source_match: float = 0.0,
    freshness_score: float = 0.0,
    historical_stability: float = 0.0,
) -> SourceConfidence:
    """무료 데이터 수집 결과의 신뢰도를 0~1 사이로 계산한다."""

    official_component = min(max(int(official_source_count), 0), 3) / 3.0
    score = (
        _clamp01(official_component) * 0.35
        + _clamp01(cross_source_match) * 0.30
        + _clamp01(freshness_score) * 0.20
        + _clamp01(historical_stability) * 0.15
    )
    mode = classify_data_mode(score)
    allow_numeric_claims = mode == DataMode.NUMERIC_BRIEFING

    if mode == DataMode.NUMERIC_BRIEFING:
        reason = "공식/교차/최신성 점수가 충분해 수치 기반 브리핑이 가능하다."
    elif mode == DataMode.CONDITIONAL_BRIEFING:
        reason = "일부 데이터가 부족하므로 방향성과 조건 중심으로 작성한다."
    else:
        reason = "데이터 신뢰도가 낮아 통찰형 글이나 체크리스트형 글로 전환한다."

    return SourceConfidence(
        score=round(score, 4),
        mode=mode,
        allow_numeric_claims=allow_numeric_claims,
        reason=reason,
    )


def classify_data_mode(score: float) -> DataMode:
    """신뢰도 점수에 따른 작성 모드를 반환한다."""

    normalized = _clamp01(score)
    if normalized >= 0.72:
        return DataMode.NUMERIC_BRIEFING
    if normalized >= 0.55:
        return DataMode.CONDITIONAL_BRIEFING
    return DataMode.INSIGHT_FALLBACK


def _clamp01(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, numeric))
