from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

from modules.market import (
    BlogSlot,
    MarketDataPoint,
    MarketNewsItem,
    MarketScope,
    MarketSnapshot,
    SkippedSource,
    SourcePackCollector,
    append_source_pack_section,
    compute_source_confidence,
    render_source_pack_section,
    source_pack_from_market_snapshot,
)


class FakeMarketCollector:
    def collect(
        self,
        scope,
        *,
        slot=None,
        now=None,
        max_news_items=5,
    ) -> MarketSnapshot:
        collected_at = now or datetime(2026, 6, 23, tzinfo=timezone.utc)
        return _snapshot(collected_at=collected_at)


class FakeFetcher:
    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 8.0,
    ) -> str:
        del url, headers, timeout_sec
        return json.dumps(
            {
                "name": "Apple Inc.",
                "filings": {
                    "recent": {
                        "form": ["10-Q", "8-K"],
                        "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                        "filingDate": ["2026-05-01", "2026-04-20"],
                    }
                },
            }
        )


def _snapshot(*, collected_at: datetime) -> MarketSnapshot:
    confidence = compute_source_confidence(
        official_source_count=1,
        cross_source_match=0.9,
        freshness_score=0.9,
        historical_stability=0.9,
    )
    return MarketSnapshot(
        scope=MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        collected_at=collected_at,
        data_points=(
            MarketDataPoint(
                symbol="US10Y",
                source="FRED",
                value=4.2,
                observed_at=collected_at,
                url="https://fred.stlouisfed.org/series/DGS10",
                label="DGS10",
            ),
            MarketDataPoint(
                symbol="BTC",
                source="CoinGecko",
                value=104000.0,
                observed_at=collected_at,
                url="https://api.coingecko.com/api/v3/simple/price",
                label="bitcoin",
            ),
            MarketDataPoint(
                symbol="ETH",
                source="Binance",
                value=2500.0,
                observed_at=collected_at,
                url="https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT",
                label="ETHUSDT",
            ),
        ),
        news_items=(
            MarketNewsItem(
                title="Federal Reserve press release",
                source="Federal Reserve RSS",
                url="https://www.federalreserve.gov/",
                published_at=collected_at,
            ),
        ),
        skipped_sources=(SkippedSource("SEC", "optional in this fixture"),),
        confidence=confidence,
        fallback_topic_hints=(),
    )


def test_source_pack_from_market_snapshot_allows_numeric_briefing():
    pack = source_pack_from_market_snapshot(
        _snapshot(collected_at=datetime(2026, 6, 23, tzinfo=timezone.utc)),
        topic="미장 개장 전 브리핑",
    )
    data = pack.to_dict()

    assert data["schema_version"] == "source_pack.v1"
    assert data["official_source_count"] >= 1
    assert data["market_data_source_count"] >= 2
    assert len(data["confirmed_metrics"]) >= 3
    assert data["publish_allowed"] is True


def test_source_pack_treats_china_and_japan_sources_as_official():
    collected_at = datetime(2026, 6, 23, tzinfo=timezone.utc)
    confidence = compute_source_confidence(
        official_source_count=2,
        cross_source_match=0.8,
        freshness_score=0.9,
        historical_stability=0.8,
    )
    snapshot = MarketSnapshot(
        scope=MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        collected_at=collected_at,
        data_points=(
            MarketDataPoint(
                symbol="USD_JPY_BOJ",
                source="BOJ Time-Series Data Search",
                value=145.32,
                observed_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
                url="https://www.stat-search.boj.or.jp/ssi/mtshtml/fm08_d_1.html",
                label="FM08 FXERD04 Tokyo interbank USD/JPY",
            ),
            MarketDataPoint(
                symbol="KOSPI",
                source="Stooq",
                value=2870.5,
                observed_at=collected_at,
                url="https://stooq.com/",
                label="^ks11",
            ),
            MarketDataPoint(
                symbol="BTC",
                source="CoinGecko",
                value=104000.0,
                observed_at=collected_at,
                url="https://api.coingecko.com/api/v3/simple/price",
                label="bitcoin",
            ),
        ),
        news_items=(
            MarketNewsItem(
                title="China NBS Consumer Price Index official table",
                source="China NBS National Data",
                url="https://data.stats.gov.cn/english/tablequery.htm?code=AA0108",
                published_at=collected_at,
            ),
        ),
        skipped_sources=(),
        confidence=confidence,
        fallback_topic_hints=(),
    )

    data = source_pack_from_market_snapshot(snapshot, topic="국장 개장 전 브리핑").to_dict()

    assert data["official_source_count"] >= 2
    assert any(metric["key"] == "USD_JPY_BOJ" for metric in data["confirmed_metrics"])
    assert any(
        source["source"] == "China NBS National Data"
        and source["source_type"] == "official_news"
        for source in data["sources"]
    )


def test_source_pack_collector_adds_sec_filings_when_cik_is_configured():
    collector = SourcePackCollector(
        market_collector=FakeMarketCollector(),
        fetcher=FakeFetcher(),
        env={
            "AUTOBLOG_SEC_CIKS": "320193",
            "SEC_USER_AGENT": "AutoBlogGenerator test@example.com",
        },
    )

    pack = collector.collect(
        topic="미장 개장 전 브리핑",
        scope=MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        now=datetime(2026, 6, 23, tzinfo=timezone.utc),
    ).to_dict()

    assert any(source["source"] == "SEC EDGAR" for source in pack["sources"])
    assert any(metric["source"] == "SEC EDGAR" for metric in pack["confirmed_metrics"])


def test_render_source_pack_section_is_concise_and_deduped():
    pack = source_pack_from_market_snapshot(
        _snapshot(collected_at=datetime(2026, 6, 23, tzinfo=timezone.utc)),
        topic="미장 개장 전 브리핑",
    ).to_dict()

    section = render_source_pack_section(pack, max_items=3)

    assert section.startswith("■ 참고한 공식/시장 데이터")
    assert "FRED" in section
    assert section.count("•") == 3


def test_append_source_pack_section_is_idempotent():
    pack = source_pack_from_market_snapshot(
        _snapshot(collected_at=datetime(2026, 6, 23, tzinfo=timezone.utc)),
        topic="미장 개장 전 브리핑",
    ).to_dict()

    once = append_source_pack_section("본문입니다.", pack, max_items=2)
    twice = append_source_pack_section(once, pack, max_items=2)

    assert once == twice
    assert "■ 참고한 공식/시장 데이터" in once
