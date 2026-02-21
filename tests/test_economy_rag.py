from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from modules.automation.job_store import Job
from modules.llm.claude_client import LLMResponse
from modules.llm.content_generator import ContentGenerator


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
    )
    result = asyncio.run(generator.generate(_build_finance_job()))

    assert fake_collector.calls == [["금리", "환율"]]
    assert "<NewsData>" in fake_client.calls[0]["user_prompt"]
    assert "참고 자료: 한국 기준금리 동결 ( https://example.com/news/1 )" in result.final_content


def test_economy_rag_falls_back_when_no_news():
    """뉴스가 없으면 일반 템플릿으로 생성하고 출처를 강제하지 않아야 한다."""
    fake_collector = FakeCollector([])
    fake_client = FakeLLMClient(
        [
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
    )
    result = asyncio.run(generator.generate(_build_finance_job()))

    assert "<NewsData>" not in fake_client.calls[0]["user_prompt"]
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
    )
    result = asyncio.run(generator.generate(_build_finance_category_job_with_non_finance_persona()))

    assert fake_collector.calls == [["시장", "브리핑"]]
    assert "<NewsData>" in fake_client.calls[0]["user_prompt"]
    assert "참고 자료: 원/달러 환율 약세 ( https://example.com/news/2 )" in result.final_content
