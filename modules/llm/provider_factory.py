"""LLM Provider 팩토리."""

from __future__ import annotations

from typing import Optional

from .. import constants
from .base_client import BaseLLMClient
from .claude_client import ClaudeClient
from .deepseek_client import DeepSeekClient
from .openai_compat_client import (
    create_cerebras_client,
    create_gemini_client,
    create_groq_client,
    create_openai_client,
)
from .qwen_client import QwenClient


def create_client(
    provider: str,
    model: Optional[str] = None,
    timeout_sec: float = constants.LLM_REQUEST_TIMEOUT_SEC,
    max_tokens: int = 4096,
    api_key: Optional[str] = None,
) -> BaseLLMClient:
    """Provider 이름으로 클라이언트를 생성한다."""
    normalized = provider.strip().lower()

    if normalized == "qwen":
        return QwenClient(model=model or "qwen-plus", timeout_sec=timeout_sec, api_key=api_key)
    if normalized == "deepseek":
        return DeepSeekClient(model=model or "deepseek-chat", timeout_sec=timeout_sec, api_key=api_key)
    if normalized == "claude":
        return ClaudeClient(
            model=model or "claude-sonnet-4-20250514",
            timeout_sec=timeout_sec,
            max_tokens=max_tokens,
            api_key=api_key,
        )
    if normalized == "groq":
        return create_groq_client(
            model=model or "llama-3.3-70b-versatile",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "cerebras":
        return create_cerebras_client(
            model=model or "llama3.1-8b",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "gemini":
        return create_gemini_client(
            model=model or "gemini-2.0-flash",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "openai":
        return create_openai_client(
            model=model or "gpt-4.1-mini",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    raise ValueError(f"Unknown provider: {provider}")
