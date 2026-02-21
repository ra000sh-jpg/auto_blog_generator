"""Qwen API 클라이언트."""

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


class QwenClient(BaseLLMClient):
    """DashScope(OpenAI 호환) 기반 Qwen 클라이언트."""

    DEFAULT_API_BASE = "https://dashscope-us.aliyuncs.com/compatible-mode/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "qwen-plus",
        timeout_sec: float = 120.0,
        base_url: Optional[str] = None,
    ):
        resolved_api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if not resolved_api_key:
            raise ValueError("DASHSCOPE_API_KEY 환경변수가 필요합니다.")

        self.api_key = resolved_api_key
        self.model = model
        self.timeout_sec = timeout_sec
        resolved_base = base_url if base_url is not None else os.getenv("DASHSCOPE_BASE_URL", self.DEFAULT_API_BASE)
        self.api_base = self._normalize_api_base(resolved_base)
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    def _normalize_api_base(self, raw_base: str) -> str:
        base = raw_base.strip().rstrip("/")
        if base.endswith("/compatible-mode/v1"):
            return base
        return f"{base}/compatible-mode/v1"

    @property
    def provider_name(self) -> str:
        return "qwen"

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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
            f"{self.api_base}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        llm_response = LLMResponse(
            content=str(message.get("content", "")).strip(),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model", self.model)),
            stop_reason=str(choice.get("finish_reason", "stop")),
        )
        logger.info(
            "Qwen generation complete",
            extra={
                "provider": self.provider_name,
                "model": llm_response.model,
                "input_tokens": llm_response.input_tokens,
                "output_tokens": llm_response.output_tokens,
                "stop_reason": llm_response.stop_reason,
            },
        )
        return llm_response

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
                        raise RateLimitError("qwen rate limited (429)") from exc
                    delay = min(2**attempt, 30) + random.uniform(0.1, 0.5)
                    logger.warning("Qwen rate limited, retrying", extra={"attempt": attempt, "delay_sec": delay})
                    await asyncio.sleep(delay)
                    continue
                if attempt >= attempts:
                    raise
                delay = float(attempt)
                logger.warning(
                    "Qwen http error, retrying",
                    extra={"attempt": attempt, "delay_sec": delay, "status_code": exc.response.status_code},
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                delay = float(attempt)
                logger.warning("Qwen transient error, retrying", extra={"attempt": attempt, "delay_sec": delay})
                await asyncio.sleep(delay)

        raise last_error or RuntimeError("Qwen API failed after retries")

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
