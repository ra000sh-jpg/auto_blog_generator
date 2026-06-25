"""LLM Provider 팩토리."""

from __future__ import annotations

from typing import Optional

from .. import constants
from .base_client import BaseLLMClient


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
        from .qwen_client import QwenClient

        return QwenClient(model=model or "qwen-plus", timeout_sec=timeout_sec, api_key=api_key)
    if normalized == "deepseek":
        from .deepseek_client import DeepSeekClient

        return DeepSeekClient(
            model=model or constants.DEFAULT_DEEPSEEK_MODEL,
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "claude":
        from .claude_client import ClaudeClient

        return ClaudeClient(
            model=model or "claude-sonnet-4-20250514",
            timeout_sec=timeout_sec,
            max_tokens=max_tokens,
            api_key=api_key,
        )
    if normalized == "groq":
        from .openai_compat_client import create_groq_client

        return create_groq_client(
            model=model or "llama-3.3-70b-versatile",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "cerebras":
        from .openai_compat_client import create_cerebras_client

        return create_cerebras_client(
            model=model or "gpt-oss-120b",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "gemini":
        from .openai_compat_client import create_gemini_client

        return create_gemini_client(
            model=model or "gemini-2.0-flash",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "openai":
        from .openai_compat_client import create_openai_client

        return create_openai_client(
            model=model or "gpt-4.1-mini",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "nvidia":
        from .openai_compat_client import create_nvidia_client

        return create_nvidia_client(
            model=model or "meta/llama-3.3-70b-instruct",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "zai":
        from .openai_compat_client import create_zai_client

        return create_zai_client(
            model=model or "glm-4.7-flash",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "nvidia_vlm":
        from .openai_compat_client import create_nvidia_vlm_client

        return create_nvidia_vlm_client(
            model=model or constants.VLM_DEFAULT_MODEL,
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "openai_vlm":
        from .openai_compat_client import create_openai_vlm_client

        return create_openai_vlm_client(
            model=model or "gpt-4.1-mini",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "gemini_vlm":
        from .openai_compat_client import create_gemini_vlm_client

        return create_gemini_vlm_client(
            model=model or "gemini-2.5-flash-lite",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "groq_vlm":
        from .openai_compat_client import create_groq_vlm_client

        return create_groq_vlm_client(
            model=model or "meta-llama/llama-4-scout-17b-16e-instruct",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    if normalized == "qwen_vlm":
        from .openai_compat_client import create_qwen_vlm_client

        return create_qwen_vlm_client(
            model=model or "qwen-vl-plus",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    raise ValueError(f"Unknown provider: {provider}")
