from __future__ import annotations

import json
from pathlib import Path
import os

from fastapi.testclient import TestClient

from server.main import app


def _write_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_ai_toggle_report_returns_unavailable_when_missing(monkeypatch, tmp_path):
    """리포트 파일이 없으면 available=false를 반환해야 한다."""
    monkeypatch.setenv("NAVER_AI_TOGGLE_REPORT_DIR", str(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/ai-toggle/report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["expected_on"] == 0
    assert payload["post_verify_passed"] == 0


def test_ai_toggle_report_returns_summary_and_failure_streak(monkeypatch, tmp_path):
    """리포트가 있으면 요약값과 연속 실패 횟수를 계산해 반환해야 한다."""
    report_dir = tmp_path
    monkeypatch.setenv("NAVER_AI_TOGGLE_REPORT_DIR", str(report_dir))

    latest = {
        "mode": "metadata",
        "post_url": "https://blog.naver.com/PostView.naver?logNo=123",
        "expected_on": 3,
        "actual_on": 2,
        "post_verify_passed": 2,
        "created_at": 1771673832,
        "rows": [
            {
                "image_path": "data/images/a.png",
                "expected_on": True,
                "actual_on": False,
                "post_verify_on": False,
            },
            {
                "image_path": "data/images/b.png",
                "expected_on": True,
                "actual_on": True,
                "post_verify_on": True,
            },
        ],
        "summary": {
            "prepublish": {"expected_on": 3, "verified_on": 2, "repaired": 0, "failed": 1},
            "postverify": {"expected_on": 3, "passed": 2, "failed": 1},
        },
    }
    _write_report(report_dir / "last_report.json", latest)

    # 최근 히스토리 2건 연속 실패 + 그 이전 성공 1건
    history_fail_new = {
        "summary": {
            "prepublish": {"failed": 0},
            "postverify": {"failed": 1},
        }
    }
    history_fail_old = {
        "summary": {
            "prepublish": {"failed": 1},
            "postverify": {"failed": 0},
        }
    }
    history_pass = {
        "summary": {
            "prepublish": {"failed": 0},
            "postverify": {"failed": 0},
        }
    }
    _write_report(report_dir / "report_1003.json", history_fail_new)
    _write_report(report_dir / "report_1002.json", history_fail_old)
    _write_report(report_dir / "report_1001.json", history_pass)

    # 최신순 정렬을 위해 mtime을 명시한다.
    os.utime(report_dir / "report_1001.json", (1001, 1001))
    os.utime(report_dir / "report_1002.json", (1002, 1002))
    os.utime(report_dir / "report_1003.json", (1003, 1003))

    with TestClient(app) as client:
        response = client.get("/api/ai-toggle/report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["mode"] == "metadata"
    assert payload["expected_on"] == 3
    assert payload["actual_on"] == 2
    assert payload["post_verify_passed"] == 2
    assert payload["prepublish"]["failed"] == 1
    assert payload["postverify"]["failed"] == 1
    assert payload["recent_failure_streak"] == 2
    assert payload["unresolved_images"] == ["data/images/a.png"]
