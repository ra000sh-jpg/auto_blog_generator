"""Phase 22 Idea Vault Auto Collector — E2E 테스트.

Track A (RSS 자동 수집) 와 Track B (Telegram Webhook + 오프라인 폴백) 를 검증한다.

테스트 시나리오:
1. IdeaVaultAutoCollector.run_once() — RSS 모킹 → vault 적재 확인
2. IdeaVaultAutoCollector 중복 URL 차단 — 같은 URL 두 번 실행 시 1건만 저장
3. TelegramWebhook POST — 올바른 시크릿 헤더 → vault 적재 + 200 응답
4. TelegramWebhook POST — 잘못된 시크릿 → 403
5. TelegramWebhook POST — 빈 텍스트 → stored=False
6. collect_pending_updates() — getUpdates 모킹 → vault 적재 + last_id 갱신 확인
7. collect_pending_updates() — 봇 토큰 없음 → graceful skip (0 반환)
"""

from __future__ import annotations

import asyncio
import json as json_mod
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

import pytest

# FastAPI app 을 모듈 레벨에서 한 번만 import (naver_connect.py asyncio.Lock 문제 회피)
# 이 import 는 반드시 첫 번째 asyncio.run() 이전에 실행되어야 한다.
_event_loop_bootstrap = asyncio.new_event_loop()
asyncio.set_event_loop(_event_loop_bootstrap)

from server.main import app  # noqa: E402 — bootstrap 이후 import 필수
from server.dependencies import get_job_store  # noqa: E402

from modules.automation.job_store import JobConfig, JobStore


# ─────────────────────────────────────────────────────────────────────────────
# 공통 헬퍼
# ─────────────────────────────────────────────────────────────────────────────


def build_store(tmp_path: Path, name: str = "vault_auto_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _make_vault_parser_mock(accepted_items: List[Any]) -> MagicMock:
    """IdeaVaultBatchParser.parse_bulk 응답을 모킹한다."""
    mock_result = MagicMock()
    mock_result.accepted_items = accepted_items
    mock_result.parser_used = "gemini_flash"

    mock_parser = MagicMock()
    mock_parser.parse_bulk = AsyncMock(return_value=mock_result)
    return mock_parser


def _make_accepted_item(raw_text: str, category: str = "IT 기술", topic_mode: str = "it") -> MagicMock:
    item = MagicMock()
    item.raw_text = raw_text
    item.mapped_category = category
    item.topic_mode = topic_mode
    return item


# ─────────────────────────────────────────────────────────────────────────────
# 1. Track A — IdeaVaultAutoCollector: RSS 수집 → vault 적재
# ─────────────────────────────────────────────────────────────────────────────


def test_idea_vault_auto_collector_rss_to_vault(tmp_path: Path):
    """RSS 3건 수집 → 파서 통과 → vault 에 3건 저장되어야 한다."""
    store = build_store(tmp_path)
    store.set_system_setting("custom_categories", json_mod.dumps(["IT 기술", "경제"]))

    # RSS 수집 모킹
    fake_articles = [
        {"title": "AI 혁신 뉴스", "link": "https://news/1", "content": "인공지능이 산업을 바꾸고 있다."},
        {"title": "경제 성장 전망", "link": "https://news/2", "content": "내년 경제 성장률이 2%로 예측된다."},
        {"title": "클라우드 컴퓨팅 동향", "link": "https://news/3", "content": "클라우드 시장이 급성장하고 있다."},
    ]

    accepted = [
        _make_accepted_item("AI 혁신 뉴스 — 인공지능이 산업을 바꾸고 있다.", "IT 기술", "it"),
        _make_accepted_item("경제 성장 전망 — 내년 경제 성장률이 2%로 예측된다.", "경제", "economy"),
        _make_accepted_item("클라우드 컴퓨팅 동향 — 클라우드 시장이 급성장하고 있다.", "IT 기술", "it"),
    ]

    from modules.collectors.idea_vault_auto_collector import IdeaVaultAutoCollector

    collector = IdeaVaultAutoCollector(
        job_store=store,
        feed_urls=["https://fake-feed"],
    )

    # _triage_articles 를 직접 모킹해 LLM 호출 없이 파싱 결과를 반환
    async def _fake_triage(articles):
        return [
            {
                "raw_text": item.raw_text,
                "mapped_category": item.mapped_category,
                "topic_mode": item.topic_mode,
                "parser_used": "gemini_flash",
                "source_url": articles[i].get("link", ""),
            }
            for i, item in enumerate(accepted)
        ]

    with patch.object(collector, "_fetch_articles", return_value=fake_articles):
        with patch.object(collector, "_triage_articles", side_effect=_fake_triage):
            saved = asyncio.run(collector.run_once())

    assert saved == 3, f"Expected 3 saved, got {saved}"

    # DB 에서 확인 (idea_vault 전용 stats 사용)
    vault_stats = store.get_idea_vault_stats()
    assert vault_stats.get("total", 0) >= 3, f"Expected ≥3 vault items in DB, got {vault_stats}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Track A — 중복 URL 차단 (UNIQUE INDEX)
# ─────────────────────────────────────────────────────────────────────────────


def test_idea_vault_auto_collector_dedup_by_url(tmp_path: Path):
    """같은 source_url 로 두 번 적재 시 두 번째는 무시되어야 한다."""
    store = build_store(tmp_path)
    store.set_system_setting("custom_categories", json_mod.dumps(["IT 기술"]))

    fake_articles = [
        {"title": "중복 기사", "link": "https://news/dup", "content": "중복 테스트 내용"},
    ]
    accepted = [_make_accepted_item("중복 기사 — 중복 테스트 내용", "IT 기술", "it")]

    from modules.collectors.idea_vault_auto_collector import IdeaVaultAutoCollector

    collector = IdeaVaultAutoCollector(job_store=store, feed_urls=["https://fake"])

    async def _fake_triage_dup(articles):
        return [
            {
                "raw_text": accepted[0].raw_text,
                "mapped_category": accepted[0].mapped_category,
                "topic_mode": accepted[0].topic_mode,
                "parser_used": "gemini_flash",
                "source_url": "https://news/dup",
            }
        ]

    async def _run_twice():
        with patch.object(collector, "_fetch_articles", return_value=fake_articles):
            with patch.object(collector, "_triage_articles", side_effect=_fake_triage_dup):
                first = await collector.run_once()
                second = await collector.run_once()
        return first, second

    first, second = asyncio.run(_run_twice())

    assert first == 1, f"First run should save 1, got {first}"
    assert second == 0, f"Second run should save 0 (duplicate), got {second}"

    vault_stats = store.get_idea_vault_stats()
    assert vault_stats.get("total", 0) == 1, f"DB should have exactly 1 item, got {vault_stats}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Track B — Telegram Webhook: 올바른 시크릿 → 200 + stored=True
# ─────────────────────────────────────────────────────────────────────────────


def test_telegram_webhook_valid_secret(tmp_path: Path):
    """올바른 X-Telegram-Bot-Api-Secret-Token 헤더 → 200, stored=True 반환."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "tg_webhook_test.db")
    store.set_system_setting("telegram_webhook_secret", "mysecret")
    store.set_system_setting("custom_categories", json_mod.dumps(["IT 기술"]))

    accepted = [_make_accepted_item("AI 뉴스 아이디어", "IT 기술", "it")]
    mock_parser = _make_vault_parser_mock(accepted)

    app.dependency_overrides[get_job_store] = lambda: store

    # lifespan 내 비동기 작업(스케줄러 시작, 텔레그램 폴링) 을 무력화
    async def _noop_collect(*args, **kwargs):
        return 0

    try:
        with patch("server.main._build_scheduler", return_value=None):
            with patch("server.main.collect_pending_updates", side_effect=_noop_collect):
                client = TestClient(app, raise_server_exceptions=True)

                payload = {
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 123456},
                        "text": "AI 뉴스 아이디어",
                    }
                }

                with patch(
                    "modules.llm.idea_vault_parser.IdeaVaultBatchParser",
                    return_value=mock_parser,
                ):
                    response = client.post(
                        "/api/telegram/webhook",
                        json=payload,
                        headers={"X-Telegram-Bot-Api-Secret-Token": "mysecret"},
                    )

        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["ok"] is True
        assert data["stored"] is True
    finally:
        app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Track B — Telegram Webhook: 잘못된 시크릿 → 403
# ─────────────────────────────────────────────────────────────────────────────


def test_telegram_webhook_invalid_secret(tmp_path: Path):
    """잘못된 Webhook Secret 헤더 → 403 반환."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "tg_403_test.db")
    store.set_system_setting("telegram_webhook_secret", "correctsecret")

    app.dependency_overrides[get_job_store] = lambda: store

    async def _noop_collect(*args, **kwargs):
        return 0

    try:
        with patch("server.main._build_scheduler", return_value=None):
            with patch("server.main.collect_pending_updates", side_effect=_noop_collect):
                client = TestClient(app, raise_server_exceptions=False)
                payload = {"message": {"chat": {"id": 1}, "text": "hello"}}
                response = client.post(
                    "/api/telegram/webhook",
                    json=payload,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "WRONG"},
                )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
    finally:
        app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Track B — Telegram Webhook: 빈 텍스트 → stored=False
# ─────────────────────────────────────────────────────────────────────────────


def test_telegram_webhook_empty_text(tmp_path: Path):
    """빈 텍스트 메시지 → 200 응답, stored=False."""
    from fastapi.testclient import TestClient

    store = build_store(tmp_path, "tg_empty_test.db")
    # 시크릿 없음 (검증 스킵)

    app.dependency_overrides[get_job_store] = lambda: store

    async def _noop_collect(*args, **kwargs):
        return 0

    try:
        with patch("server.main._build_scheduler", return_value=None):
            with patch("server.main.collect_pending_updates", side_effect=_noop_collect):
                client = TestClient(app, raise_server_exceptions=True)
                payload = {"message": {"chat": {"id": 1}, "text": ""}}
                response = client.post("/api/telegram/webhook", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["stored"] is False
        assert data["reason"] == "empty_text"
    finally:
        app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Track B — collect_pending_updates: getUpdates 모킹 → vault 적재
# ─────────────────────────────────────────────────────────────────────────────


def test_collect_pending_updates_stores_messages(tmp_path: Path):
    """getUpdates 에서 2건 반환 시 vault 에 2건 저장되고 last_id 가 갱신되어야 한다."""
    store = build_store(tmp_path, "tg_getup_test.db")
    store.set_system_setting("telegram_bot_token", "fake:BOT_TOKEN")
    store.set_system_setting("custom_categories", json_mod.dumps(["IT 기술"]))

    fake_updates = [
        {
            "update_id": 101,
            "message": {"chat": {"id": 123}, "text": "첫 번째 오프라인 메시지"},
        },
        {
            "update_id": 102,
            "message": {"chat": {"id": 123}, "text": "두 번째 오프라인 메시지"},
        },
    ]

    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "result": fake_updates}

    accepted_1 = [_make_accepted_item("첫 번째 오프라인 메시지", "IT 기술", "it")]
    accepted_2 = [_make_accepted_item("두 번째 오프라인 메시지", "IT 기술", "it")]

    call_count = 0

    async def _fake_parse_bulk(text, categories, batch_size):
        nonlocal call_count
        call_count += 1
        mock_result = MagicMock()
        mock_result.accepted_items = accepted_1 if call_count == 1 else accepted_2
        mock_result.parser_used = "gemini_flash"
        return mock_result

    mock_parser_instance = MagicMock()
    mock_parser_instance.parse_bulk = _fake_parse_bulk

    async def _run():
        from server.routers.telegram_webhook import collect_pending_updates

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("server.routers.telegram_webhook.httpx.AsyncClient", return_value=mock_client):
            with patch("modules.llm.idea_vault_parser.IdeaVaultBatchParser", return_value=mock_parser_instance):
                return await collect_pending_updates(store)

    stored = asyncio.run(_run())

    assert stored == 2, f"Expected 2 stored, got {stored}"

    # last_id 갱신 확인
    last_id = store.get_system_setting("telegram_last_processed_update_id", "0")
    assert last_id == "102", f"Expected last_id=102, got {last_id}"

    # vault DB 확인
    vault_stats = store.get_idea_vault_stats()
    assert vault_stats.get("total", 0) >= 2, f"Expected ≥2 items in vault, got {vault_stats}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Track B — collect_pending_updates: 봇 토큰 없음 → graceful skip
# ─────────────────────────────────────────────────────────────────────────────


def test_collect_pending_updates_no_token(tmp_path: Path):
    """봇 토큰이 없을 때 collect_pending_updates 가 0을 반환해야 한다."""
    store = build_store(tmp_path, "tg_notoken_test.db")
    # 봇 토큰 설정 없음

    async def _run():
        from server.routers.telegram_webhook import collect_pending_updates
        return await collect_pending_updates(store)

    result = asyncio.run(_run())
    assert result == 0, f"Expected 0 (no token), got {result}"
