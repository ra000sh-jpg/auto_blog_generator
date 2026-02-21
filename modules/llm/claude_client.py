"""Claude API 클라이언트."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Optional

from .base_client import BaseLLMClient, LLMResponse

logger = logging.getLogger(__name__)


class ClaudeClient(BaseLLMClient):
    """Anthropic Claude API 래퍼 클라이언트."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        timeout_sec: float = 120.0,
    ):
        resolved_api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not resolved_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")

        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("anthropic package is required. pip install anthropic") from exc

        env_model = os.getenv("CLAUDE_MODEL")
        env_max_tokens = os.getenv("CLAUDE_MAX_TOKENS")

        self.api_key = resolved_api_key
        self.model = env_model or model
        self.max_tokens = int(env_max_tokens) if env_max_tokens else max_tokens
        self.timeout_sec = timeout_sec
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=resolved_api_key, timeout=timeout_sec)

    @property
    def provider_name(self) -> str:
        return "claude"

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """단일 메시지 생성."""
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ),
        )

        llm_response = self._to_llm_response(response)
        logger.info(
            "Claude generation complete",
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
        """지수 백오프를 적용한 생성."""
        attempts = max(1, max_retries)
        for attempt in range(1, attempts + 1):
            try:
                return await self.generate(
                    system_prompt,
                    user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                retryable = self._is_retryable_error(exc)
                if (not retryable) or attempt >= attempts:
                    raise

                delay = min(2 ** (attempt - 1), 30) + random.uniform(0.0, 0.5)
                logger.warning(
                    "Claude retry",
                    extra={
                        "attempt": attempt,
                        "max_retries": attempts,
                        "delay_sec": round(delay, 3),
                        "error_type": exc.__class__.__name__,
                    },
                )
                await asyncio.sleep(delay)

        raise RuntimeError("unreachable retry state")

    def _to_llm_response(self, response: Any) -> LLMResponse:
        content_parts = getattr(response, "content", []) or []
        text_chunks = []
        for block in content_parts:
            block_text = getattr(block, "text", "")
            if block_text:
                text_chunks.append(block_text)
        content = "\n".join(text_chunks).strip()

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        model = str(getattr(response, "model", self.model))
        stop_reason = str(getattr(response, "stop_reason", "unknown"))

        return LLMResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            stop_reason=stop_reason,
        )

    def _is_retryable_error(self, error: Exception) -> bool:
        retryable_names = {
            "RateLimitError",
            "APIError",
            "APIConnectionError",
            "APITimeoutError",
            "InternalServerError",
        }
        if error.__class__.__name__ in retryable_names:
            return True

        error_text = str(error).lower()
        return any(
            marker in error_text
            for marker in ("rate limit", "timeout", "connection", "temporarily unavailable")
        )
