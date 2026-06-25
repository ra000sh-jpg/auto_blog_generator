import json
from datetime import date, datetime, timezone

import pytest

from modules.automation.job_store import Job
from modules.llm.content_generator import ContentGenerator
from modules.llm.prompts import QUALITY_LAYER_ECONOMY_PROMPT
from modules.llm.model_upgrade_policy import ModelCandidateEvaluation, decide_model_upgrade
from modules.llm.provider_factory import create_client
from modules.market import (
    BlogSlot,
    DataMode,
    MarketDataCollector,
    MarketOpenState,
    MarketScope,
    build_free_source_plan,
    collect_market_snapshot,
    compute_source_confidence,
    get_us_preopen_kst,
    resolve_daily_slots,
)


def test_weekend_slots_convert_to_evergreen_and_reflection():
    """주말에는 시장 브리핑을 통찰형 글 슬롯으로 대체한다."""
    resolved = resolve_daily_slots(
        MarketOpenState(krx_open=False, us_open=False, is_weekend=True)
    )

    assert resolved[BlogSlot.KR_PREOPEN] == BlogSlot.EVERGREEN_INSIGHT
    assert resolved[BlogSlot.US_PREOPEN] == BlogSlot.EVERGREEN_INSIGHT
    assert resolved[BlogSlot.EVERGREEN_INSIGHT] == BlogSlot.WEEKLY_REFLECTION


def test_single_market_holiday_only_replaces_that_slot():
    """국장만 휴장일 때는 국장 브리핑만 통찰 글로 대체한다."""
    resolved = resolve_daily_slots(
        MarketOpenState(krx_open=False, us_open=True, is_weekend=False)
    )

    assert resolved[BlogSlot.KR_PREOPEN] == BlogSlot.EVERGREEN_INSIGHT
    assert resolved[BlogSlot.US_PREOPEN] == BlogSlot.US_PREOPEN


def test_us_preopen_uses_new_york_timezone():
    """미장 개장 전 시각은 미국 동부시간 기준으로 계산한다."""
    summer = get_us_preopen_kst(date(2026, 6, 8), minutes_before_open=60)
    winter = get_us_preopen_kst(date(2026, 12, 8), minutes_before_open=60)

    assert summer.hour == 21
    assert summer.minute == 30
    assert winter.hour == 22
    assert winter.minute == 30


def test_us_preopen_default_matches_operating_plan():
    """기본 미장 브리핑 시각은 서머타임 20:30, 표준시 21:30 KST다."""
    summer = get_us_preopen_kst(date(2026, 6, 8))
    winter = get_us_preopen_kst(date(2026, 12, 8))

    assert (summer.hour, summer.minute) == (20, 30)
    assert (winter.hour, winter.minute) == (21, 30)


def test_us_free_source_plan_contains_overseas_market_universe():
    """미장 브리핑은 해외 ETF/금리/달러/공시 데이터를 넓게 본다."""
    plan = build_free_source_plan(MarketScope.US)

    assert plan["scope"] == "us"
    assert "QQQ" in plan["universe"]
    assert "SMH" in plan["universe"]
    assert "US10Y" in plan["universe"]
    assert "Federal Reserve" in plan["priority_keywords"]


def test_source_confidence_blocks_numeric_claims_when_weak():
    """데이터 신뢰도가 낮으면 수치 단정 대신 통찰형 글로 전환한다."""
    weak = compute_source_confidence(
        official_source_count=0,
        cross_source_match=0.2,
        freshness_score=0.3,
        historical_stability=0.4,
    )

    assert weak.mode == DataMode.INSIGHT_FALLBACK
    assert weak.allow_numeric_claims is False


def test_source_confidence_allows_numeric_claims_when_strong():
    """공식/교차/최신성 조건이 충분하면 수치 기반 브리핑을 허용한다."""
    strong = compute_source_confidence(
        official_source_count=3,
        cross_source_match=0.9,
        freshness_score=0.9,
        historical_stability=0.8,
    )

    assert strong.mode == DataMode.NUMERIC_BRIEFING
    assert strong.allow_numeric_claims is True


def test_model_upgrade_requires_budget_and_quality():
    """새 모델 자동 승격은 비용/품질/기능 조건을 모두 통과해야 한다."""
    decision = decide_model_upgrade(
        ModelCandidateEvaluation(
            provider="deepseek",
            model="deepseek-v4-next",
            current_avg_cost_per_1m_usd=0.21,
            candidate_avg_cost_per_1m_usd=0.19,
            current_quality_score=88,
            candidate_quality_score=90,
            json_supported=True,
            tool_calls_supported=True,
            numeric_hallucination_count=0,
            persona_score_delta=1.0,
            consecutive_successes=3,
        )
    )

    assert decision.approved_for_auto_switch is True
    assert decision.action == "auto_switch_candidate"


def test_provider_factory_supports_deepseek_v4_default(monkeypatch):
    """DeepSeek 기본 모델은 V4 Flash여야 한다."""
    pytest.importorskip("httpx")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek")

    deepseek = create_client("deepseek")

    assert deepseek.model == "deepseek-v4-flash"


class FakeMarketFetcher:
    """네트워크 없이 시장 수집기를 검증하기 위한 fetcher."""

    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get_text(self, url, *, headers=None, timeout_sec=8.0):
        self.urls.append(url)
        for marker, response in self.responses.items():
            if marker in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise RuntimeError(f"no fake response: {url}")


def test_market_snapshot_skips_missing_paid_keys_without_crashing():
    """키가 없으면 공식 유료/준유료 소스를 건너뛰고 무료 소스만 시도한다."""
    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nSPY.US,2026-06-05,22:00:00,1,1,1,621.5,100\n",
            "gdeltproject": '{"articles":[{"title":"Nasdaq futures steady before payrolls","url":"https://example.com/a","domain":"example.com","seendate":"20260605080000"}]}',
            "coingecko": '{"bitcoin":{"usd":70000,"usd_24h_change":1.2},"ethereum":{"usd":3500,"usd_24h_change":0.8}}',
            "binance.com": '{"lastPrice":"70010.0","priceChangePercent":"1.1"}',
        }
    )

    snapshot = collect_market_snapshot(
        MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={},
    )

    assert any(item.source == "FRED" and "FRED_API_KEY 없음" in item.reason for item in snapshot.skipped_sources)
    assert not any("api.stlouisfed.org" in url for url in fetcher.urls)
    assert any("api.gdeltproject.org" in url for url in fetcher.urls)
    assert any(point.symbol == "QQQ" for point in snapshot.data_points)


def test_market_snapshot_uses_fred_public_csv_without_api_key():
    """FRED 키가 없어도 공개 CSV로 주요 금리 데이터를 보강해야 한다."""
    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nSPY.US,2026-06-05,22:00:00,1,1,1,621.5,100\n",
            "id=DGS10": "observation_date,DGS10\n2026-06-04,4.12\n",
            "id=DGS2": "observation_date,DGS2\n2026-06-04,3.91\n",
            "id=DEXKOUS": "observation_date,DEXKOUS\n2026-06-04,1365.5\n",
            "gdeltproject": '{"articles":[]}',
            "coingecko": '{"bitcoin":{"usd":70000,"usd_24h_change":1.2},"ethereum":{"usd":3500,"usd_24h_change":0.8}}',
            "binance.com": '{"lastPrice":"70010.0","priceChangePercent":"1.1"}',
        }
    )

    snapshot = collect_market_snapshot(
        MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={},
    )

    assert any(item.source == "FRED" and "공개 CSV" in item.reason for item in snapshot.skipped_sources)
    assert not any("api.stlouisfed.org" in url for url in fetcher.urls)
    assert any("fred.stlouisfed.org/graph/fredgraph.csv" in url for url in fetcher.urls)
    assert any(point.symbol == "US10Y" and point.source == "FRED CSV" for point in snapshot.data_points)


def test_market_snapshot_collects_official_macro_api_points():
    """BLS/BEA/미 재무부 공식 API 수치를 Source Pack 후보 데이터로 수집해야 한다."""
    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nSPY.US,2026-06-05,22:00:00,1,1,1,621.5,100\n",
            "id=DGS10": "observation_date,DGS10\n2026-06-04,4.12\n",
            "id=DGS2": "observation_date,DGS2\n2026-06-04,3.91\n",
            "id=DEXKOUS": "observation_date,DEXKOUS\n2026-06-04,1365.5\n",
            "CUSR0000SA0": (
                '{"status":"REQUEST_SUCCEEDED","Results":{"series":[{"seriesID":"CUSR0000SA0",'
                '"data":[{"year":"2026","period":"M05","value":"321.465"}]}]}}'
            ),
            "LNS14000000": (
                '{"status":"REQUEST_SUCCEEDED","Results":{"series":[{"seriesID":"LNS14000000",'
                '"data":[{"year":"2026","period":"M05","value":"4.1"}]}]}}'
            ),
            "apps.bea.gov/api/data": (
                '{"BEAAPI":{"Results":{"Data":['
                '{"LineNumber":"1","TimePeriod":"2025Q4","DataValue":"2.3"},'
                '{"LineNumber":"1","TimePeriod":"2026Q1","DataValue":"1.6"}'
                "]}}}"
            ),
            "daily_treasury_rates": (
                '{"data":[{"record_date":"2026-06-05","bc_2year":"3.91",'
                '"bc_10year":"4.12","bc_30year":"4.75"}]}'
            ),
            "coingecko": '{"bitcoin":{"usd":70000,"usd_24h_change":1.2},"ethereum":{"usd":3500,"usd_24h_change":0.8}}',
            "binance.com": '{"lastPrice":"70010.0","priceChangePercent":"1.1"}',
        }
    )

    snapshot = collect_market_snapshot(
        MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={"BEA_API_KEY": "bea-key"},
        max_news_items=0,
    )
    by_symbol = {point.symbol: point for point in snapshot.data_points}

    assert by_symbol["US_CPI"].source == "BLS"
    assert by_symbol["US_CPI"].observed_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert by_symbol["US_UNEMPLOYMENT_RATE"].value == 4.1
    assert by_symbol["US_REAL_GDP_GROWTH"].source == "BEA"
    assert by_symbol["US_REAL_GDP_GROWTH"].observed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert by_symbol["US10Y_TREASURY"].source == "U.S. Treasury FiscalData"
    assert by_symbol["US30Y_TREASURY"].value == 4.75
    assert snapshot.data_mode == DataMode.NUMERIC_BRIEFING


def test_market_snapshot_collects_korean_official_sources():
    """국장 브리핑은 ECOS/KOSIS/OpenDART 공식 근거를 함께 담을 수 있어야 한다."""
    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nKOSPI,2026-06-05,15:30:00,1,1,1,2870.5,100\n",
            "ecos.bok.or.kr/api/StatisticSearch": (
                '{"StatisticSearch":{"row":[{"TIME":"20260605","DATA_VALUE":"1368.2",'
                '"ITEM_NAME1":"원/미국달러"}]}}'
            ),
            "statisticsParameterData.do": (
                '[{"PRD_SE":"M","PRD_DE":"202605","DT":"58210.4",'
                '"TBL_NM":"수출입 총괄","ITM_NM":"수출액"}]'
            ),
            "opendart.fss.or.kr/api/list.json": (
                '{"status":"000","message":"정상","list":['
                '{"corp_name":"삼성전자","report_nm":"분기보고서","rcept_no":"20260605000123",'
                '"rcept_dt":"20260605","stock_code":"005930","corp_cls":"Y"},'
                '{"corp_name":"SK하이닉스","report_nm":"주요사항보고서","rcept_no":"20260604000456",'
                '"rcept_dt":"20260604","stock_code":"000660","corp_cls":"Y"}'
                "]}"
            ),
        }
    )

    snapshot = collect_market_snapshot(
        MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={
            "ECOS_API_KEY": "ecos-secret",
            "KOSIS_API_KEY": "kosis-secret",
            "OPENDART_API_KEY": "dart-secret",
            "AUTOBLOG_OPENDART_CORP_CODES": "00126380,00164779",
            "AUTOBLOG_ECOS_SERIES": json.dumps(
                [
                    {
                        "symbol": "KR_USD_KRW_ECOS",
                        "stat_code": "731Y001",
                        "cycle": "D",
                        "item_code1": "0000001",
                        "label": "USD/KRW",
                    }
                ]
            ),
            "AUTOBLOG_KOSIS_SERIES": json.dumps(
                [
                    {
                        "symbol": "KR_EXPORT_KOSIS",
                        "org_id": "101",
                        "tbl_id": "DT_TEST",
                        "itm_id": "T1",
                        "obj_l1": "ALL",
                        "prd_se": "M",
                        "label": "수출액",
                    }
                ]
            ),
        },
        max_news_items=2,
    )
    by_symbol = {point.symbol: point for point in snapshot.data_points}

    assert by_symbol["KR_USD_KRW_ECOS"].source == "ECOS"
    assert by_symbol["KR_USD_KRW_ECOS"].value == 1368.2
    assert by_symbol["KR_USD_KRW_ECOS"].observed_at == datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert "ecos-secret" not in by_symbol["KR_USD_KRW_ECOS"].url
    assert by_symbol["KR_EXPORT_KOSIS"].source == "KOSIS"
    assert by_symbol["KR_EXPORT_KOSIS"].observed_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert "kosis-secret" not in by_symbol["KR_EXPORT_KOSIS"].url
    assert any(item.source == "OpenDART" and "삼성전자" in item.title for item in snapshot.news_items)
    assert any("dart.fss.or.kr/dsaf001" in item.url for item in snapshot.news_items)


def test_market_snapshot_collects_china_and_japan_keyless_sources():
    """중국/일본 확장은 키 없이 BOJ 수치와 NBS 공식 맥락을 담아야 한다."""
    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nKOSPI,2026-06-05,15:30:00,1,1,1,2870.5,100\n",
            "stat-search.boj.or.jp": (
                "<html><body>"
                "更新日時：2026/06/23 15:00\n"
                "為替相場（東京インターバンク相場）（日次）\n"
                "2026/06/18 145.10\n"
                "2026/06/19 145.32\n"
                "</body></html>"
            ),
        }
    )

    snapshot = collect_market_snapshot(
        MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        now=datetime(2026, 6, 23, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={},
        max_news_items=2,
    )
    by_symbol = {point.symbol: point for point in snapshot.data_points}

    assert by_symbol["USD_JPY_BOJ"].source == "BOJ Time-Series Data Search"
    assert by_symbol["USD_JPY_BOJ"].value == 145.32
    assert by_symbol["USD_JPY_BOJ"].observed_at == datetime(2026, 6, 19, tzinfo=timezone.utc)
    assert any(
        item.source == "China NBS National Data" and "Consumer Price Index" in item.title
        for item in snapshot.news_items
    )
    assert not any("easyquery.htm" in url for url in fetcher.urls)


def test_market_snapshot_collects_keyless_official_us_rss_feeds():
    """BLS/BEA/Census RSS는 키 없이 미국 브리핑 공식 맥락으로 들어와야 한다."""
    fetcher = FakeMarketFetcher(
        {
            "bls.gov/feed/bls_latest.rss": (
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>BLS principal indicators update</title>"
                "<link>https://www.bls.gov/example</link>"
                "<pubDate>Fri, 05 Jun 2026 08:00:00 GMT</pubDate>"
                "</item></channel></rss>"
            ),
            "apps.bea.gov/rss/rss.xml": (
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>BEA releases personal income data</title>"
                "<link>https://www.bea.gov/example</link>"
                "<pubDate>Fri, 05 Jun 2026 07:30:00 GMT</pubDate>"
                "</item></channel></rss>"
            ),
            "census.gov/economic-indicators/indicator.xml": (
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>Census retail trade indicator</title>"
                "<link>https://www.census.gov/example</link>"
                "<pubDate>Fri, 05 Jun 2026 07:00:00 GMT</pubDate>"
                "</item></channel></rss>"
            ),
        }
    )

    snapshot = collect_market_snapshot(
        MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={},
        max_news_items=3,
    )

    sources = {item.source for item in snapshot.news_items}
    assert "BLS Principal Indicators RSS" in sources
    assert "BEA News RSS" in sources
    assert "Census Economic Indicators RSS" in sources


def test_market_snapshot_allows_numeric_mode_when_sources_are_rich():
    """무료 ETF/코인과 공식 FRED/RSS가 함께 있으면 수치 브리핑 모드가 된다."""
    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nSPY.US,2026-06-05,22:00:00,1,1,1,621.5,100\n",
            "api.stlouisfed.org": '{"observations":[{"date":"2026-06-04","value":"4.12"}]}',
            "coingecko": '{"bitcoin":{"usd":70000,"usd_24h_change":1.2},"ethereum":{"usd":3500,"usd_24h_change":0.8}}',
            "binance.com": '{"lastPrice":"70010.0","priceChangePercent":"1.1"}',
            "gdeltproject": '{"articles":[{"title":"Federal Reserve speakers keep market cautious","url":"https://example.com/b","domain":"example.com","seendate":"20260605080000"}]}',
            "federalreserve.gov": (
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>Federal Reserve issues statement</title>"
                "<link>https://www.federalreserve.gov/example</link>"
                "<pubDate>Fri, 05 Jun 2026 08:00:00 GMT</pubDate>"
                "</item></channel></rss>"
            ),
            "sec.gov": (
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>SEC announces market structure update</title>"
                "<link>https://www.sec.gov/example</link>"
                "<pubDate>Fri, 05 Jun 2026 07:00:00 GMT</pubDate>"
                "</item></channel></rss>"
            ),
        }
    )
    collector = MarketDataCollector(
        fetcher=fetcher,
        env={"FRED_API_KEY": "fred-key", "COINGECKO_API_KEY": "cg-key"},
    )

    snapshot = collector.collect(
        MarketScope.US,
        slot=BlogSlot.US_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
    )

    assert snapshot.data_mode == DataMode.NUMERIC_BRIEFING
    assert snapshot.confidence.allow_numeric_claims is True
    assert any(point.symbol == "BTC" and point.source == "CoinGecko" for point in snapshot.data_points)
    assert any(item.source == "Federal Reserve RSS" for item in snapshot.news_items)


def test_market_snapshot_falls_back_to_insight_when_data_is_missing():
    """데이터 확보가 거의 실패하면 통찰형 글 힌트를 제공한다."""
    fetcher = FakeMarketFetcher({})

    snapshot = collect_market_snapshot(
        MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={},
    )

    assert snapshot.data_mode == DataMode.INSIGHT_FALLBACK
    assert snapshot.confidence.allow_numeric_claims is False
    assert snapshot.fallback_topic_hints
    assert "국장" in snapshot.fallback_topic_hints[0]


class DummyClient:
    @property
    def provider_name(self) -> str:
        return "dummy"


def test_economy_prompt_locks_complete_easy_market_post_shape():
    """경제 브리핑 프롬프트는 짧은 초안/깨진 표/프롬프트 라벨을 막아야 한다."""

    assert "최소 1,500자" in QUALITY_LAYER_ECONOMY_PROMPT
    assert "H2 소제목 최소 4개" in QUALITY_LAYER_ECONOMY_PROMPT
    assert "모든 행의 열 개수" in QUALITY_LAYER_ECONOMY_PROMPT
    assert "미수집" in QUALITY_LAYER_ECONOMY_PROMPT
    assert "[출력]" in QUALITY_LAYER_ECONOMY_PROMPT
    assert "프롬프트 라벨" in QUALITY_LAYER_ECONOMY_PROMPT


def test_market_snapshot_context_carries_easy_shape_rules():
    """시장 스냅샷 문맥도 글 구조/표 완결성 규칙을 전달해야 한다."""

    fetcher = FakeMarketFetcher(
        {
            "stooq.com": "Symbol,Date,Time,Open,High,Low,Close,Volume\nEWY.US,2026-06-05,22:00:00,1,1,1,70.5,100\n",
            "coingecko": '{"bitcoin":{"usd":70000,"usd_24h_change":1.2},"ethereum":{"usd":3500,"usd_24h_change":0.8}}',
            "binance.com": '{"lastPrice":"70010.0","priceChangePercent":"1.1"}',
        }
    )
    snapshot = collect_market_snapshot(
        MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        now=datetime(2026, 6, 5, 8, 10, tzinfo=timezone.utc),
        fetcher=fetcher,
        env={},
    )
    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    context = generator._market_snapshot_to_context(snapshot)
    content = context["content"]

    assert "H2 소제목을 최소 4개" in content
    assert "최소 1,500자" in content
    assert "모든 행의 열 개수" in content
    assert "미수집" in content
    assert "[출력]" in content


def test_evergreen_market_slot_injection_requires_complete_insight_post():
    """통찰형 슬롯은 짧은 미완성 글 대신 완결된 기준 글을 요구해야 한다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )
    job = Job(
        job_id="evergreen-injection",
        status="running",
        title="투자 공부 노트 - 예측보다 먼저 세울 기준",
        seed_keywords=["투자 공부", "리스크 관리"],
        platform="naver",
        persona_id="P4",
        scheduled_at="2026-06-05T00:00:00Z",
        category="경제 브리핑",
        tags=["market_daily", "market_slot:evergreen_insight", "market_scope:evergreen"],
    )

    injection = generator._build_market_slot_writing_injection(job)

    assert "최소 1,500자" in injection
    assert "H2 소제목을 최소 4개" in injection
    assert "생활 제약" in injection
    assert "중간에서 끊긴 문장" in injection
