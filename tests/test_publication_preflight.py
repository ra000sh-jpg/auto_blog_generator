from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.automation.publication_preflight import (
    check_naver_session_state,
    run_publication_preflight,
    scan_blocked_publication_phrases,
)


@dataclass
class DummyJob:
    job_id: str = "job-preflight"
    title: str = "국장 전 브리핑"
    platform: str = "naver"
    tags: list[str] = field(default_factory=list)


class DummyPublisher:
    pass


class SessionAwarePublisher:
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir


def _sufficient_source_pack() -> dict[str, Any]:
    return {
        "schema_version": "source_pack.v1",
        "scope": "kr",
        "sources": [
            {"source": "FRED", "source_type": "official", "title": "US10Y", "metric_key": "US10Y", "value": 4.2},
            {"source": "Stooq", "source_type": "market_data", "title": "KOSPI", "metric_key": "KOSPI", "value": 2870.0},
            {"source": "CoinGecko", "source_type": "market_data", "title": "BTC", "metric_key": "BTC", "value": 104000.0},
            {"source": "Binance", "source_type": "market_data", "title": "ETH", "metric_key": "ETH", "value": 2500.0},
        ],
        "confirmed_metrics": [
            {"key": "US10Y", "label": "DGS10", "value": 4.2, "source": "FRED"},
            {"key": "KOSPI", "label": "KOSPI", "value": 2870.0, "source": "Stooq"},
            {"key": "BTC", "label": "bitcoin", "value": 104000.0, "source": "CoinGecko"},
        ],
        "missing_sources": [],
    }


def test_missing_naver_session_blocks_before_publish(tmp_path: Path):
    issues = check_naver_session_state(tmp_path / "state.json")

    assert len(issues) == 1
    assert issues[0].code == "AUTH_EXPIRED"
    assert issues[0].blocking is True


def test_valid_naver_session_passes_cookie_preflight(tmp_path: Path):
    session_file = tmp_path / "state.json"
    session_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "NID_AUT",
                        "value": "token",
                        "domain": ".naver.com",
                        "expires": -1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    issues = check_naver_session_state(
        session_file,
        now=datetime.now(timezone.utc),
        env={"AUTOBLOG_NAVER_SESSION_WARN_AGE_DAYS": "99"},
    )

    assert [issue for issue in issues if issue.blocking] == []


def test_blocked_publication_phrase_is_detected():
    issues = scan_blocked_publication_phrases(
        title="시장 브리핑",
        content="구체적 수치는 확인하지 못했습니다. 그래서 방향성만 봅니다.",
    )

    assert any(issue.code == "BLOCKED_PUBLICATION_PHRASE" for issue in issues)


def test_preflight_blocks_when_session_file_missing(tmp_path: Path):
    result = run_publication_preflight(
        job=DummyJob(tags=[]),
        payload={
            "title": "테스트",
            "content": "본문입니다.",
            "source_pack": _sufficient_source_pack(),
        },
        publisher=SessionAwarePublisher(tmp_path / "missing-session"),
        publish_mode="draft",
    )

    assert result.ok is False
    assert result.primary_error_code == "AUTH_EXPIRED"


def test_source_pack_blocks_public_market_publish_when_insufficient():
    result = run_publication_preflight(
        job=DummyJob(tags=["market_daily", "market_slot:kr_preopen"]),
        payload={
            "title": "국장 전 브리핑",
            "content": "시장 기준을 확인합니다.",
            "seo_snapshot": {
                "market_snapshot": {
                    "scope": "kr",
                    "data_points": [
                        {"symbol": "KOSPI", "source": "Stooq", "value": 2870.0},
                    ],
                }
            },
        },
        publisher=DummyPublisher(),
        publish_mode="publish",
    )

    assert result.ok is False
    assert result.primary_error_code == "QUALITY_REJECTED"
    assert any(issue.code == "SOURCE_PACK_INSUFFICIENT" for issue in result.issues)


def test_source_pack_blocks_draft_market_publish_by_default_when_insufficient():
    result = run_publication_preflight(
        job=DummyJob(tags=["market_daily", "market_slot:kr_preopen"]),
        payload={
            "title": "국장 전 브리핑",
            "content": "시장 기준을 확인합니다.",
            "seo_snapshot": {
                "market_snapshot": {
                    "scope": "kr",
                    "data_points": [
                        {"symbol": "KOSPI", "source": "Stooq", "value": 2870.0},
                    ],
                }
            },
        },
        publisher=DummyPublisher(),
        publish_mode="draft",
    )

    assert result.ok is False
    assert any(issue.code == "SOURCE_PACK_INSUFFICIENT" for issue in result.issues)


def test_claim_ledger_blocks_unsupported_numeric_claim():
    result = run_publication_preflight(
        job=DummyJob(tags=["market_daily", "market_slot:kr_preopen"]),
        payload={
            "title": "국장 전 브리핑",
            "content": "FRED 기준 US10Y는 4.2입니다. 하지만 KOSPI는 3000을 확정 돌파합니다.",
            "source_pack": _sufficient_source_pack(),
        },
        publisher=DummyPublisher(),
        publish_mode="draft",
    )

    assert result.ok is False
    assert any(issue.code == "UNSUPPORTED_CLAIM" for issue in result.issues)
    assert result.claim_ledger["unsupported_claim_count"] == 1


def test_source_pack_warns_for_draft_market_publish_when_insufficient():
    result = run_publication_preflight(
        job=DummyJob(tags=["market_daily", "market_slot:kr_preopen"]),
        payload={
            "title": "국장 전 브리핑",
            "content": "시장 기준을 확인합니다.",
            "seo_snapshot": {
                "market_snapshot": {
                    "scope": "kr",
                    "data_points": [
                        {"symbol": "KOSPI", "source": "Stooq", "value": 2870.0},
                    ],
                }
            },
        },
        publisher=DummyPublisher(),
        publish_mode="draft",
        env={"AUTOBLOG_SOURCE_PACK_REQUIRED_FOR_DRAFT": "0"},
    )

    assert result.ok is True
    assert any(issue.code == "SOURCE_PACK_WARNING" for issue in result.issues)


def test_directional_title_gate_warns_for_numeric_centered_market_title():
    result = run_publication_preflight(
        job=DummyJob(tags=["market_daily", "market_slot:kr_preopen"]),
        payload={
            "title": "국장 개장 전 브리핑 - 금리와 환율이 남긴 기준",
            "content": "시장 기준을 확인합니다.",
            "source_pack": _sufficient_source_pack(),
        },
        publisher=DummyPublisher(),
        publish_mode="draft",
    )

    assert result.ok is True
    assert any(issue.code == "DIRECTIONAL_TITLE_WEAK" and issue.severity == "warning" for issue in result.issues)
