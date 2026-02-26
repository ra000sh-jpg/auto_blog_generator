"""LLM 프로바이더 회로 차단기."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from modules.automation.job_store import JobStore
    from modules.automation.notifier import TelegramNotifier

logger = logging.getLogger(__name__)

DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_OPEN_TTL_SECONDS = 1800
DB_KEY_PREFIX = "provider_circuit_"


class ProviderCircuitOpenError(Exception):
    """회로가 열려 있어 해당 프로바이더를 건너뛴다."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"Provider '{provider}' circuit is OPEN")
        self.llm_retryable = False


class ProviderCircuitBreaker:
    """프로바이더별 연속 실패를 추적해 임시 차단한다."""

    def __init__(
        self,
        *,
        job_store: Optional["JobStore"] = None,
        notifier: Optional["TelegramNotifier"] = None,
        fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
        open_ttl_seconds: int = DEFAULT_OPEN_TTL_SECONDS,
    ) -> None:
        self._job_store = job_store
        self._notifier = notifier
        self._fail_threshold = max(1, int(fail_threshold or DEFAULT_FAIL_THRESHOLD))
        self._open_ttl_seconds = max(60, int(open_ttl_seconds or DEFAULT_OPEN_TTL_SECONDS))
        self._fail_counts: dict[str, int] = {}
        self._open_until: dict[str, datetime] = {}

    def is_open(self, provider: str) -> bool:
        """현재 프로바이더 회로가 열려 있는지 확인한다."""
        key = str(provider or "").strip().lower()
        if not key:
            return False
        open_until = self._open_until.get(key)
        if open_until is None:
            return False
        now = datetime.now(timezone.utc)
        if now < open_until:
            return True
        self._close(key, reason="ttl_expired")
        return False

    def record_success(self, provider: str) -> None:
        """성공 호출 시 연속 실패 카운트를 초기화한다."""
        key = str(provider or "").strip().lower()
        if not key:
            return
        current_failures = int(self._fail_counts.get(key, 0))
        was_open = key in self._open_until
        if current_failures <= 0 and not was_open:
            return
        if current_failures > 0:
            logger.debug("[CircuitBreaker] %s success -> reset fail count", key)
        self._fail_counts[key] = 0
        self._open_until.pop(key, None)
        self._persist(key)

    def record_failure(self, provider: str) -> None:
        """실패 호출을 기록하고 임계치 도달 시 회로를 연다."""
        key = str(provider or "").strip().lower()
        if not key:
            return
        next_count = self._fail_counts.get(key, 0) + 1
        self._fail_counts[key] = next_count
        logger.warning(
            "[CircuitBreaker] %s failure recorded (%d/%d)",
            key,
            next_count,
            self._fail_threshold,
        )
        if next_count >= self._fail_threshold and key not in self._open_until:
            self._open_circuit(key, next_count)
        else:
            self._persist(key)

    def reset(self, provider: str) -> None:
        """수동으로 회로를 닫고 카운터를 초기화한다."""
        key = str(provider or "").strip().lower()
        if not key:
            return
        self._close(key, reason="manual_reset")

    def status_summary(self) -> dict[str, Any]:
        """회로 상태를 요약해 반환한다."""
        summary: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        for provider in set(self._fail_counts.keys()) | set(self._open_until.keys()):
            open_until = self._open_until.get(provider)
            summary[provider] = {
                "open": bool(open_until and now < open_until),
                "open_until": open_until.isoformat() if open_until else None,
                "failure_count": int(self._fail_counts.get(provider, 0)),
            }
        return summary

    def load_from_db(self, provider: str) -> None:
        """단일 프로바이더 상태를 DB에서 복원한다."""
        if not self._job_store:
            return
        key = str(provider or "").strip().lower()
        if not key:
            return
        raw = self._job_store.get_system_setting(f"{DB_KEY_PREFIX}{key}", "")
        if not raw:
            return
        try:
            data = json.loads(raw)
            failure_count = int(data.get("failure_count", 0) or 0)
            self._fail_counts[key] = max(0, failure_count)
            open_until_raw = str(data.get("open_until", "") or "").strip()
            if open_until_raw:
                parsed = datetime.fromisoformat(open_until_raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                if now < parsed:
                    self._open_until[key] = parsed
        except Exception as exc:
            logger.warning("[CircuitBreaker] failed to load %s from db: %s", key, exc)

    def load_all_from_db(self, providers: list[str]) -> None:
        """복수 프로바이더 상태를 DB에서 복원한다."""
        for provider in providers:
            self.load_from_db(provider)

    def _open_circuit(self, provider: str, failure_count: int) -> None:
        open_until = datetime.now(timezone.utc) + timedelta(seconds=self._open_ttl_seconds)
        self._open_until[provider] = open_until
        self._persist(provider)
        logger.error(
            "[CircuitBreaker] %s OPEN (%d failures, ttl=%d sec)",
            provider,
            failure_count,
            self._open_ttl_seconds,
        )
        if self._notifier and self._notifier.enabled:
            self._notifier.send_message_background(
                f"⚡ [Circuit Breaker] 프로바이더 차단\n"
                f"- 대상: {provider}\n"
                f"- 연속 실패: {failure_count}회\n"
                f"- 차단 시간: {self._open_ttl_seconds // 60}분\n"
                f"- 복구 예정: {open_until.strftime('%H:%M UTC')}"
            )

    def _close(self, provider: str, *, reason: str) -> None:
        self._open_until.pop(provider, None)
        self._fail_counts[provider] = 0
        self._persist(provider)
        logger.info("[CircuitBreaker] %s CLOSED (%s)", provider, reason)
        if self._notifier and self._notifier.enabled:
            self._notifier.send_message_background(
                f"✅ [Circuit Breaker] 프로바이더 복구\n"
                f"- 대상: {provider}\n"
                f"- 사유: {reason}"
            )

    def _persist(self, provider: str) -> None:
        """현재 상태를 DB에 저장한다."""
        if not self._job_store:
            return
        now = datetime.now(timezone.utc)
        open_until = self._open_until.get(provider)
        payload = {
            "open": bool(open_until and now < open_until),
            "open_until": open_until.isoformat() if open_until else None,
            "failure_count": int(self._fail_counts.get(provider, 0)),
        }
        try:
            self._job_store.set_system_setting(
                f"{DB_KEY_PREFIX}{provider}",
                json.dumps(payload),
            )
        except Exception as exc:
            logger.warning("[CircuitBreaker] failed to persist %s: %s", provider, exc)
