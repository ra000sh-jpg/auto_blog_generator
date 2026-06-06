"""무료 시장 데이터 수집 스냅샷 생성기."""

from __future__ import annotations

import csv
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import StringIO
from typing import Any, Mapping, Protocol, Sequence

from .slots import BlogSlot
from .sources import (
    DataMode,
    MarketScope,
    SourceConfidence,
    build_free_source_plan,
    compute_source_confidence,
)


@dataclass(frozen=True)
class MarketDataPoint:
    """시장 브리핑에 사용할 단일 수치 데이터."""

    symbol: str
    source: str
    value: float | None = None
    change_percent: float | None = None
    observed_at: datetime | None = None
    url: str = ""
    label: str = ""


@dataclass(frozen=True)
class MarketNewsItem:
    """시장 브리핑에 사용할 뉴스/공시 이벤트."""

    title: str
    source: str
    url: str
    published_at: datetime | None = None
    summary: str = ""
    relevance_keyword: str = ""


@dataclass(frozen=True)
class SkippedSource:
    """수집하지 못했거나 의도적으로 건너뛴 소스."""

    source: str
    reason: str


@dataclass(frozen=True)
class MarketSnapshot:
    """LLM 초안 생성기로 넘길 구조화 시장 스냅샷."""

    scope: MarketScope
    slot: BlogSlot | None
    collected_at: datetime
    data_points: tuple[MarketDataPoint, ...]
    news_items: tuple[MarketNewsItem, ...]
    skipped_sources: tuple[SkippedSource, ...]
    confidence: SourceConfidence
    fallback_topic_hints: tuple[str, ...]

    @property
    def data_mode(self) -> DataMode:
        """현재 스냅샷의 글 작성 모드를 반환한다."""

        return self.confidence.mode


class MarketTextFetcher(Protocol):
    """텍스트 기반 HTTP fetcher 프로토콜."""

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 8.0,
    ) -> str:
        """URL의 텍스트 응답을 반환한다."""


class UrllibMarketTextFetcher:
    """추가 의존성 없이 동작하는 기본 HTTP fetcher."""

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 8.0,
    ) -> str:
        """URL에서 텍스트를 읽는다."""

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                **dict(headers or {}),
            },
        )
        ssl_context = _build_ssl_context()
        if ssl_context is None:
            response_cm = urllib.request.urlopen(request, timeout=timeout_sec)  # nosec B310
        else:
            response_cm = urllib.request.urlopen(
                request,
                timeout=timeout_sec,
                context=ssl_context,
            )  # nosec B310
        with response_cm as response:
            raw = response.read(2_000_000)
            encoding = response.headers.get_content_charset() or "utf-8"
            return raw.decode(encoding, errors="replace")


class MarketDataCollector:
    """무료/저비용 소스에서 시장 브리핑용 데이터를 수집한다."""

    def __init__(
        self,
        *,
        fetcher: MarketTextFetcher | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float = 8.0,
    ):
        self.fetcher = fetcher or UrllibMarketTextFetcher()
        self.env = env if env is not None else os.environ
        self.timeout_sec = timeout_sec

    def collect(
        self,
        scope: MarketScope | str,
        *,
        slot: BlogSlot | None = None,
        now: datetime | None = None,
        max_news_items: int = 5,
    ) -> MarketSnapshot:
        """시장 범위별 무료 데이터 스냅샷을 만든다."""

        normalized_scope = _normalize_scope(scope)
        collected_at = _ensure_aware_utc(now or datetime.now(timezone.utc))
        data_points: list[MarketDataPoint] = []
        news_items: list[MarketNewsItem] = []
        skipped_sources: list[SkippedSource] = []

        data_points.extend(
            self._collect_stooq_quotes(normalized_scope, collected_at, skipped_sources)
        )
        data_points.extend(
            self._collect_fred_series(normalized_scope, collected_at, skipped_sources)
        )
        data_points.extend(
            self._collect_coingecko_prices(
                normalized_scope,
                collected_at,
                skipped_sources,
            )
        )
        data_points.extend(
            self._collect_binance_crypto(
                normalized_scope,
                collected_at,
                skipped_sources,
            )
        )
        news_items.extend(
            self._collect_rss_news(
                normalized_scope,
                collected_at,
                skipped_sources,
                max_items=max_news_items,
            )
        )
        news_items.extend(
            self._collect_gdelt_news(
                normalized_scope,
                collected_at,
                skipped_sources,
                max_items=max(0, max_news_items - len(news_items)),
            )
        )

        confidence = _estimate_confidence(
            scope=normalized_scope,
            collected_at=collected_at,
            data_points=data_points,
            news_items=news_items,
            skipped_sources=skipped_sources,
        )
        return MarketSnapshot(
            scope=normalized_scope,
            slot=slot,
            collected_at=collected_at,
            data_points=tuple(data_points),
            news_items=tuple(news_items),
            skipped_sources=tuple(skipped_sources),
            confidence=confidence,
            fallback_topic_hints=_build_fallback_topic_hints(normalized_scope, slot),
        )

    def _collect_stooq_quotes(
        self,
        scope: MarketScope,
        collected_at: datetime,
        skipped_sources: list[SkippedSource],
    ) -> list[MarketDataPoint]:
        points: list[MarketDataPoint] = []
        symbols = STOOQ_SYMBOLS_BY_SCOPE.get(scope, {})
        if not symbols:
            skipped_sources.append(SkippedSource("Stooq", f"{scope.value} 범위에는 Stooq 대상이 없습니다."))
            return points

        for symbol, stooq_symbol in symbols.items():
            url = _build_stooq_quote_url(stooq_symbol)
            try:
                text = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
                point = _parse_stooq_quote(
                    text,
                    symbol=symbol,
                    source_symbol=stooq_symbol,
                    url=url,
                    observed_at=collected_at,
                )
            except Exception as exc:
                skipped_sources.append(SkippedSource("Stooq", f"{symbol} 수집 실패: {exc}"))
                continue
            if point is None:
                skipped_sources.append(SkippedSource("Stooq", f"{symbol} 유효 값 없음"))
                continue
            points.append(point)
        return points

    def _collect_fred_series(
        self,
        scope: MarketScope,
        collected_at: datetime,
        skipped_sources: list[SkippedSource],
    ) -> list[MarketDataPoint]:
        points: list[MarketDataPoint] = []
        series_map = FRED_SERIES_BY_SCOPE.get(scope, {})
        if not series_map:
            skipped_sources.append(SkippedSource("FRED", f"{scope.value} 범위에는 FRED 대상이 없습니다."))
            return points

        api_key = _read_api_key(self.env, "FRED_API_KEY")
        if not api_key:
            skipped_sources.append(SkippedSource("FRED", "FRED_API_KEY 없음 - 공개 CSV로 대체 시도"))

        for symbol, series_id in series_map.items():
            url = _build_fred_url(series_id, api_key) if api_key else _build_fred_csv_url(series_id)
            try:
                text = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
                if api_key:
                    point = _parse_fred_observation(
                        text,
                        symbol=symbol,
                        series_id=series_id,
                        url=url,
                        fallback_observed_at=collected_at,
                    )
                else:
                    point = _parse_fred_csv_observation(
                        text,
                        symbol=symbol,
                        series_id=series_id,
                        url=url,
                        fallback_observed_at=collected_at,
                    )
            except Exception as exc:
                source_name = "FRED" if api_key else "FRED CSV"
                skipped_sources.append(SkippedSource(source_name, f"{series_id} 수집 실패: {exc}"))
                continue
            if point is None:
                source_name = "FRED" if api_key else "FRED CSV"
                skipped_sources.append(SkippedSource(source_name, f"{series_id} 유효 값 없음"))
                continue
            points.append(point)
        return points

    def _collect_coingecko_prices(
        self,
        scope: MarketScope,
        collected_at: datetime,
        skipped_sources: list[SkippedSource],
    ) -> list[MarketDataPoint]:
        if scope == MarketScope.EVERGREEN:
            return []

        ids = ",".join(COINGECKO_IDS_BY_SYMBOL.values())
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={urllib.parse.quote(ids)}"
            "&vs_currencies=usd&include_24hr_change=true"
        )
        headers: dict[str, str] = {}
        api_key = _read_api_key(self.env, "COINGECKO_API_KEY")
        if api_key:
            headers["x-cg-demo-api-key"] = api_key

        try:
            text = self.fetcher.get_text(
                url,
                headers=headers,
                timeout_sec=self.timeout_sec,
            )
            return _parse_coingecko_prices(text, url=url, observed_at=collected_at)
        except Exception as exc:
            skipped_sources.append(SkippedSource("CoinGecko", f"BTC/ETH 수집 실패: {exc}"))
            return []

    def _collect_binance_crypto(
        self,
        scope: MarketScope,
        collected_at: datetime,
        skipped_sources: list[SkippedSource],
    ) -> list[MarketDataPoint]:
        if scope == MarketScope.EVERGREEN:
            return []

        points: list[MarketDataPoint] = []
        for symbol, binance_symbol in BINANCE_SYMBOLS_BY_SYMBOL.items():
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={binance_symbol}"
            try:
                text = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
                point = _parse_binance_ticker(
                    text,
                    symbol=symbol,
                    binance_symbol=binance_symbol,
                    url=url,
                    observed_at=collected_at,
                )
            except Exception as exc:
                skipped_sources.append(SkippedSource("Binance", f"{binance_symbol} 수집 실패: {exc}"))
                continue
            if point is None:
                skipped_sources.append(SkippedSource("Binance", f"{binance_symbol} 유효 값 없음"))
                continue
            points.append(point)
        return points

    def _collect_gdelt_news(
        self,
        scope: MarketScope,
        collected_at: datetime,
        skipped_sources: list[SkippedSource],
        *,
        max_items: int,
    ) -> list[MarketNewsItem]:
        if max_items <= 0:
            return []

        query = _build_gdelt_query(scope)
        if not query:
            return []

        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={urllib.parse.quote(query)}"
            "&mode=ArtList&format=json&sort=HybridRel"
            f"&maxrecords={max(1, min(max_items, 10))}"
        )
        try:
            text = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
            return _parse_gdelt_articles(text, url=url, fallback_published_at=collected_at)
        except Exception as exc:
            skipped_sources.append(SkippedSource("GDELT", f"뉴스 수집 실패: {exc}"))
            return []

    def _collect_rss_news(
        self,
        scope: MarketScope,
        collected_at: datetime,
        skipped_sources: list[SkippedSource],
        *,
        max_items: int,
    ) -> list[MarketNewsItem]:
        if max_items <= 0:
            return []

        feeds = RSS_FEEDS_BY_SCOPE.get(scope, ())
        if not feeds:
            return []

        news_items: list[MarketNewsItem] = []
        for source, url in feeds:
            try:
                text = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
                news_items.extend(
                    _parse_rss_items(
                        text,
                        source=source,
                        fallback_url=url,
                        fallback_published_at=collected_at,
                        max_items=min(1, max_items - len(news_items)),
                    )
                )
            except Exception as exc:
                skipped_sources.append(SkippedSource(source, f"RSS 수집 실패: {exc}"))
                continue
            if len(news_items) >= max_items:
                break
        return news_items


def collect_market_snapshot(
    scope: MarketScope | str,
    *,
    slot: BlogSlot | None = None,
    now: datetime | None = None,
    fetcher: MarketTextFetcher | None = None,
    env: Mapping[str, str] | None = None,
    max_news_items: int = 5,
) -> MarketSnapshot:
    """함수형 API로 시장 스냅샷을 수집한다."""

    collector = MarketDataCollector(fetcher=fetcher, env=env)
    return collector.collect(
        scope,
        slot=slot,
        now=now,
        max_news_items=max_news_items,
    )


STOOQ_SYMBOLS_BY_SCOPE: dict[MarketScope, dict[str, str]] = {
    MarketScope.US: {
        "SPY": "spy.us",
        "QQQ": "qqq.us",
        "DIA": "dia.us",
        "IWM": "iwm.us",
        "SMH": "smh.us",
        "SOXX": "soxx.us",
        "EWY": "ewy.us",
        "FXI": "fxi.us",
        "KWEB": "kweb.us",
        "WTI": "cl.f",
        "GOLD": "gc.f",
    },
    MarketScope.KR: {
        "KOSPI": "^ks11",
        "KOSDAQ": "^kq11",
        "EWY": "ewy.us",
        "SMH": "smh.us",
        "SOXX": "soxx.us",
        "WTI": "cl.f",
        "GOLD": "gc.f",
    },
    MarketScope.GLOBAL: {
        "SPY": "spy.us",
        "QQQ": "qqq.us",
        "EWY": "ewy.us",
        "FXI": "fxi.us",
        "KWEB": "kweb.us",
        "WTI": "cl.f",
        "GOLD": "gc.f",
    },
}

FRED_SERIES_BY_SCOPE: dict[MarketScope, dict[str, str]] = {
    MarketScope.US: {
        "US10Y": "DGS10",
        "US2Y": "DGS2",
        "USD_KRW": "DEXKOUS",
    },
    MarketScope.KR: {
        "US10Y": "DGS10",
        "US2Y": "DGS2",
        "USD_KRW": "DEXKOUS",
    },
    MarketScope.GLOBAL: {
        "US10Y": "DGS10",
        "US2Y": "DGS2",
        "USD_KRW": "DEXKOUS",
    },
}

COINGECKO_IDS_BY_SYMBOL: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
}

BINANCE_SYMBOLS_BY_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}

RSS_FEEDS_BY_SCOPE: dict[MarketScope, tuple[tuple[str, str], ...]] = {
    MarketScope.US: (
        ("Federal Reserve RSS", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("BLS Principal Indicators RSS", "https://www.bls.gov/feed/bls_latest.rss"),
        ("BLS CPI RSS", "https://www.bls.gov/feed/cpi_latest.rss"),
        ("BEA News RSS", "https://apps.bea.gov/rss/rss.xml"),
        ("Census Economic Indicators RSS", "https://www.census.gov/economic-indicators/indicator.xml"),
        ("SEC RSS", "https://www.sec.gov/news/pressreleases.rss"),
    ),
    MarketScope.KR: (
        ("Federal Reserve RSS", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("BLS Principal Indicators RSS", "https://www.bls.gov/feed/bls_latest.rss"),
        ("BEA News RSS", "https://apps.bea.gov/rss/rss.xml"),
        ("Census Economic Indicators RSS", "https://www.census.gov/economic-indicators/indicator.xml"),
        (
            "Google News RSS",
            "https://news.google.com/rss/search?q=KOSPI%20OR%20USD%2FKRW%20OR%20semiconductor&hl=en-US&gl=US&ceid=US:en",
        ),
    ),
    MarketScope.GLOBAL: (
        ("Federal Reserve RSS", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("BLS Principal Indicators RSS", "https://www.bls.gov/feed/bls_latest.rss"),
        ("BEA News RSS", "https://apps.bea.gov/rss/rss.xml"),
        ("Census Economic Indicators RSS", "https://www.census.gov/economic-indicators/indicator.xml"),
        (
            "Google News RSS",
            "https://news.google.com/rss/search?q=global%20markets%20OR%20Treasury%20yields%20OR%20AI%20chip&hl=en-US&gl=US&ceid=US:en",
        ),
    ),
}

OFFICIAL_SOURCE_PREFIXES = (
    "FRED",
    "Federal Reserve",
    "BLS",
    "BEA",
    "Census",
    "SEC",
)

ABSENT_KEY_PREFIXES = (
    "your_",
    "changeme",
    "replace_",
    "test_",
)


def _build_stooq_quote_url(stooq_symbol: str) -> str:
    encoded = urllib.parse.quote(stooq_symbol)
    return f"https://stooq.com/q/l/?s={encoded}&f=sd2t2ohlcv&h&e=csv"


def _build_fred_url(series_id: str, api_key: str) -> str:
    query = urllib.parse.urlencode(
        {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": "5",
        }
    )
    return f"https://api.stlouisfed.org/fred/series/observations?{query}"


def _build_fred_csv_url(series_id: str) -> str:
    encoded = urllib.parse.quote(series_id)
    return f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={encoded}"


def _build_google_news_rss_url(query: str) -> str:
    encoded = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def _build_gdelt_query(scope: MarketScope) -> str:
    plan = build_free_source_plan(scope)
    keywords = [str(item) for item in plan.get("priority_keywords", []) if str(item).strip()]
    if not keywords:
        return ""
    return " OR ".join(f'"{keyword}"' if " " in keyword else keyword for keyword in keywords[:4])


def _parse_stooq_quote(
    text: str,
    *,
    symbol: str,
    source_symbol: str,
    url: str,
    observed_at: datetime,
) -> MarketDataPoint | None:
    reader = csv.DictReader(StringIO(text.strip()))
    for row in reader:
        close_value = _parse_float(row.get("Close"))
        if close_value is None:
            return None
        return MarketDataPoint(
            symbol=symbol,
            source="Stooq",
            value=close_value,
            observed_at=observed_at,
            url=url,
            label=source_symbol,
        )
    return None


def _parse_fred_observation(
    text: str,
    *,
    symbol: str,
    series_id: str,
    url: str,
    fallback_observed_at: datetime,
) -> MarketDataPoint | None:
    payload = json.loads(text)
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        return None

    for observation in observations:
        if not isinstance(observation, dict):
            continue
        value = _parse_float(observation.get("value"))
        if value is None:
            continue
        observed_at = _parse_date_value(observation.get("date")) or fallback_observed_at
        return MarketDataPoint(
            symbol=symbol,
            source="FRED",
            value=value,
            observed_at=observed_at,
            url=url,
            label=series_id,
        )
    return None


def _parse_fred_csv_observation(
    text: str,
    *,
    symbol: str,
    series_id: str,
    url: str,
    fallback_observed_at: datetime,
) -> MarketDataPoint | None:
    reader = csv.DictReader(StringIO(str(text or "").strip()))
    rows = list(reader)
    for row in reversed(rows):
        value = _parse_float(row.get(series_id) or row.get(symbol) or row.get("VALUE"))
        if value is None:
            continue
        observed_at = _parse_date_value(row.get("observation_date") or row.get("DATE") or row.get("Date"))
        return MarketDataPoint(
            symbol=symbol,
            source="FRED CSV",
            value=value,
            observed_at=observed_at or fallback_observed_at,
            url=url,
            label=series_id,
        )
    return None


def _parse_coingecko_prices(
    text: str,
    *,
    url: str,
    observed_at: datetime,
) -> list[MarketDataPoint]:
    payload = json.loads(text)
    points: list[MarketDataPoint] = []
    for symbol, coingecko_id in COINGECKO_IDS_BY_SYMBOL.items():
        item = payload.get(coingecko_id)
        if not isinstance(item, dict):
            continue
        value = _parse_float(item.get("usd"))
        if value is None:
            continue
        points.append(
            MarketDataPoint(
                symbol=symbol,
                source="CoinGecko",
                value=value,
                change_percent=_parse_float(item.get("usd_24h_change")),
                observed_at=observed_at,
                url=url,
                label=coingecko_id,
            )
        )
    return points


def _parse_binance_ticker(
    text: str,
    *,
    symbol: str,
    binance_symbol: str,
    url: str,
    observed_at: datetime,
) -> MarketDataPoint | None:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return None
    value = _parse_float(payload.get("lastPrice"))
    if value is None:
        return None
    return MarketDataPoint(
        symbol=symbol,
        source="Binance",
        value=value,
        change_percent=_parse_float(payload.get("priceChangePercent")),
        observed_at=observed_at,
        url=url,
        label=binance_symbol,
    )


def _parse_gdelt_articles(
    text: str,
    *,
    url: str,
    fallback_published_at: datetime,
) -> list[MarketNewsItem]:
    payload = json.loads(text)
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return []

    news_items: list[MarketNewsItem] = []
    seen_urls: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = _clean_text(str(article.get("title", "")))
        article_url = _clean_text(str(article.get("url", "")))
        if not title or not article_url or article_url in seen_urls:
            continue
        domain = _clean_text(str(article.get("domain", ""))) or "GDELT"
        published_at = _parse_gdelt_datetime(article.get("seendate")) or fallback_published_at
        news_items.append(
            MarketNewsItem(
                title=title,
                source=f"GDELT:{domain}",
                url=article_url or url,
                published_at=published_at,
                summary=_clean_text(str(article.get("socialimage", ""))),
            )
        )
        seen_urls.add(article_url)
    return news_items


def _parse_rss_items(
    text: str,
    *,
    source: str,
    fallback_url: str,
    fallback_published_at: datetime,
    max_items: int,
) -> list[MarketNewsItem]:
    if max_items <= 0:
        return []

    root = ET.fromstring(text)
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    news_items: list[MarketNewsItem] = []
    for item in items[:max_items]:
        title = _clean_text(_find_child_text(item, "title"))
        link = _clean_text(_find_child_text(item, "link"))
        if not link:
            link = _extract_atom_link(item) or fallback_url
        if not title:
            continue
        raw_published = (
            _find_child_text(item, "pubDate")
            or _find_child_text(item, "published")
            or _find_child_text(item, "updated")
        )
        news_items.append(
            MarketNewsItem(
                title=title,
                source=source,
                url=link,
                published_at=_parse_datetime_value(raw_published) or fallback_published_at,
                summary=_clean_text(
                    _find_child_text(item, "description")
                    or _find_child_text(item, "summary")
                    or _find_child_text(item, "content")
                ),
            )
        )
    return news_items


def _estimate_confidence(
    *,
    scope: MarketScope,
    collected_at: datetime,
    data_points: Sequence[MarketDataPoint],
    news_items: Sequence[MarketNewsItem],
    skipped_sources: Sequence[SkippedSource],
) -> SourceConfidence:
    official_sources = {
        item.source
        for item in data_points
        if item.source.startswith(OFFICIAL_SOURCE_PREFIXES)
    }
    official_sources.update(
        item.source
        for item in news_items
        if item.source.startswith(OFFICIAL_SOURCE_PREFIXES)
    )
    expected_points = 6 if scope in {MarketScope.KR, MarketScope.US} else 4
    coverage_score = min((len(data_points) + len(news_items) * 0.5) / expected_points, 1.0)
    source_diversity_score = min(
        len({item.source for item in data_points} | {item.source for item in news_items}) / 5,
        1.0,
    )
    freshness_score = _compute_freshness_score(
        collected_at=collected_at,
        timestamps=[
            *(item.observed_at for item in data_points),
            *(item.published_at for item in news_items),
        ],
    )
    attempted_count = len(data_points) + len(news_items) + len(skipped_sources)
    success_rate = (len(data_points) + len(news_items)) / max(attempted_count, 1)
    stability_score = 0.35 + min(success_rate, 1.0) * 0.65

    return compute_source_confidence(
        official_source_count=len(official_sources),
        cross_source_match=coverage_score * 0.65 + source_diversity_score * 0.35,
        freshness_score=freshness_score,
        historical_stability=stability_score,
    )


def _compute_freshness_score(
    *,
    collected_at: datetime,
    timestamps: Sequence[datetime | None],
) -> float:
    usable = [_ensure_aware_utc(item) for item in timestamps if item is not None]
    if not usable:
        return 0.0
    fresh_scores: list[float] = []
    for timestamp in usable:
        age_hours = abs((collected_at - timestamp).total_seconds()) / 3600
        if age_hours <= 12:
            fresh_scores.append(1.0)
        elif age_hours <= 48:
            fresh_scores.append(0.7)
        elif age_hours <= 120:
            fresh_scores.append(0.45)
        else:
            fresh_scores.append(0.2)
    return sum(fresh_scores) / len(fresh_scores)


def _build_fallback_topic_hints(
    scope: MarketScope,
    slot: BlogSlot | None,
) -> tuple[str, ...]:
    if scope == MarketScope.KR or slot == BlogSlot.KR_PREOPEN:
        return (
            "오늘 국장에서 숫자보다 먼저 확인할 투자자의 태도",
            "환율과 금리를 모를 때도 지켜야 할 초심자 체크리스트",
            "주도 섹터를 맞히려 하기보다 관찰 기준을 세우는 법",
        )
    if scope == MarketScope.US or slot == BlogSlot.US_PREOPEN:
        return (
            "미장 개장 전 초심자가 피해야 할 예측 과잉",
            "나스닥보다 내 판단 습관을 먼저 점검해야 하는 이유",
            "AI와 반도체 흐름을 장기투자 언어로 다시 읽는 법",
        )
    return (
        "투자 공부를 오래 지속하게 만드는 기록 습관",
        "자동화가 대신할 수 없는 투자자의 마지막 판단",
        "수익률보다 먼저 단단해져야 할 생활의 구조",
    )


def _normalize_scope(scope: MarketScope | str) -> MarketScope:
    if isinstance(scope, MarketScope):
        return scope
    try:
        return MarketScope(str(scope).strip().lower())
    except ValueError:
        return MarketScope.EVERGREEN


def _build_ssl_context() -> ssl.SSLContext | None:
    """macOS/Python 인증서 체인 문제를 피하기 위해 certifi CA를 우선 사용한다."""

    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _read_api_key(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "")).strip()
    if not value:
        return ""
    lowered = value.lower()
    if any(lowered.startswith(prefix) for prefix in ABSENT_KEY_PREFIXES):
        return ""
    if lowered in {"none", "null", "demo", "placeholder"}:
        return ""
    return value


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date_value(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_datetime_value(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        return _ensure_aware_utc(parsed)
    except Exception:
        pass
    try:
        return _ensure_aware_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_gdelt_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y%m%d%H%M%S", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return _parse_datetime_value(text)


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _find_child_text(element: ET.Element, local_name: str) -> str:
    for child in element:
        if _local_name(child.tag) == local_name:
            return child.text or ""
    return ""


def _extract_atom_link(element: ET.Element) -> str:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "")
        if href:
            return href
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
