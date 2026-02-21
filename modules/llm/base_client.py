"""LLM Provider 공통 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str
    cached: bool = False


class BaseLLMClient(ABC):
    """모든 LLM Provider가 구현해야 하는 인터페이스."""

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """단일 메시지를 생성한다."""

    @abstractmethod
    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """재시도 로직과 함께 메시지를 생성한다."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider 이름을 반환한다."""
