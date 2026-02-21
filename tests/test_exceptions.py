from modules.exceptions import (
    AutoBlogError,
    ContentGenerationError,
    PublishError,
    SessionExpiredError,
)


def test_publish_error_fields():
    """PublishError의 필드가 정확히 저장되는지 검증한다."""
    error = PublishError(
        "publish failed",
        error_code="PUBLISH_FAILED",
        retryable=True,
        context={"url": "https://example.com"},
    )
    assert isinstance(error, AutoBlogError)
    assert error.error_code == "PUBLISH_FAILED"
    assert error.retryable is True
    assert error.context["url"] == "https://example.com"


def test_session_expired_error_defaults():
    """SessionExpiredError 기본값을 검증한다."""
    error = SessionExpiredError()
    assert error.error_code == "AUTH_EXPIRED"
    assert error.retryable is False
    assert "세션 만료" in str(error)


def test_content_generation_error_type():
    """ContentGenerationError 타입 계층을 검증한다."""
    error = ContentGenerationError("generation failed")
    assert isinstance(error, AutoBlogError)
    assert str(error) == "generation failed"
