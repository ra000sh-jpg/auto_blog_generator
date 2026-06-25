import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

import httpx
import pytest

from modules.automation.job_store import Job
from modules.exceptions import RateLimitError
from modules.llm.circuit_breaker import ProviderCircuitBreaker
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


class AlwaysRateLimitClient:
    """항상 RateLimitError를 반환하는 테스트용 클라이언트."""

    def __init__(self, name: str):
        self.name = name
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return self.name

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        del max_retries, temperature, max_tokens
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        raise RateLimitError(f"{self.name} rate limited (429)")


class StaticSuccessClient:
    """항상 동일한 성공 응답을 반환하는 테스트용 클라이언트."""

    def __init__(self, name: str, content: str = "# 제목\n\n정상 본문"):
        self.name = name
        self.content = content
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return self.name

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        del max_retries, temperature, max_tokens
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})

        class Response:
            def __init__(self, content: str, model: str):
                self.content = content
                self.model = model
                self.input_tokens = 120
                self.output_tokens = 180
                self.stop_reason = "stop"

        return Response(self.content, model=f"{self.name}-model")


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
    monkeypatch.setenv("ZAI_API_KEY", "test-zai-key")

    qwen = create_client("qwen")
    deepseek = create_client("deepseek")
    zai = create_client("zai")

    assert isinstance(qwen, QwenClient)
    assert isinstance(deepseek, DeepSeekClient)
    assert qwen.provider_name == "qwen"
    assert deepseek.provider_name == "deepseek"
    assert zai.provider_name == "zai"
    assert zai.model == "glm-4.7-flash"


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
    # parser_client를 별도로 지정해 pre_writing_analysis(Step 0)와
    # sentence_polish(Step 6)가 secondary mock 출력을 소비하지 않도록 분리한다.
    parser = StaticSuccessClient("parser", "# 분석 완료")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
    )

    result = asyncio.run(generator.generate(build_job("dual-strategy")))
    assert result.quality_gate == "pass"
    assert result.llm_calls_used == 6  # pre_analysis + draft + SEO + quality + voice rewrite + image prompts
    assert result.provider_used == "qwen"
    assert result.provider_fallback_from == ""
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 4  # SEO + quality + voice rewrite + image prompts


def test_sentence_polish_prefers_stable_secondary_over_groq_parser():
    """문장 다듬기는 Groq parser보다 안정적인 secondary를 우선해야 한다."""
    primary = StaticSuccessClient("qwen")
    secondary = StaticSuccessClient("deepseek")
    parser = StaticSuccessClient("groq")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )

    selected = generator._select_sentence_polish_client()

    assert selected.provider_name == "deepseek"


def test_pre_analysis_prefers_stable_secondary_over_groq_parser():
    """사전 분석도 Groq parser보다 안정적인 secondary를 우선해야 한다."""
    primary = StaticSuccessClient("qwen")
    secondary = StaticSuccessClient("deepseek")
    parser = StaticSuccessClient("groq")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )

    selected = generator._select_pre_analysis_client()

    assert selected.provider_name == "deepseek"


def test_client_display_label_includes_model_when_available():
    """같은 provider 내 모델 fallback 로그는 모델명까지 표시해야 한다."""
    client = StaticSuccessClient("deepseek")
    client.model = "deepseek-v4-flash"  # type: ignore[attr-defined]
    generator = ContentGenerator(
        primary_client=StaticSuccessClient("qwen"),
        secondary_client=client,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )

    assert generator._client_display_label(client) == "DeepSeek(deepseek-v4-flash)"


def test_local_plain_language_polish_keeps_tables_and_softens_terms():
    """LLM 다듬기 실패 시 로컬 보정은 표를 보존하고 어려운 표현을 낮춰야 한다."""
    generator = ContentGenerator(
        primary_client=StaticSuccessClient("qwen"),
        secondary_client=StaticSuccessClient("deepseek"),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = (
        "| 지표 | 값 |\n"
        "| --- | --- |\n"
        "| ETF | 100 |\n\n"
        "외국인 수급과 ETF 흐름은 확실합니다. 하지만 환율과 금리를 같이 봐야 합니다."
    )

    polished = generator._local_plain_language_polish(content)

    assert "| ETF | 100 |" in polished
    assert "외국인 수급(외국인 투자자의 사고파는 흐름)" in polished
    assert "ETF(여러 자산을 한 바구니처럼 담은 상장 펀드)" in polished
    assert "환율(원화와 달러의 교환 비율)" in polished
    assert "금리(돈을 빌릴 때 붙는 이자율)" in polished
    assert "확실합니다" not in polished
    assert "수급(사고파는 힘의 균형)(외국인" not in polished


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
    parser = StaticSuccessClient("parser", "# 분석 완료")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
    )

    result = asyncio.run(generator.generate(build_job("fallback-case")))
    assert result.quality_gate == "pass"
    assert result.llm_calls_used == 6  # pre_analysis + draft(fallback) + SEO + quality + voice rewrite + image prompts
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
    parser = StaticSuccessClient("parser", "# 분석 완료")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
    )

    result = asyncio.run(generator.generate(build_job("fallback-rate-limit")))
    assert result.quality_gate == "pass"
    assert result.provider_used == "qwen"


def test_quality_check_revalidates_with_primary_when_secondary_is_unreliable():
    """Secondary 품질 평가가 비정형일 때 Primary로 재검증해 점수를 확정해야 한다."""
    primary = FakeLLMClient(
        "qwen",
        [
            "# 제목\n\n## 본문\n\n충분히 긴 초안 본문입니다. " * 20,
            '{"score": 85, "issues": [], "summary": "backup pass"}',
        ],
    )
    secondary = FakeLLMClient(
        "cerebras",
        [
            "평가를 시작합니다. 기준별로 검토합니다.",  # 1차 품질 평가: JSON/점수 없음
            "점수는 53점 정도로 보입니다.",  # 단순 재평가: 텍스트 점수(저신뢰)
            '{"thumbnail": {"prompt": "test"}, "content_images": []}',  # 이미지 프롬프트
        ],
    )
    parser = StaticSuccessClient("parser", "# 분석 완료")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=True,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        max_rewrites=0,
    )

    result = asyncio.run(generator.generate(build_job("quality-backup-recheck")))

    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 85
    assert any("전문 편집자의 관점" in call["user_prompt"] for call in primary.calls)
    assert result.provider_fallback_from == ""
    assert len(primary.calls) >= 2
    assert len(secondary.calls) >= 1


def test_quality_check_uses_primary_backup_when_secondary_raises():
    """Secondary 품질 평가가 예외일 때도 Primary 백업 점수로 판정해야 한다."""
    primary = FakeLLMClient(
        "qwen",
        [
            "# 제목\n\n## 본문\n\n충분히 긴 초안 본문입니다. " * 20,
            '{"score": 86, "issues": [], "summary": "backup pass on exception"}',
        ],
    )
    secondary = AlwaysRateLimitClient("cerebras")
    parser = StaticSuccessClient("parser", "# 분석 완료")

    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=True,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        max_rewrites=0,
    )

    result = asyncio.run(generator.generate(build_job("quality-backup-on-exception")))

    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 86
    assert len(primary.calls) >= 2
    assert any("전문 편집자의 관점" in call["user_prompt"] for call in primary.calls)


def test_select_quality_client_prefers_primary_when_secondary_is_cerebras():
    """secondary가 cerebras면 품질 체크는 primary를 우선 사용해야 한다."""
    primary = StaticSuccessClient("qwen", "# 제목\n\n본문")
    secondary = StaticSuccessClient("cerebras", "# 제목\n\n본문")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )

    selected = generator._select_quality_client()
    assert selected.provider_name == "qwen"


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


def test_openai_compat_does_not_retry_on_410(monkeypatch: pytest.MonkeyPatch):
    """410 Gone은 비재시도 오류로 처리되어 1회만 호출되어야 한다."""
    from modules.llm.openai_compat_client import OpenAICompatClient

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        model="deepseek-ai/deepseek-r1",
        provider="nvidia",
        timeout_sec=5.0,
    )

    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    response = httpx.Response(status_code=410, request=request)
    call_count = 0

    async def always_410(*args, **kwargs):
        del args, kwargs
        nonlocal call_count
        call_count += 1
        raise httpx.HTTPStatusError("410", request=request, response=response)

    monkeypatch.setattr(client, "generate", always_410)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            client.generate_with_retry(
                system_prompt="sys",
                user_prompt="user",
                max_retries=3,
            )
        )
    assert call_count == 1


def test_zai_flash_disables_thinking(monkeypatch: pytest.MonkeyPatch):
    """Z.AI Flash 호출은 thinking을 꺼서 지연과 토큰 사용을 줄여야 한다."""
    from modules.llm.openai_compat_client import create_zai_client

    captured_payload: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "model": "glm-4.7-flash",
                "choices": [{"message": {"content": "테스트"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            request=request,
        )

    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    client = create_zai_client(timeout_sec=5.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)  # noqa: SLF001

    try:
        asyncio.run(client.generate(system_prompt="sys", user_prompt="user", max_tokens=8))
    finally:
        asyncio.run(client.close())

    assert captured_payload["model"] == "glm-4.7-flash"
    assert captured_payload["thinking"] == {"type": "disabled"}


def test_fallback_alert_is_deduped_per_job():
    """동일 job/provider 조합 fallback 알림은 1회만 전송되어야 한다."""
    alerts: List[Dict[str, Any]] = []
    generator = ContentGenerator(
        primary_client=StaticSuccessClient("qwen"),
        secondary_client=StaticSuccessClient("gemini"),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        fallback_alert_fn=lambda payload: alerts.append(dict(payload)),
    )

    base_job_id = f"dedupe-{uuid.uuid4()}"
    generator._notify_fallback_success(
        from_provider="nvidia",
        to_provider="gemini",
        title="테스트 제목",
        job_id=base_job_id,
    )
    generator._notify_fallback_success(
        from_provider="nvidia",
        to_provider="gemini",
        title="테스트 제목",
        job_id=base_job_id,
    )
    generator._notify_fallback_success(
        from_provider="nvidia",
        to_provider="gemini",
        title="테스트 제목",
        job_id=f"{base_job_id}-second",
    )

    assert len(alerts) == 2


def test_circuit_breaker_skips_open_provider():
    """회로가 열린 프로바이더는 즉시 건너뛰고 다음 프로바이더를 사용해야 한다."""
    primary = FakeLLMClient("groq", outputs=["unused"])
    secondary = FakeLLMClient(
        "qwen",
        [
            "# 제목\n\nfallback 초안",
            "# 제목\n\nfallback SEO",
            '{"score": 88, "issues": [], "summary": "ok"}',
            "# 제목\n\nfallback SEO",
            '{"thumbnail": {"prompt": "test"}, "content_images": []}',
        ],
    )
    circuit_breaker = ProviderCircuitBreaker(job_store=None, notifier=None, fail_threshold=3, open_ttl_seconds=1800)
    circuit_breaker.record_failure("groq")
    circuit_breaker.record_failure("groq")
    circuit_breaker.record_failure("groq")
    assert circuit_breaker.is_open("groq") is True

    # parser_client를 별도 지정해 pre_writing_analysis / sentence_polish 가
    # secondary mock 출력을 소비하지 않도록 분리한다.
    parser = StaticSuccessClient("parser", "# 분석 완료")
    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        parser_client=parser,
        enable_quality_check=True,
        enable_seo_optimization=True,
        fallback_to_secondary=True,
        circuit_breaker=circuit_breaker,
    )
    result = asyncio.run(generator.generate(build_job("circuit-open-skip")))

    assert result.provider_used == "qwen"
    assert result.provider_fallback_from == "groq"
    assert len(primary.calls) == 0
    assert len(secondary.calls) == 5  # draft(fallback) + SEO + quality + voice rewrite + image prompts


def test_circuit_breaker_opens_after_rate_limit_threshold():
    """연속 429 실패 임계값 도달 후 다음 호출부터 primary를 스킵해야 한다."""
    primary = AlwaysRateLimitClient("groq")
    secondary = StaticSuccessClient("qwen")
    circuit_breaker = ProviderCircuitBreaker(job_store=None, notifier=None, fail_threshold=3, open_ttl_seconds=1800)

    generator = ContentGenerator(
        primary_client=primary,
        secondary_client=secondary,
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
        fallback_to_secondary=True,
        circuit_breaker=circuit_breaker,
    )

    for index in range(4):
        result = asyncio.run(generator.generate(build_job(f"circuit-threshold-{index}")))
        assert result.provider_used == "qwen"

    assert circuit_breaker.is_open("groq") is True
    # 첫 3회는 실패를 기록하고, 4회차부터는 호출 전 스킵되어야 한다.
    assert len(primary.calls) == 3
