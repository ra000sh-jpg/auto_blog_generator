"""시스템 헬스체크 스크립트."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.logging_config import setup_logging


def _check_database(db_path: Path) -> Dict[str, Any]:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        pending_row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM jobs
            WHERE status IN ('queued', 'retry_wait')
            """
        ).fetchone()
        running_row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'"
        ).fetchone()
        conn.close()
        return {
            "status": "ok",
            "pending_jobs": int(pending_row["count"] if pending_row else 0),
            "running_jobs": int(running_row["count"] if running_row else 0),
        }
    except Exception as exc:
        return {"status": "fail", "error": str(exc)}


def _check_session(session_file: Path) -> Dict[str, Any]:
    if not session_file.exists():
        return {"status": "fail", "reason": "session_file_missing"}

    try:
        with session_file.open("r", encoding="utf-8") as file:
            session_data = json.load(file)
    except Exception as exc:
        return {"status": "fail", "reason": "session_file_invalid", "error": str(exc)}

    cookies = session_data.get("cookies", [])
    if not isinstance(cookies, list) or not cookies:
        return {"status": "degraded", "reason": "cookies_missing"}

    now_epoch = datetime.now(timezone.utc).timestamp()
    expires = [
        float(cookie.get("expires", 0))
        for cookie in cookies
        if isinstance(cookie, dict) and cookie.get("expires")
    ]
    if not expires:
        return {"status": "degraded", "reason": "cookie_expiry_missing"}

    max_expiry = max(expires)
    expires_in_hours = (max_expiry - now_epoch) / 3600
    if expires_in_hours <= 0:
        return {"status": "fail", "reason": "session_expired", "expires_in_hours": 0}

    return {
        "status": "ok",
        "expires_in_hours": round(expires_in_hours, 2),
    }


def _check_success_rate(db_path: Path, sample_size: int = 10) -> Dict[str, Any]:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT status
            FROM jobs
            WHERE status IN ('completed', 'failed')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (sample_size,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        return {"status": "fail", "error": str(exc), "rate": 0.0, "sample_size": 0}

    if not rows:
        return {"status": "degraded", "rate": 0.0, "sample_size": 0}

    completed_count = sum(1 for row in rows if row["status"] == "completed")
    total_count = len(rows)
    success_rate = completed_count / total_count
    status = "ok" if success_rate >= 0.7 else "degraded"
    return {
        "status": status,
        "rate": round(success_rate, 3),
        "sample_size": total_count,
    }


def run_healthcheck(db_path: str, session_file: str) -> Dict[str, Any]:
    """헬스체크를 실행하고 결과를 반환한다."""
    database_check = _check_database(Path(db_path))
    session_check = _check_session(Path(session_file))
    success_rate_check = _check_success_rate(Path(db_path))

    statuses = [
        database_check.get("status", "fail"),
        session_check.get("status", "fail"),
        success_rate_check.get("status", "fail"),
    ]

    if "fail" in statuses:
        overall_status = "unhealthy"
    elif "degraded" in statuses:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return {
        "status": overall_status,
        "checks": {
            "database": database_check,
            "session": session_check,
            "success_rate": success_rate_check,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="auto_blog_generator 헬스체크")
    parser.add_argument("--db", default="data/automation.db")
    parser.add_argument("--session-file", default="data/sessions/naver/state.json")
    parser.add_argument("--indent", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()
    result = run_healthcheck(args.db, args.session_file)
    print(json.dumps(result, ensure_ascii=False, indent=args.indent))


if __name__ == "__main__":
    main()
