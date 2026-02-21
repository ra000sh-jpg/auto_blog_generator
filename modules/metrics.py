"""간단한 메트릭 저장/조회 모듈."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Optional

from .automation.time_utils import now_utc


class MetricsStore:
    """SQLite 기반 메트릭 저장소."""

    def __init__(self, db_path: str = "data/automation.db"):
        self.db_path = db_path
        self._ensure_directory()
        self._init_tables()

    def _ensure_directory(self) -> None:
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    labels TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_metrics_name_time
                ON metrics_events(metric_name, created_at)
                """
            )

    def record_jobs_total(self, status: str, amount: float = 1.0) -> None:
        self._record("jobs_total", amount, labels={"status": status})

    def record_publish_duration_seconds(self, seconds: float) -> None:
        self._record("publish_duration_seconds", seconds)

    def record_llm_calls_total(self, calls: int) -> None:
        self._record("llm_calls_total", float(calls))

    def record_errors_total(self, error_code: str, amount: float = 1.0) -> None:
        self._record("errors_total", amount, labels={"error_code": error_code})

    def _record(self, metric_name: str, value: float, labels: Optional[Dict[str, Any]] = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO metrics_events (metric_name, metric_value, labels, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (metric_name, value, json.dumps(labels or {}), now_utc()),
            )

    def get_summary(self) -> Dict[str, Any]:
        """핵심 메트릭 요약을 반환한다."""
        summary: Dict[str, Any] = {
            "jobs_total": {},
            "publish_duration_seconds": {
                "count": 0,
                "avg": 0.0,
                "max": 0.0,
            },
            "llm_calls_total": 0,
            "errors_total": {},
        }

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT metric_name, metric_value, labels
                FROM metrics_events
                ORDER BY created_at DESC
                """
            ).fetchall()

        publish_values = []
        for row in rows:
            name = row["metric_name"]
            value = float(row["metric_value"])
            labels = json.loads(row["labels"] or "{}")
            if name == "jobs_total":
                status = labels.get("status", "unknown")
                summary["jobs_total"][status] = summary["jobs_total"].get(status, 0.0) + value
            elif name == "publish_duration_seconds":
                publish_values.append(value)
            elif name == "llm_calls_total":
                summary["llm_calls_total"] += value
            elif name == "errors_total":
                error_code = labels.get("error_code", "UNKNOWN")
                summary["errors_total"][error_code] = summary["errors_total"].get(error_code, 0.0) + value

        if publish_values:
            summary["publish_duration_seconds"] = {
                "count": len(publish_values),
                "avg": sum(publish_values) / len(publish_values),
                "max": max(publish_values),
            }

        return summary
