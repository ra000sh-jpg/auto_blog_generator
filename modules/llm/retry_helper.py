"""LLM 클라이언트 공통 재시도 헬퍼."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


async def llm_retry(
    func: Callable[[], Awaitable[Any]],
    attempts: int,
    base_delay: float,
    logger: Any,
    provider: str,
) -> Any:
    """공통 선형 백오프 재시도 헬퍼."""
    safe_attempts = max(1, int(attempts or 1))
    safe_base_delay = max(0.0, float(base_delay or 0.0))

    for attempt in range(1, safe_attempts + 1):
        try:
            return await func()
        except Exception as exc:
            retryable = bool(getattr(exc, "llm_retryable", True))
            logger.warning(
                "LLM request failed, retrying",
                extra={
                    "provider": str(provider or ""),
                    "attempt": attempt,
                    "attempts": safe_attempts,
                    "error_type": exc.__class__.__name__,
                },
            )
            if (not retryable) or attempt >= safe_attempts:
                raise
            delay = safe_base_delay * attempt
            if delay > 0:
                await asyncio.sleep(delay)

    raise RuntimeError("unreachable retry state")
