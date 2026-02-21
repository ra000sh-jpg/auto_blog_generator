import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest

from modules.automation.job_store import Job
from modules.exceptions import RateLimitError
from modules.llm.content_generator import ContentGenerator
from modules.llm.deepseek_client import DeepSeekClient
from modules.llm.provider_factory import create_client
from modules.llm.qwen_client import QwenClient


def build_job(job_id: str = "multi-provider-job") -> Job:
    """멀티 프로바이더 테스트용 Job 객체를 생성한다."""
    return Job(
        job_id=job_id,
        status="running",
        title="멀티 프로바이더 전략 테스트",
        seed_keywords=["Qwen", "DeepSeek", "블로그 자동화"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-19T00:00:00Z",
    )


class FakeLLMClient:
    """응답 큐 기반 테스트용 LLM 클라이언트."""

    def __init__(
        self,
        name: str,
        outputs: List[str],
        fail_once: bool = False,
        fail_error: Optional[Exception] = None,
    ):
        self.name = name
        self.outputs = list(outputs)
        self.fail_once = fail_once
        self.fail_error = fail_error
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return self.name

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        return await self.generate_with_retry(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        del max_retries
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.fail_once:
            self.fail_once = False
            if self.fail_error is not None:
                raise self.fail_error
            raise RuntimeError(f"{self.name} temporary error")
        if not self.outputs:
            raise RuntimeError(f"{self.name} outputs depleted")

        class Response:
            def __init__(self, content: str, model: str):
                self.content = content
                self.model = model
                self.input_tokens = 120
                self.output_tokens = 180
                self.stop_reason = "stop"

        return Response(self.outputs.pop(0), model=f"{self.name}-model")


def test_qwen_client_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    """Qwen API 키가 없으면 초기화에 실패해야 한다."""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ValueError):
        QwenClient(api_key=None)


def test_deepseek_client_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    """DeepSeek API 키가 없으면 초기화에 실패해야 한다."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError):
        DeepSeekClient(api_key=None)


def test_provider_factory_creates_correct_client(monkeypatch: pytest.MonkeyPatch):
    """Provider 팩토리가 올바른 클라이언트를 생성해야 한다."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-qwen-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")

    qwen = create_client("qwen")
    deepseek = create_client("deepseek")

    assert isinstance(qwen, QwenClient)
    assert isinstance(deepseek, DeepSeekClient)
    assert qwen.provider_name == "qwen"
    assert deepseek.provider_name == "deepseek"


def test_dual_model_strategy():
    """Primary(Qwen) 생성 후 Secondary(DeepSeek) 검증 흐름을 검증한다."""
    primary = FakeLLMClient("qwen", ["# 제목\n\n초안 본문"])
    secondary = FakeLLMClient(
        "deepseek",
        [
            "# 제목\n\nSEO 반영 본문",
            '{"score": 90, "issues": [], "summary": "좋음"}',
            "# 제목\n\nSEO 반영 본문",
            '{"thumbnail": {"prompt": "test"}, "content_images": []}',  # 이미지 프롬프트
        ],
    )
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
    )

    result = asyncio.run(generator.generate(build_job("dual-strategy")))
    assert result.quality_gate == "pass"
    assert result.llm_calls_used == 5  # draft + SEO + quality + voice rewrite + image prompts
    assert result.provider_used == "qwen"
    assert result.provider_fallback_from == ""
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 4  # SEO + quality + voice rewrite + image prompts


def test_fallback_when_primary_fails():
    """Primary 실패 시 Secondary로 fallback 되는지 검증한다."""
    primary = FakeLLMClient("qwen", outputs=[], fail_once=True)
    secondary = FakeLLMClient(
        "deepseek",
        [
            "# 제목\n\nfallback 초안",
            "# 제목\n\nfallback SEO",
            '{"score": 82, "issues": [], "summary": "fallback 성공"}',
            "# 제목\n\nfallback SEO",
            '{"thumbnail": {"prompt": "test"}, "content_images": []}',  # 이미지 프롬프트
        ],
    )
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
    )

    result = asyncio.run(generator.generate(build_job("fallback-case")))
    assert result.quality_gate == "pass"
    assert result.llm_calls_used == 5  # draft + SEO + quality + voice rewrite + image prompts
    assert result.provider_used == "deepseek"
    assert result.provider_fallback_from == "qwen"
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 5  # draft + SEO + quality + voice rewrite + image prompts


def test_fallback_when_primary_rate_limited():
    """Primary에서 429 RateLimitError 발생 시 Secondary로 폴백해야 한다."""
    primary = FakeLLMClient(
        "groq",
        outputs=[],
        fail_once=True,
        fail_error=RateLimitError("groq rate limited (429)"),
    )
    secondary = FakeLLMClient(
        "qwen",
        [
            "# 제목\n\nfallback 초안",
            "# 제목\n\nfallback SEO",
            '{"score": 85, "issues": [], "summary": "fallback 성공"}',
            "# 제목\n\nfallback SEO",
            '{"thumbnail": {"prompt": "test"}, "content_images": []}',
        ],
    )
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
    )

    result = asyncio.run(generator.generate(build_job("fallback-rate-limit")))
    assert result.quality_gate == "pass"
    assert result.provider_used == "qwen"
    assert result.provider_fallback_from == "groq"
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 5


def test_openai_compat_raises_rate_limit_error_on_429(monkeypatch: pytest.MonkeyPatch):
    """OpenAI 호환 클라이언트는 429 최종 실패를 RateLimitError로 표준화해야 한다."""
    from modules.llm.openai_compat_client import OpenAICompatClient

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = OpenAICompatClient(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        model="llama-3.3-70b-versatile",
        provider="groq",
        timeout_sec=5.0,
    )

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status_code=429, request=request)

    async def always_429(*args, **kwargs):
        del args, kwargs
        raise httpx.HTTPStatusError("429", request=request, response=response)

    monkeypatch.setattr(client, "generate", always_429)

    with pytest.raises(RateLimitError):
        asyncio.run(
            client.generate_with_retry(
                system_prompt="sys",
                user_prompt="user",
                max_retries=1,
            )
        )
