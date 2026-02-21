"""공통 로깅 설정 모듈."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TextIO


_RESERVED_LOG_RECORD_KEYS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """구조화 로그(JSON) 포맷터."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        log_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "extra": self._extract_extra(record),
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)

    def _extract_extra(self, record: logging.LogRecord) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_KEYS or key.startswith("_"):
                continue
            extra[key] = value
        return extra


def setup_logging(
    level: Optional[str] = None,
    log_format: Optional[str] = None,
    stream: Optional[TextIO] = None,
    reset: bool = False,
) -> None:
    """애플리케이션 공통 로깅을 설정한다."""
    target_level = str(level or os.getenv("LOG_LEVEL") or "INFO").upper()
    target_format = str(log_format or os.getenv("LOG_FORMAT") or "text").lower()

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, target_level, logging.INFO))

    if reset:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

    if not root_logger.handlers:
        console_handler = logging.StreamHandler(stream or sys.stdout)
        if target_format == "json":
            console_handler.setFormatter(JsonFormatter())
        else:
            console_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        root_logger.addHandler(console_handler)

    json_log_file = os.getenv("LOG_JSON_FILE")
    if json_log_file:
        # 동일 파일 핸들러의 중복 생성을 방지한다.
        has_same_handler = any(
            isinstance(handler, logging.FileHandler)
            and getattr(handler, "baseFilename", "").endswith(json_log_file)
            for handler in root_logger.handlers
        )
        if not has_same_handler:
            file_handler = logging.FileHandler(json_log_file, encoding="utf-8")
            file_handler.setFormatter(JsonFormatter())
            root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """공통 설정이 적용된 로거를 반환한다."""
    return logging.getLogger(name)
