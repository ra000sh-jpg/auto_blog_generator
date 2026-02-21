"""OpenAI 호환 범용 LLM 클라이언트.

Groq, Cerebras, Gemini Flash 등 OpenAI Chat Completions API를 지원하는
서비스에 재사용한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Optional

import httpx

from ..exceptions import RateLimitError
from .base_client import BaseLLMClient, LLMResponse

logger = logging.getLogger(__name__)


class OpenAICompatClient(BaseLLMClient):
    """OpenAI Chat Completions 호환 클라이언트."""

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        provider: str,
        api_key: Optional[str] = None,
        timeout_sec: float = 60.0,
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

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        result = LLMResponse(
            content=str(message.get("content", "")).strip(),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model", self.model)),
            stop_reason=str(choice.get("finish_reason", "stop")),
        )
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

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        attempts = max(1, max_retries)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                return await self.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code == 429:
                    if attempt >= attempts:
                        raise RateLimitError(f"{self._provider} rate limited (429)") from exc
                    delay = min(2**attempt, 30) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "%s rate limited, retrying",
                        self._provider,
                        extra={"attempt": attempt, "delay_sec": delay},
                    )
                    await asyncio.sleep(delay)
                    continue
                if attempt >= attempts:
                    raise
                delay = min(2**attempt, 30) + random.uniform(0.1, 0.5)
                logger.warning(
                    "%s transient http error, retrying",
                    self._provider,
                    extra={"attempt": attempt, "delay_sec": delay, "status_code": exc.response.status_code},
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                delay = float(attempt)
                logger.warning(
                    "%s transient error, retrying",
                    self._provider,
                    extra={"attempt": attempt, "delay_sec": delay},
                )
                await asyncio.sleep(delay)

        raise last_error or RuntimeError(f"{self._provider} API failed after retries")

    async def close(self) -> None:
        await self._client.aclose()


# ── 사전 정의 팩토리 함수 ──────────────────────────────

def create_groq_client(
    model: str = "llama-3.3-70b-versatile",
    timeout_sec: float = 60.0,
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
    model: str = "llama3.3-70b",
    timeout_sec: float = 60.0,
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
    timeout_sec: float = 60.0,
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
    timeout_sec: float = 60.0,
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
