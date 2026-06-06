from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from modules.automation.job_store import Job
from modules.llm.claude_client import LLMResponse
from modules.llm.content_generator import ContentGenerator
from modules.market import (
    BlogSlot,
    DataMode,
    MarketDataPoint,
    MarketNewsItem,
    MarketScope,
    MarketSnapshot,
    SourceConfidence,
)
from modules.rag import CrossEncoderRagSearchEngine


class FakeCollector:
    """경제 뉴스 수집기 테스트 더블."""

    def __init__(self, items: List[Dict[str, str]]):
        self.items = items
        self.calls: List[List[str]] = []

    def fetch_relevant_news(self, keywords: List[str], max_items: int = 3):
        del max_items
        self.calls.append(list(keywords))
        return list(self.items)


class FakeLLMClient:
    """ContentGenerator 호출 순서 확인용 LLM 클라이언트."""

    def __init__(self, outputs: List[str]):
        self.outputs = list(outputs)
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "qwen"

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        del max_retries, temperature, max_tokens
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        return LLMResponse(
            content=self.outputs.pop(0),
            input_tokens=100,
            output_tokens=100,
            model="fake-model",
            stop_reason="end_turn",
        )


class FakeMarketCollector:
    """시장 스냅샷 주입 테스트용 collector."""

    def __init__(self, snapshot: MarketSnapshot):
        self.snapshot = snapshot
        self.calls: List[Dict[str, Any]] = []

    def collect(self, scope, *, slot=None, max_news_items=5):
        self.calls.append(
            {
                "scope": scope,
                "slot": slot,
                "max_news_items": max_news_items,
            }
        )
        return self.snapshot


PRE_ANALYSIS_OUTPUT = (
    '{"reader_current_knowledge":"기본 경제 지표 용어를 안다",'
    '"reader_misconceptions":["금리 동결이면 시장이 항상 안정된다"],'
    '"reader_top_questions":["금리 영향","환율 영향","실무 대응"],'
    '"emotional_curve":{"opening_emotion":"관심","turning_point":"핵심 데이터","closing_emotion":"안정감"},'
    '"recommended_structure":[{"h2":"핵심 요약","role":"정리"},{"h2":"시사점","role":"실행"}]}'
)


def _build_finance_job() -> Job:
    return Job(
        job_id="finance-job",
        status="running",
        title="오늘의 거시경제 체크",
        seed_keywords=["금리", "환율"],
        platform="naver",
        persona_id="P4",
        scheduled_at="2026-02-20T00:00:00Z",
    )


def _build_kr_market_job() -> Job:
    return Job(
        job_id="kr-market-job",
        status="running",
        title="국장 개장 전 브리핑",
        seed_keywords=["국장", "환율", "반도체"],
        platform="naver",
        persona_id="P4",
        scheduled_at="2026-06-05T23:10:00Z",
        category="경제 브리핑",
        tags=["market_daily", "market_slot:kr_preopen", "market_scope:kr"],
    )


def _build_finance_category_job_with_non_finance_persona() -> Job:
    return Job(
        job_id="finance-category-job",
        status="running",
        title="오늘의 경제 브리핑",
        seed_keywords=["시장", "브리핑"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-20T00:00:00Z",
        category="경제 브리핑",
    )


def test_economy_rag_appends_visible_sources():
    """경제 토픽은 뉴스 데이터를 프롬프트에 넣고 출처를 본문 하단에 추가해야 한다."""
    fake_collector = FakeCollector(
        [
            {
                "title": "한국 기준금리 동결",
                "link": "https://example.com/news/1",
                "content": "금융통화위원회가 기준금리를 동결했다.",
            }
        ]
    )
    fake_client = FakeLLMClient(
        [
            PRE_ANALYSIS_OUTPUT,
            "# 거시경제 정리\n\n본문 내용",
            '{"thumbnail": {"prompt": "thumb"}, "content_images": []}',
        ]
    )

    generator = ContentGenerator(
        client=fake_client,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        rss_news_collector=fake_collector,  # type: ignore[arg-type]
        rag_search_engine=CrossEncoderRagSearchEngine(
            news_collector=fake_collector,  # type: ignore[arg-type]
            cross_encoder_enabled=False,
        ),
    )
    result = asyncio.run(generator.generate(_build_finance_job()))

    assert fake_collector.calls == [["금리", "환율"]]
    assert any("<NewsData>" in call["user_prompt"] for call in fake_client.calls)
    assert "참고 자료: 한국 기준금리 동결 ( https://example.com/news/1 )" in result.final_content


def test_economy_rag_falls_back_when_no_news():
    """뉴스가 없으면 일반 템플릿으로 생성하고 출처를 강제하지 않아야 한다."""
    fake_collector = FakeCollector([])
    fake_client = FakeLLMClient(
        [
            PRE_ANALYSIS_OUTPUT,
            "# 일반 템플릿\n\n본문",
            '{"thumbnail": {"prompt": "thumb"}, "content_images": []}',
        ]
    )

    generator = ContentGenerator(
        client=fake_client,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        rss_news_collector=fake_collector,  # type: ignore[arg-type]
        rag_search_engine=CrossEncoderRagSearchEngine(
            news_collector=fake_collector,  # type: ignore[arg-type]
            cross_encoder_enabled=False,
        ),
    )
    result = asyncio.run(generator.generate(_build_finance_job()))

    assert all("<NewsData>" not in call["user_prompt"] for call in fake_client.calls)
    assert "참고 자료:" not in result.final_content


def test_economy_rag_uses_category_topic_even_when_persona_differs():
    """페르소나와 무관하게 카테고리 기반 토픽 해석으로 RAG를 적용해야 한다."""
    fake_collector = FakeCollector(
        [
            {
                "title": "원/달러 환율 약세",
                "link": "https://example.com/news/2",
                "content": "환율이 장중 변동성을 확대했다.",
            }
        ]
    )
    fake_client = FakeLLMClient(
        [
            PRE_ANALYSIS_OUTPUT,
            "# 경제 브리핑\n\n본문",
            '{"thumbnail": {"prompt": "thumb"}, "content_images": []}',
        ]
    )

    generator = ContentGenerator(
        client=fake_client,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        rss_news_collector=fake_collector,  # type: ignore[arg-type]
        rag_search_engine=CrossEncoderRagSearchEngine(
            news_collector=fake_collector,  # type: ignore[arg-type]
            cross_encoder_enabled=False,
        ),
    )
    result = asyncio.run(generator.generate(_build_finance_category_job_with_non_finance_persona()))

    assert fake_collector.calls == [["시장", "브리핑"]]
    assert any("<NewsData>" in call["user_prompt"] for call in fake_client.calls)
    assert "참고 자료: 원/달러 환율 약세 ( https://example.com/news/2 )" in result.final_content


def test_market_slot_injects_structured_snapshot_context():
    """시장 브리핑 태그가 있으면 무료 시장 스냅샷을 NewsData에 넣는다."""

    snapshot = MarketSnapshot(
        scope=MarketScope.KR,
        slot=BlogSlot.KR_PREOPEN,
        collected_at=datetime(2026, 6, 5, 23, 10, tzinfo=timezone.utc),
        data_points=(
            MarketDataPoint(
                symbol="KOSPI",
                source="Stooq",
                value=2870.25,
                observed_at=datetime(2026, 6, 5, 23, 10, tzinfo=timezone.utc),
                url="https://stooq.com/q/l/?s=%5Eks11",
            ),
        ),
        news_items=(
            MarketNewsItem(
                title="Semiconductor shares lead Asia",
                source="GDELT:example.com",
                url="https://example.com/semis",
                published_at=datetime(2026, 6, 5, 22, 0, tzinfo=timezone.utc),
            ),
        ),
        skipped_sources=(),
        confidence=SourceConfidence(
            score=0.58,
            mode=DataMode.CONDITIONAL_BRIEFING,
            allow_numeric_claims=False,
            reason="일부 데이터가 부족하므로 방향성과 조건 중심으로 작성한다.",
        ),
        fallback_topic_hints=("주도 섹터를 맞히려 하기보다 관찰 기준을 세우는 법",),
    )
    market_collector = FakeMarketCollector(snapshot)
    fake_client = FakeLLMClient(
        [
            PRE_ANALYSIS_OUTPUT,
            "# 국장 브리핑\n\n본문",
            '{"thumbnail": {"prompt": "thumb"}, "content_images": []}',
        ]
    )

    generator = ContentGenerator(
        client=fake_client,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        rss_news_collector=FakeCollector([]),  # type: ignore[arg-type]
        rag_search_engine=None,
        market_data_collector=market_collector,
    )
    result = asyncio.run(generator.generate(_build_kr_market_job()))

    assert market_collector.calls[0]["scope"] == MarketScope.KR
    assert market_collector.calls[0]["slot"] == BlogSlot.KR_PREOPEN
    prompts = "\n\n".join(call["user_prompt"] for call in fake_client.calls)
    assert "시장 데이터 스냅샷: KR_PREOPEN" in prompts
    assert "수치 단정 허용: 아니오" in prompts
    assert result.seo_snapshot["market_snapshot"]["mode"] == "conditional_briefing"
    assert result.seo_snapshot["market_snapshot"]["data_points"][0]["symbol"] == "KOSPI"
    assert result.seo_snapshot["market_snapshot"]["data_points"][0]["value"] == 2870.25
    assert result.seo_snapshot["market_snapshot"]["chart_recommended"] is False
    assert "참고 자료: 시장 데이터 스냅샷: KR_PREOPEN" in result.final_content
