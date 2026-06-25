"""시장 글의 근거 묶음(Source Pack) 스키마와 수집기."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from .free_data_collector import (
    MarketDataCollector,
    MarketDataPoint,
    MarketNewsItem,
    MarketSnapshot,
    MarketTextFetcher,
    SkippedSource,
)
from .slots import BlogSlot
from .sources import MarketScope


OFFICIAL_SOURCE_KEYWORDS = (
    "fred",
    "fred csv",
    "federal reserve",
    "bls",
    "bea",
    "census",
    "sec",
    "treasury",
    "ecos",
    "kosis",
    "opendart",
    "boj",
    "bank of japan",
    "china nbs",
    "national bureau of statistics",
)

MARKET_DATA_SOURCE_KEYWORDS = (
    "stooq",
    "coingecko",
    "binance",
    "yfinance",
    "yahoo",
    "nasdaq",
    "cme",
)


@dataclass(frozen=True)
class SourceEvidence:
    """Source Pack에 남길 단일 근거 출처."""

    source: str
    source_type: str
    title: str
    url: str = ""
    observed_at: str = ""
    collected_at: str = ""
    metric_key: str = ""
    value: float | None = None
    change_percent: float | None = None
    raw_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        payload: dict[str, Any] = {
            "source": self.source,
            "source_type": self.source_type,
            "title": self.title,
            "url": self.url,
            "observed_at": self.observed_at,
            "collected_at": self.collected_at,
            "metric_key": self.metric_key,
            "raw_id": self.raw_id,
        }
        if self.value is not None:
            payload["value"] = self.value
        if self.change_percent is not None:
            payload["change_percent"] = self.change_percent
        return payload


@dataclass(frozen=True)
class ConfirmedMetric:
    """본문에 사용할 수 있는 확인된 수치."""

    key: str
    label: str
    value: float
    source: str
    source_url: str = ""
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "source_url": self.source_url,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class SourcePack:
    """한 글의 발행 전 근거 패키지."""

    topic: str
    scope: str
    collected_at: str
    sources: tuple[SourceEvidence, ...] = ()
    confirmed_metrics: tuple[ConfirmedMetric, ...] = ()
    missing_sources: tuple[str, ...] = ()
    quality_score: float = 0.0
    publish_allowed: bool = False
    reasons: tuple[str, ...] = ()
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def official_source_count(self) -> int:
        """공식/1차 기관 출처 수를 반환한다."""

        return _count_unique_sources(self.sources, OFFICIAL_SOURCE_KEYWORDS)

    @property
    def market_data_source_count(self) -> int:
        """시장 데이터 출처 수를 반환한다."""

        return _count_unique_sources(self.sources, MARKET_DATA_SOURCE_KEYWORDS)

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "schema_version": "source_pack.v1",
            "topic": self.topic,
            "scope": self.scope,
            "collected_at": self.collected_at,
            "sources": [source.to_dict() for source in self.sources],
            "confirmed_metrics": [metric.to_dict() for metric in self.confirmed_metrics],
            "missing_sources": list(self.missing_sources),
            "official_source_count": self.official_source_count,
            "market_data_source_count": self.market_data_source_count,
            "missing_source_count": len(self.missing_sources),
            "quality_score": round(float(self.quality_score), 4),
            "publish_allowed": bool(self.publish_allowed),
            "reasons": list(self.reasons),
            "policy": dict(self.policy),
        }


class SourcePackMarketCollector(Protocol):
    """SourcePackCollector가 기대하는 시장 수집기 인터페이스."""

    def collect(
        self,
        scope: MarketScope | str,
        *,
        slot: BlogSlot | None = None,
        now: datetime | None = None,
        max_news_items: int = 5,
    ) -> MarketSnapshot:
        """시장 스냅샷을 수집한다."""


class SourcePackCollector:
    """FRED, SEC, CoinGecko, Binance 등 1차 근거를 Source Pack으로 묶는다."""

    def __init__(
        self,
        *,
        market_collector: SourcePackMarketCollector | None = None,
        fetcher: MarketTextFetcher | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float = 8.0,
    ):
        self.env = env if env is not None else os.environ
        self.market_collector = market_collector or MarketDataCollector(fetcher=fetcher, env=self.env)
        self.fetcher = fetcher
        self.timeout_sec = timeout_sec

    def collect(
        self,
        *,
        topic: str,
        scope: MarketScope | str,
        slot: BlogSlot | None = None,
        now: datetime | None = None,
        max_news_items: int = 5,
    ) -> SourcePack:
        """시장 스냅샷과 SEC 보강 수집을 Source Pack으로 변환한다."""

        collected_at = _iso_utc(now or datetime.now(timezone.utc))
        snapshot = self.market_collector.collect(
            scope,
            slot=slot,
            now=now,
            max_news_items=max_news_items,
        )
        pack = source_pack_from_market_snapshot(snapshot, topic=topic, collected_at=collected_at)
        sec_sources, sec_metrics, sec_missing = self._collect_sec_sources(collected_at=collected_at)
        merged = SourcePack(
            topic=pack.topic,
            scope=pack.scope,
            collected_at=pack.collected_at,
            sources=(*pack.sources, *sec_sources),
            confirmed_metrics=(*pack.confirmed_metrics, *sec_metrics),
            missing_sources=tuple(dict.fromkeys((*pack.missing_sources, *sec_missing))),
        )
        return evaluate_source_pack(merged)

    def _collect_sec_sources(
        self,
        *,
        collected_at: str,
    ) -> tuple[tuple[SourceEvidence, ...], tuple[ConfirmedMetric, ...], tuple[str, ...]]:
        """환경변수 CIK 목록을 기준으로 SEC 최근 공시를 수집한다."""

        fetcher = self.fetcher
        if fetcher is None:
            market_collector = getattr(self.market_collector, "fetcher", None)
            fetcher = market_collector if hasattr(market_collector, "get_text") else None
        if fetcher is None:
            return (), (), ("SEC: fetcher unavailable",)

        cik_values = _split_env_list(self.env.get("AUTOBLOG_SEC_CIKS", ""))
        if not cik_values:
            return (), (), ("SEC: AUTOBLOG_SEC_CIKS not configured",)

        user_agent = str(
            self.env.get("SEC_USER_AGENT")
            or self.env.get("AUTOBLOG_SEC_USER_AGENT")
            or "AutoBlogGenerator/1.0 contact@example.com"
        ).strip()
        sources: list[SourceEvidence] = []
        metrics: list[ConfirmedMetric] = []
        missing: list[str] = []
        headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}

        for raw_cik in cik_values[:8]:
            cik = _normalize_cik(raw_cik)
            if not cik:
                continue
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            try:
                text = fetcher.get_text(url, headers=headers, timeout_sec=self.timeout_sec)
                payload = json.loads(text)
            except Exception as exc:
                missing.append(f"SEC:{cik} collection failed: {exc}")
                continue

            company_name = str(payload.get("name", "") or f"CIK {cik}").strip()
            recent = payload.get("filings", {}).get("recent", {}) if isinstance(payload.get("filings"), dict) else {}
            forms = recent.get("form", []) if isinstance(recent, dict) else []
            accession_numbers = recent.get("accessionNumber", []) if isinstance(recent, dict) else []
            filing_dates = recent.get("filingDate", []) if isinstance(recent, dict) else []
            count = 0
            for index, form in enumerate(forms[:3] if isinstance(forms, list) else []):
                accession = _list_value(accession_numbers, index)
                filing_date = _list_value(filing_dates, index)
                source_url = _build_sec_filing_url(cik, accession) or url
                source = SourceEvidence(
                    source="SEC EDGAR",
                    source_type="official",
                    title=f"{company_name} {form} filing",
                    url=source_url,
                    observed_at=str(filing_date or ""),
                    collected_at=collected_at,
                    metric_key="sec_recent_filing",
                    raw_id=str(accession or ""),
                )
                sources.append(source)
                count += 1
            if count > 0:
                metrics.append(
                    ConfirmedMetric(
                        key=f"SEC_{cik}_RECENT_FILINGS",
                        label=f"{company_name} recent SEC filings",
                        value=float(count),
                        source="SEC EDGAR",
                        source_url=url,
                        observed_at=collected_at,
                    )
                )
            else:
                missing.append(f"SEC:{cik} no recent filings")

        return tuple(sources), tuple(metrics), tuple(missing)


SOURCE_SECTION_HEADING = "■ 참고한 공식/시장 데이터"


def render_source_pack_section(
    source_pack: Mapping[str, Any],
    *,
    max_items: int = 6,
) -> str:
    """Source Pack을 블로그 하단 출처 섹션 텍스트로 렌더링한다."""

    sources = _source_section_items(source_pack, max_items=max_items)
    if not sources:
        return ""

    lines = [SOURCE_SECTION_HEADING]
    for item in sources:
        observed_at = str(item.get("observed_at", "") or "").strip()
        observed_suffix = f" / 기준일: {observed_at[:10]}" if observed_at else ""
        metric_key = str(item.get("metric_key", "") or "").strip()
        metric_suffix = f" ({metric_key})" if metric_key else ""
        lines.append(
            f"• {item['source']} - {item['title']}{metric_suffix}{observed_suffix}"
        )
    lines.append("※ 위 자료는 글 작성 시점의 참고 데이터이며, 투자 판단의 전체 근거가 아닙니다.")
    return "\n".join(lines).strip()


def append_source_pack_section(
    content: str,
    source_pack: Mapping[str, Any],
    *,
    max_items: int = 6,
) -> str:
    """본문에 Source Pack 출처 섹션을 중복 없이 추가한다."""

    text = str(content or "").rstrip()
    if not text:
        return text
    if SOURCE_SECTION_HEADING in text:
        return text
    section = render_source_pack_section(source_pack, max_items=max_items)
    if not section:
        return text
    return f"{text}\n\n{section}".strip()


def source_pack_from_market_snapshot(
    snapshot: MarketSnapshot,
    *,
    topic: str = "",
    collected_at: str = "",
) -> SourcePack:
    """MarketSnapshot을 Source Pack으로 변환한다."""

    pack_collected_at = collected_at or _iso_utc(getattr(snapshot, "collected_at", None) or datetime.now(timezone.utc))
    scope = str(getattr(getattr(snapshot, "scope", ""), "value", getattr(snapshot, "scope", "")) or "")
    sources: list[SourceEvidence] = []
    metrics: list[ConfirmedMetric] = []
    missing: list[str] = []

    for point in getattr(snapshot, "data_points", ()) or ():
        evidence = _evidence_from_market_data_point(point, collected_at=pack_collected_at)
        if evidence:
            sources.append(evidence)
        metric = _metric_from_market_data_point(point)
        if metric:
            metrics.append(metric)

    for item in getattr(snapshot, "news_items", ()) or ():
        evidence = _evidence_from_market_news_item(item, collected_at=pack_collected_at)
        if evidence:
            sources.append(evidence)

    for skipped in getattr(snapshot, "skipped_sources", ()) or ():
        text = _missing_from_skipped_source(skipped)
        if text:
            missing.append(text)

    return evaluate_source_pack(
        SourcePack(
            topic=str(topic or "").strip(),
            scope=scope,
            collected_at=pack_collected_at,
            sources=tuple(sources),
            confirmed_metrics=tuple(metrics),
            missing_sources=tuple(dict.fromkeys(missing)),
        )
    )


def source_pack_from_payload(payload: Mapping[str, Any], *, topic: str = "") -> dict[str, Any]:
    """ready payload의 market_snapshot/source_pack을 표준 Source Pack dict로 정규화한다."""

    existing = payload.get("source_pack")
    if isinstance(existing, Mapping):
        return normalize_source_pack_dict(existing, topic=topic)

    seo_snapshot = payload.get("seo_snapshot")
    if not isinstance(seo_snapshot, Mapping):
        return normalize_source_pack_dict({}, topic=topic)

    market_snapshot = seo_snapshot.get("market_snapshot")
    if not isinstance(market_snapshot, Mapping):
        return normalize_source_pack_dict({}, topic=topic)

    nested = market_snapshot.get("source_pack")
    if isinstance(nested, Mapping):
        return normalize_source_pack_dict(nested, topic=topic)

    collected_at = str(market_snapshot.get("collected_at") or datetime.now(timezone.utc).isoformat())
    scope = str(market_snapshot.get("scope", "") or "")
    sources: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []

    raw_points = market_snapshot.get("data_points", [])
    if isinstance(raw_points, Sequence) and not isinstance(raw_points, (str, bytes, bytearray)):
        for raw in raw_points:
            if not isinstance(raw, Mapping):
                continue
            source_name = str(raw.get("source", "") or "").strip()
            symbol = str(raw.get("symbol", "") or raw.get("label", "") or "").strip()
            url = str(raw.get("url", "") or "").strip()
            observed_at = str(raw.get("observed_at", "") or "").strip()
            value = _float_or_none(raw.get("value"))
            change_percent = _float_or_none(raw.get("change_percent"))
            source_type = "official" if _is_named_source(source_name, OFFICIAL_SOURCE_KEYWORDS) else "market_data"
            sources.append(
                {
                    "source": source_name,
                    "source_type": source_type,
                    "title": f"{symbol or source_name} data point",
                    "url": url,
                    "observed_at": observed_at,
                    "collected_at": collected_at,
                    "metric_key": symbol,
                    "value": value,
                    "change_percent": change_percent,
                }
            )
            if value is not None:
                metrics.append(
                    {
                        "key": symbol or source_name,
                        "label": str(raw.get("label", "") or symbol or source_name),
                        "value": value,
                        "source": source_name,
                        "source_url": url,
                        "observed_at": observed_at,
                    }
                )

    missing_sources: list[str] = []
    for key in ("skipped_sources", "missing_sources"):
        raw_missing = market_snapshot.get(key, [])
        if isinstance(raw_missing, Sequence) and not isinstance(raw_missing, (str, bytes, bytearray)):
            for item in raw_missing:
                if isinstance(item, Mapping):
                    source = str(item.get("source", "") or "").strip()
                    reason = str(item.get("reason", "") or "").strip()
                    if source or reason:
                        missing_sources.append(f"{source}: {reason}".strip(": "))
                else:
                    text = str(item or "").strip()
                    if text:
                        missing_sources.append(text)

    return normalize_source_pack_dict(
        {
            "schema_version": "source_pack.v1",
            "topic": topic,
            "scope": scope,
            "collected_at": collected_at,
            "sources": sources,
            "confirmed_metrics": metrics,
            "missing_sources": missing_sources,
        },
        topic=topic,
    )


def normalize_source_pack_dict(raw: Mapping[str, Any], *, topic: str = "") -> dict[str, Any]:
    """느슨한 Source Pack 입력을 검증 결과가 포함된 표준 dict로 만든다."""

    sources = tuple(_source_evidence_from_dict(item) for item in _as_mapping_list(raw.get("sources")))
    sources = tuple(item for item in sources if item is not None)
    metrics = tuple(_confirmed_metric_from_dict(item) for item in _as_mapping_list(raw.get("confirmed_metrics")))
    metrics = tuple(item for item in metrics if item is not None)
    missing = tuple(
        dict.fromkeys(str(item or "").strip() for item in _as_sequence(raw.get("missing_sources")) if str(item or "").strip())
    )
    pack = SourcePack(
        topic=str(raw.get("topic") or topic or "").strip(),
        scope=str(raw.get("scope", "") or "").strip(),
        collected_at=str(raw.get("collected_at") or datetime.now(timezone.utc).isoformat()),
        sources=sources,
        confirmed_metrics=metrics,
        missing_sources=missing,
    )
    return evaluate_source_pack(pack).to_dict()


def evaluate_source_pack(
    pack: SourcePack,
    *,
    min_official_sources: int = 1,
    min_market_data_sources: int = 2,
    min_confirmed_metrics: int = 3,
    max_missing_sources: int = 3,
) -> SourcePack:
    """Source Pack이 발행 가능한 근거 수준인지 평가한다."""

    official_count = pack.official_source_count
    market_count = pack.market_data_source_count
    metric_count = len(pack.confirmed_metrics)
    missing_count = len(pack.missing_sources)
    reasons: list[str] = []
    if official_count < min_official_sources:
        reasons.append(f"official_source_count {official_count}/{min_official_sources}")
    if market_count < min_market_data_sources:
        reasons.append(f"market_data_source_count {market_count}/{min_market_data_sources}")
    if metric_count < min_confirmed_metrics:
        reasons.append(f"confirmed_metric_count {metric_count}/{min_confirmed_metrics}")
    if missing_count > max_missing_sources:
        reasons.append(f"missing_source_count {missing_count}/{max_missing_sources}")

    quality_score = (
        min(official_count / max(min_official_sources, 1), 1.0) * 0.35
        + min(market_count / max(min_market_data_sources, 1), 1.0) * 0.25
        + min(metric_count / max(min_confirmed_metrics, 1), 1.0) * 0.30
        + (1.0 if missing_count <= max_missing_sources else 0.0) * 0.10
    )

    return SourcePack(
        topic=pack.topic,
        scope=pack.scope,
        collected_at=pack.collected_at,
        sources=pack.sources,
        confirmed_metrics=pack.confirmed_metrics,
        missing_sources=pack.missing_sources,
        quality_score=round(quality_score, 4),
        publish_allowed=not reasons,
        reasons=tuple(reasons),
        policy={
            "min_official_sources": min_official_sources,
            "min_market_data_sources": min_market_data_sources,
            "min_confirmed_metrics": min_confirmed_metrics,
            "max_missing_sources": max_missing_sources,
        },
    )


def evaluate_source_pack_dict(
    raw: Mapping[str, Any],
    *,
    min_official_sources: int = 1,
    min_market_data_sources: int = 2,
    min_confirmed_metrics: int = 3,
    max_missing_sources: int = 3,
) -> dict[str, Any]:
    """dict Source Pack을 정책으로 재평가한다."""

    normalized = normalize_source_pack_dict(raw)
    sources = tuple(_source_evidence_from_dict(item) for item in _as_mapping_list(normalized.get("sources")))
    metrics = tuple(_confirmed_metric_from_dict(item) for item in _as_mapping_list(normalized.get("confirmed_metrics")))
    pack = SourcePack(
        topic=str(normalized.get("topic", "") or ""),
        scope=str(normalized.get("scope", "") or ""),
        collected_at=str(normalized.get("collected_at", "") or ""),
        sources=tuple(item for item in sources if item is not None),
        confirmed_metrics=tuple(item for item in metrics if item is not None),
        missing_sources=tuple(str(item or "").strip() for item in _as_sequence(normalized.get("missing_sources")) if str(item or "").strip()),
    )
    return evaluate_source_pack(
        pack,
        min_official_sources=min_official_sources,
        min_market_data_sources=min_market_data_sources,
        min_confirmed_metrics=min_confirmed_metrics,
        max_missing_sources=max_missing_sources,
    ).to_dict()


def _evidence_from_market_data_point(
    point: MarketDataPoint,
    *,
    collected_at: str,
) -> SourceEvidence | None:
    source = str(getattr(point, "source", "") or "").strip()
    symbol = str(getattr(point, "symbol", "") or "").strip()
    if not source and not symbol:
        return None
    source_type = "official" if _is_named_source(source, OFFICIAL_SOURCE_KEYWORDS) else "market_data"
    observed_at = _iso_or_text(getattr(point, "observed_at", ""))
    return SourceEvidence(
        source=source,
        source_type=source_type,
        title=f"{symbol or source} market data",
        url=str(getattr(point, "url", "") or "").strip(),
        observed_at=observed_at,
        collected_at=collected_at,
        metric_key=symbol,
        value=_float_or_none(getattr(point, "value", None)),
        change_percent=_float_or_none(getattr(point, "change_percent", None)),
        raw_id=str(getattr(point, "label", "") or "").strip(),
    )


def _source_section_items(
    source_pack: Mapping[str, Any],
    *,
    max_items: int,
) -> list[dict[str, str]]:
    raw_sources = _as_mapping_list(source_pack.get("sources"))
    priority = {
        "official": 0,
        "official_news": 1,
        "market_data": 2,
        "news_context": 3,
    }
    sorted_sources = sorted(
        raw_sources,
        key=lambda item: priority.get(str(item.get("source_type", "") or "").strip().lower(), 9),
    )
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sorted_sources:
        source = str(item.get("source", "") or "").strip()
        title = _clean_section_title(str(item.get("title", "") or item.get("metric_key", "") or "").strip())
        metric_key = str(item.get("metric_key", "") or "").strip()
        observed_at = str(item.get("observed_at", "") or "").strip()
        if not source or not title:
            continue
        key = (source.lower(), title.lower(), metric_key.lower())
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "source": source,
                "title": title,
                "metric_key": metric_key,
                "observed_at": observed_at,
            }
        )
        if len(items) >= max(1, int(max_items)):
            break
    return items


def _clean_section_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = text.replace(" market data", " data")
    return text[:120]


def _metric_from_market_data_point(point: MarketDataPoint) -> ConfirmedMetric | None:
    value = _float_or_none(getattr(point, "value", None))
    if value is None:
        return None
    symbol = str(getattr(point, "symbol", "") or "").strip()
    source = str(getattr(point, "source", "") or "").strip()
    return ConfirmedMetric(
        key=symbol or source,
        label=str(getattr(point, "label", "") or symbol or source).strip(),
        value=value,
        source=source,
        source_url=str(getattr(point, "url", "") or "").strip(),
        observed_at=_iso_or_text(getattr(point, "observed_at", "")),
    )


def _evidence_from_market_news_item(
    item: MarketNewsItem,
    *,
    collected_at: str,
) -> SourceEvidence | None:
    source = str(getattr(item, "source", "") or "").strip()
    title = str(getattr(item, "title", "") or "").strip()
    if not source and not title:
        return None
    source_type = "official_news" if _is_named_source(source, OFFICIAL_SOURCE_KEYWORDS) else "news_context"
    return SourceEvidence(
        source=source,
        source_type=source_type,
        title=title or source,
        url=str(getattr(item, "url", "") or "").strip(),
        observed_at=_iso_or_text(getattr(item, "published_at", "")),
        collected_at=collected_at,
    )


def _missing_from_skipped_source(skipped: SkippedSource) -> str:
    source = str(getattr(skipped, "source", "") or "").strip()
    reason = str(getattr(skipped, "reason", "") or "").strip()
    if source or reason:
        return f"{source}: {reason}".strip(": ")
    return ""


def _source_evidence_from_dict(raw: Mapping[str, Any]) -> SourceEvidence | None:
    source = str(raw.get("source", "") or "").strip()
    title = str(raw.get("title", "") or raw.get("metric_key", "") or source).strip()
    if not source and not title:
        return None
    value = _float_or_none(raw.get("value"))
    change_percent = _float_or_none(raw.get("change_percent"))
    source_type = str(raw.get("source_type", "") or "").strip()
    if not source_type:
        if _is_named_source(source, OFFICIAL_SOURCE_KEYWORDS):
            source_type = "official"
        elif _is_named_source(source, MARKET_DATA_SOURCE_KEYWORDS):
            source_type = "market_data"
        else:
            source_type = "news_context"
    return SourceEvidence(
        source=source,
        source_type=source_type,
        title=title,
        url=str(raw.get("url", "") or "").strip(),
        observed_at=str(raw.get("observed_at", "") or "").strip(),
        collected_at=str(raw.get("collected_at", "") or "").strip(),
        metric_key=str(raw.get("metric_key", "") or "").strip(),
        value=value,
        change_percent=change_percent,
        raw_id=str(raw.get("raw_id", "") or "").strip(),
    )


def _confirmed_metric_from_dict(raw: Mapping[str, Any]) -> ConfirmedMetric | None:
    value = _float_or_none(raw.get("value"))
    if value is None:
        return None
    key = str(raw.get("key", "") or raw.get("label", "") or raw.get("source", "") or "").strip()
    source = str(raw.get("source", "") or "").strip()
    if not key or not source:
        return None
    return ConfirmedMetric(
        key=key,
        label=str(raw.get("label", "") or key).strip(),
        value=value,
        source=source,
        source_url=str(raw.get("source_url", "") or raw.get("url", "") or "").strip(),
        observed_at=str(raw.get("observed_at", "") or "").strip(),
    )


def _count_unique_sources(sources: Sequence[SourceEvidence], keywords: Sequence[str]) -> int:
    unique: set[str] = set()
    for source in sources:
        if _is_named_source(source.source, keywords) or _is_named_source(source.source_type, keywords):
            unique.add(_normalize_source_name(source.source) or _normalize_source_name(source.source_type))
    return len(unique)


def _is_named_source(value: str, keywords: Sequence[str]) -> bool:
    normalized = _normalize_source_name(value)
    return any(keyword in normalized for keyword in keywords)


def _normalize_source_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_or_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value or "")
    return str(value or "").strip()


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _as_sequence(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(value)


def _split_env_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _normalize_cik(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    return digits.zfill(10)


def _list_value(values: Any, index: int) -> Any:
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        if 0 <= index < len(values):
            return values[index]
    return ""


def _build_sec_filing_url(cik: str, accession: Any) -> str:
    accession_text = str(accession or "").strip()
    if not accession_text:
        return ""
    cik_int = str(int(cik)) if cik.isdigit() else cik.lstrip("0")
    accession_path = urllib.parse.quote(accession_text.replace("-", ""))
    accession_file = urllib.parse.quote(accession_text)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_path}/{accession_file}-index.html"
