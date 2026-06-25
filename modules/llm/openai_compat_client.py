"""OpenAI 호환 범용 LLM 클라이언트.

Groq, Cerebras, Gemini Flash 등 OpenAI Chat Completions API를 지원하는
서비스에 재사용한다.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx

from .. import constants
from ..exceptions import RateLimitError
from .base_client import BaseLLMClient, LLMResponse
from .retry_helper import llm_retry

logger = logging.getLogger(__name__)

# DeepSeek R1, QwQ 등 reasoning 모델이 출력하는 <think>...</think> 태그 패턴
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# 모델 이름에 이 문자열 중 하나가 포함되면 thinking tag stripping 적용
_REASONING_MODEL_KEYWORDS = ("deepseek-r1", "deepseek-reasoner", "qwq", "-r1-")


def _is_reasoning_model(model_name: str) -> bool:
    """reasoning 모델 여부를 판단한다."""
    lowered = model_name.lower()
    return any(kw in lowered for kw in _REASONING_MODEL_KEYWORDS)


def _strip_thinking_tags(text: str) -> str:
    """reasoning 모델의 <think>...</think> 블록을 제거하고 본문만 반환한다."""
    return _THINK_TAG_RE.sub("", text).strip()


class OpenAICompatClient(BaseLLMClient):
    """OpenAI Chat Completions 호환 클라이언트."""
    NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 410, 422})

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        provider: str,
        api_key: Optional[str] = None,
        timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    ):
        resolved_key = api_key or os.getenv(api_key_env, "")
        if not resolved_key:
            raise ValueError(f"{api_key_env} 환경변수가 필요합니다.")

        self._provider = provider
        self.model = model
        self.timeout_sec = timeout_sec
        self._base_url = base_url.rstrip("/")
        self._api_key = resolved_key
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    @property
    def provider_name(self) -> str:
        return self._provider

    def _parse_chat_completion(self, data: dict[str, Any]) -> LLMResponse:
        """Chat Completions 응답을 공통 파싱한다."""
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})
        raw_content = str(message.get("content", "")).strip()
        if _is_reasoning_model(self.model):
            raw_content = _strip_thinking_tags(raw_content)
        return LLMResponse(
            content=raw_content,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model", self.model)),
            stop_reason=str(choice.get("finish_reason", "stop")),
        )

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._provider == "zai" and "flash" in self.model.lower():
            # Flash 무료 fallback은 짧은 자동화 작업이 많으므로 reasoning을 꺼서 지연과 토큰 사용을 줄인다.
            payload["thinking"] = {"type": "disabled"}

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        result = self._parse_chat_completion(data)
        logger.info(
            "%s generation complete",
            self._provider,
            extra={
                "provider": self._provider,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
        return result

    async def generate_vision(
        self,
        text_prompt: str,
        image_base64: str,
        image_media_type: str = "image/png",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """이미지 + 텍스트 입력 기반 Vision 응답을 생성한다."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image_media_type};base64,{image_base64}"},
                        },
                        {"type": "text", "text": text_prompt},
                    ],
                }
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        result = self._parse_chat_completion(data)
        logger.info(
            "%s vision generation complete",
            self._provider,
            extra={
                "provider": self._provider,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
        return result

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        retry_base_delay_sec: Optional[float] = None,
        retry_max_delay_sec: Optional[float] = None,
    ) -> LLMResponse:
        attempts = max(1, max_retries)
        current_attempt = 0

        async def _execute() -> LLMResponse:
            nonlocal current_attempt
            current_attempt += 1
            try:
                return await self.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and current_attempt >= attempts:
                    raise RateLimitError(f"{self._provider} rate limited (429)") from exc
                if exc.response.status_code in self.NON_RETRYABLE_STATUS_CODES:
                    # 모델 폐기(410), 엔드포인트/모델 미지원(404) 등은 즉시 폴백한다.
                    setattr(exc, "llm_retryable", False)
                raise

        return await llm_retry(
            func=_execute,
            attempts=attempts,
            base_delay=(
                float(retry_base_delay_sec)
                if retry_base_delay_sec is not None
                else constants.LLM_RETRY_BASE_DELAY_SEC
            ),
            max_delay=retry_max_delay_sec,
            logger=logger,
            provider=self._provider,
        )

    async def generate_vision_with_retry(
        self,
        text_prompt: str,
        image_base64: str,
        image_media_type: str = "image/png",
        max_retries: int = 3,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        retry_base_delay_sec: Optional[float] = None,
        retry_max_delay_sec: Optional[float] = None,
    ) -> LLMResponse:
        """Vision 호출을 retry 정책과 함께 실행한다."""
        attempts = max(1, max_retries)
        current_attempt = 0

        async def _execute() -> LLMResponse:
            nonlocal current_attempt
            current_attempt += 1
            try:
                return await self.generate_vision(
                    text_prompt=text_prompt,
                    image_base64=image_base64,
                    image_media_type=image_media_type,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and current_attempt >= attempts:
                    raise RateLimitError(f"{self._provider} rate limited (429)") from exc
                if exc.response.status_code in self.NON_RETRYABLE_STATUS_CODES:
                    setattr(exc, "llm_retryable", False)
                raise

        return await llm_retry(
            func=_execute,
            attempts=attempts,
            base_delay=(
                float(retry_base_delay_sec)
                if retry_base_delay_sec is not None
                else constants.LLM_RETRY_BASE_DELAY_SEC
            ),
            max_delay=retry_max_delay_sec,
            logger=logger,
            provider=self._provider,
        )

    async def close(self) -> None:
        await self._client.aclose()


# ── 사전 정의 팩토리 함수 ──────────────────────────────

def create_groq_client(
    model: str = "llama-3.3-70b-versatile",
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        model=model,
        provider="groq",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_cerebras_client(
    model: str = "gpt-oss-120b",
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        model=model,
        provider="cerebras",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_gemini_client(
    model: str = "gemini-2.0-flash",
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        model=model,
        provider="gemini",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_openai_client(
    model: str = "gpt-4.1-mini",
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        model=model,
        provider="openai",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_nvidia_client(
    model: str = "meta/llama-3.3-70b-instruct",
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        model=model,
        provider="nvidia",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_zai_client(
    model: str = "glm-4.7-flash",
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        model=model,
        provider="zai",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_nvidia_vlm_client(
    model: str = constants.VLM_DEFAULT_MODEL,
    timeout_sec: float = constants.VLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        model=model,
        provider="nvidia_vlm",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_openai_vlm_client(
    model: str = "gpt-4.1-mini",
    timeout_sec: float = constants.VLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        model=model,
        provider="openai_vlm",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_gemini_vlm_client(
    model: str = "gemini-2.5-flash-lite",
    timeout_sec: float = constants.VLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        model=model,
        provider="gemini_vlm",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_groq_vlm_client(
    model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
    timeout_sec: float = constants.VLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        model=model,
        provider="groq_vlm",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def create_qwen_vlm_client(
    model: str = "qwen-vl-plus",
    timeout_sec: float = constants.VLM_REQUEST_TIMEOUT_SEC,
    api_key: Optional[str] = None,
) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        model=model,
        provider="qwen_vlm",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )
