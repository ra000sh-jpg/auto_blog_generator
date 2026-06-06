"""시장 브리핑 블로그용 슬롯/데이터 정책 모듈."""

from .slots import (
    BlogSlot,
    MarketOpenState,
    get_default_daily_slots,
    get_us_preopen_kst,
    resolve_daily_slots,
)
from .sources import (
    DataMode,
    FREE_DATA_SOURCE_TIERS,
    MarketScope,
    SourceConfidence,
    build_free_source_plan,
    classify_data_mode,
    compute_source_confidence,
)
from .free_data_collector import (
    MarketDataCollector,
    MarketDataPoint,
    MarketNewsItem,
    MarketSnapshot,
    MarketTextFetcher,
    SkippedSource,
    UrllibMarketTextFetcher,
    collect_market_snapshot,
)
from .opportunity import (
    ContentBrief,
    KR_PREOPEN_SEEDS,
    KeywordSignal,
    MarketOpportunityEngine,
    OpportunityCandidate,
    OpportunityScore,
    OpportunityScorer,
    select_best_kr_preopen_opportunity,
)

__all__ = [
    "BlogSlot",
    "MarketOpenState",
    "get_default_daily_slots",
    "get_us_preopen_kst",
    "resolve_daily_slots",
    "DataMode",
    "FREE_DATA_SOURCE_TIERS",
    "MarketScope",
    "SourceConfidence",
    "build_free_source_plan",
    "classify_data_mode",
    "compute_source_confidence",
    "MarketDataCollector",
    "MarketDataPoint",
    "MarketNewsItem",
    "MarketSnapshot",
    "MarketTextFetcher",
    "SkippedSource",
    "UrllibMarketTextFetcher",
    "collect_market_snapshot",
    "ContentBrief",
    "KR_PREOPEN_SEEDS",
    "KeywordSignal",
    "MarketOpportunityEngine",
    "OpportunityCandidate",
    "OpportunityScore",
    "OpportunityScorer",
    "select_best_kr_preopen_opportunity",
]
