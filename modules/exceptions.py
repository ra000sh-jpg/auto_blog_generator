"""자동 블로그 시스템 공통 예외 정의."""

from __future__ import annotations

from typing import Any, Dict, Optional


class AutoBlogError(Exception):
    """auto_blog_generator 기본 예외."""


class PublishError(AutoBlogError):
    """발행 관련 예외."""

    def __init__(
        self,
        message: str,
        error_code: str,
        retryable: bool = True,
        context: Optional[Dict[str, Any]] = None,
    ):
        self.error_code = error_code
        self.retryable = retryable
        self.context = context or {}
        super().__init__(message)


class ContentGenerationError(AutoBlogError):
    """콘텐츠 생성 예외."""


class RateLimitError(AutoBlogError):
    """LLM 프로바이더 요청 한도 초과 예외."""


class SessionExpiredError(PublishError):
    """세션 만료 예외."""

    def __init__(self, message: str = "세션 만료", context: Optional[Dict[str, Any]] = None):
        super().__init__(message, "AUTH_EXPIRED", retryable=False, context=context)
