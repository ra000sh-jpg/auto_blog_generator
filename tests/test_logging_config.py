import io
import json
import logging

from modules.logging_config import setup_logging


def test_json_logging_format_includes_extra_fields():
    """JSON 로그에 표준/extra 필드가 포함되는지 검증한다."""
    stream = io.StringIO()
    setup_logging(level="INFO", log_format="json", stream=stream, reset=True)

    logger = logging.getLogger("tests.logging")
    logger.info("pipeline complete", extra={"job_id": "job-1", "duration_ms": 123.4})

    payload = json.loads(stream.getvalue().strip())
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tests.logging"
    assert payload["message"] == "pipeline complete"
    assert payload["extra"]["job_id"] == "job-1"
    assert payload["extra"]["duration_ms"] == 123.4
    assert "timestamp" in payload


def test_text_logging_format_is_human_readable():
    """text 로그 포맷이 사람이 읽을 수 있는 형식인지 검증한다."""
    stream = io.StringIO()
    setup_logging(level="INFO", log_format="text", stream=stream, reset=True)

    logger = logging.getLogger("tests.logging.text")
    logger.info("hello text log")

    message = stream.getvalue().strip()
    assert "INFO" in message
    assert "tests.logging.text" in message
    assert "hello text log" in message
