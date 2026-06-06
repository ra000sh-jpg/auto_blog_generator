from __future__ import annotations

import json
from pathlib import Path

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.text_model_discovery_worker import (
    DEEPSEEK_PRICING_URL,
    QWEN_PRICING_URL,
    TextModelDiscoveryWorker,
    parse_deepseek_models,
    parse_qwen_models,
)


def build_store(tmp_path: Path, name: str = "text_model_discovery.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _find_catalog_row(store: JobStore, provider: str, model: str) -> dict:
    for row in store.list_text_model_catalog_entries(limit=1000):
        if row["provider"] == provider and row["model"] == model:
            return row
    raise AssertionError(f"text model catalog row not found: {provider}/{model}")


def test_parse_official_text_model_pages():
    """공식 가격 페이지 일부만 있어도 주요 텍스트 모델을 추출해야 한다."""
    deepseek_html = """
    <html><body>
    MODEL deepseek-v4-flash deepseek-v4-pro
    FEATURES Json Output ✓ Tool Calls ✓
    1M INPUT TOKENS (CACHE MISS) $0.14 $0.435
    1M OUTPUT TOKENS $0.28 $0.87
    </body></html>
    """
    qwen_html = """
    <html><body>
    qwen-plus qwen-flash qwen3.5-flash qwen-vl-plus qwen-omni-turbo
    Input price (per 1M tokens) Output price (per 1M tokens)
    </body></html>
    """

    deepseek_models = parse_deepseek_models(deepseek_html)
    qwen_models = parse_qwen_models(qwen_html)

    assert {item["model"] for item in deepseek_models} == {"deepseek-v4-flash", "deepseek-v4-pro"}
    qwen_ids = {item["model"] for item in qwen_models}
    assert "qwen-flash" in qwen_ids
    assert "qwen-plus" in qwen_ids
    assert "qwen-vl-plus" not in qwen_ids
    assert "qwen-omni-turbo" not in qwen_ids


def test_text_model_discovery_worker_syncs_catalog_and_router_registry(tmp_path: Path):
    """공식 소스 모델을 카탈로그에 저장하고 기존 매트릭스 모델은 라우터 등록 목록에 보강해야 한다."""
    store = build_store(tmp_path)
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps({"deepseek": "test-deepseek", "qwen": "test-qwen"}, ensure_ascii=False),
    )

    def fetcher(url: str) -> str:
        if url == DEEPSEEK_PRICING_URL:
            return "<html><body>deepseek-v4-flash deepseek-v4-pro Json Output Tool Calls</body></html>"
        if url == QWEN_PRICING_URL:
            return "<html><body>qwen-plus qwen-flash qwen3.5-flash qwen-vl-plus</body></html>"
        return ""

    worker = TextModelDiscoveryWorker(job_store=store, fetcher=fetcher)
    stats = worker.sync_catalog()

    assert stats["inserted"] >= 4
    assert stats["source_failures"] == 0
    assert stats["registered_added"] >= 1
    assert store.get_system_setting("text_model_last_discovery_sync_at", "") != ""

    deepseek = _find_catalog_row(store, "deepseek", "deepseek-v4-pro")
    assert deepseek["supports_json"] is True
    assert deepseek["supports_tool_calls"] is True
    assert abs(deepseek["output_cost_per_1m"] - 0.87) < 1e-9

    qwen_flash = _find_catalog_row(store, "qwen", "qwen-flash")
    assert abs(qwen_flash["input_cost_per_1m"] - 0.05) < 1e-9

    registered = json.loads(store.get_system_setting("router_registered_models", "[]"))
    registered_ids = {item["model_id"] for item in registered}
    assert "deepseek-v4-flash" in registered_ids
    assert "qwen-flash" in registered_ids

    with store.connection() as conn:
        event_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM text_model_discovery_events
            WHERE event_type = 'discovered'
            """
        ).fetchone()
    assert event_count is not None
    assert int(event_count["count"]) >= 4
