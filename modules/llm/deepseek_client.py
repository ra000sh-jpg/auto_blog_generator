"""DeepSeek API 클라이언트."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from .. import constants
from ..exceptions import RateLimitError
from .base_client import BaseLLMClient, LLMResponse
from .retry_helper import llm_retry

logger = logging.getLogger(__name__)


class DeepSeekClient(BaseLLMClient):
    """OpenAI 호환 DeepSeek 클라이언트."""

    API_BASE = "https://api.deepseek.com/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    ):
        resolved_api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        if not resolved_api_key:
            raise ValueError("DEEPSEEK_API_KEY 환경변수가 필요합니다.")

        self.api_key = resolved_api_key
        self.model = model
        self.timeout_sec = timeout_sec
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    @property
    def provider_name(self) -> str:
        return "deepseek"

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
            f"{self.API_BASE}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})
        cached = int(usage.get("prompt_cache_hit_tokens", 0) or 0) > 0

        llm_response = LLMResponse(
            content=str(message.get("content", "")).strip(),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=str(data.get("model", self.model)),
            stop_reason=str(choice.get("finish_reason", "stop")),
            cached=cached,
        )
        logger.info(
            "DeepSeek generation complete",
            extra={
                "provider": self.provider_name,
                "model": llm_response.model,
                "input_tokens": llm_response.input_tokens,
                "output_tokens": llm_response.output_tokens,
                "cached": llm_response.cached,
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
                    raise RateLimitError("deepseek rate limited (429)") from exc
                raise

        return await llm_retry(
            func=_execute,
            attempts=attempts,
            base_delay=constants.LLM_RETRY_BASE_DELAY_SEC,
            logger=logger,
            provider=self.provider_name,
        )

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
