from __future__ import annotations

import json
from pathlib import Path

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.time_utils import now_utc
from modules.automation.vlm_discovery_worker import VLMDiscoveryWorker
from modules.automation.vlm_pricing_worker import VLMPricingWorker
from modules.automation.vlm_validation_worker import VLMValidationWorker
from modules.llm.vlm_router import VLM_MODEL_MATRIX


def build_store(tmp_path: Path, name: str = "vlm_workers.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _find_catalog_row(store: JobStore, provider: str, model: str) -> dict:
    for row in store.list_vlm_catalog_entries(limit=1000):
        if row["provider"] == provider and row["model"] == model:
            return row
    raise AssertionError(f"catalog row not found: {provider}/{model}")


def test_vlm_discovery_worker_syncs_catalog(tmp_path: Path):
    store = build_store(tmp_path, "vlm_discovery_worker.db")
    worker = VLMDiscoveryWorker(job_store=store)

    stats = worker.sync_catalog()
    assert stats["inserted"] == len(VLM_MODEL_MATRIX)
    assert stats["updated"] == 0
    assert stats["deprecated"] == 0

    rows = store.list_vlm_catalog_entries(limit=1000)
    assert len(rows) >= len(VLM_MODEL_MATRIX)
    assert store.get_system_setting("vlm_last_discovery_sync_at", "") != ""

    with store.connection() as conn:
        event_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM vlm_discovery_events
            WHERE event_type = 'discovered'
            """
        ).fetchone()
    assert event_count is not None
    assert int(event_count["count"]) == len(VLM_MODEL_MATRIX)


def test_vlm_pricing_worker_updates_catalog_and_history(tmp_path: Path):
    store = build_store(tmp_path, "vlm_pricing_worker.db")
    discovery = VLMDiscoveryWorker(job_store=store)
    discovery.sync_catalog()

    target_spec = next(spec for spec in VLM_MODEL_MATRIX if spec.provider == "gemini")
    with store.connection() as conn:
        conn.execute(
            """
            UPDATE vlm_model_catalog
            SET input_cost_per_1m = 9.99,
                output_cost_per_1m = 9.99
            WHERE provider = ? AND model = ?
            """,
            (target_spec.provider, target_spec.model),
        )

    worker = VLMPricingWorker(job_store=store, usd_to_krw=1370.5)
    stats = worker.sync_prices()
    assert stats["changed"] >= 1
    assert store.get_system_setting("vlm_last_price_sync_at", "") != ""

    updated = _find_catalog_row(store, target_spec.provider, target_spec.model)
    assert abs(float(updated["input_cost_per_1m"]) - float(target_spec.input_cost_per_1m_usd)) < 1e-9
    assert abs(float(updated["output_cost_per_1m"]) - float(target_spec.output_cost_per_1m_usd)) < 1e-9

    with store.connection() as conn:
        history_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM vlm_model_price_history
            WHERE provider = ? AND model = ?
            """,
            (target_spec.provider, target_spec.model),
        ).fetchone()
    assert history_count is not None
    assert int(history_count["count"]) >= 1


def test_vlm_validation_worker_moves_discovered_to_shadow_and_active(tmp_path: Path):
    store = build_store(tmp_path, "vlm_validation_worker.db")
    discovery = VLMDiscoveryWorker(job_store=store)
    discovery.sync_catalog()

    target = VLM_MODEL_MATRIX[0]
    for row in store.list_vlm_catalog_entries(limit=1000):
        if row["provider"] == target.provider and row["model"] == target.model:
            continue
        store.update_vlm_catalog_status(
            provider=row["provider"],
            model=row["model"],
            status="deprecated",
        )

    store.set_system_setting("router_text_api_keys", json.dumps({target.key_id: "test-key"}))
    worker = VLMValidationWorker(job_store=store)

    first = worker.run_cycle(limit=10, auto_activate=False)
    assert first["moved_shadow"] == 1
    shadow_row = _find_catalog_row(store, target.provider, target.model)
    assert shadow_row["status"] == "shadow"

    provider_name = f"{target.provider}_vlm"
    for idx in range(20):
        job_id = f"vlm-shadow-{idx}"
        assert store.schedule_job(
            job_id=job_id,
            title=f"VLM Shadow {idx}",
            seed_keywords=["vlm", "shadow"],
            platform="naver",
            persona_id="P1",
            scheduled_at=now_utc(),
        )
        store.record_job_metric(
            job_id=job_id,
            metric_type="vlm_visual_eval",
            status="success",
            duration_ms=400.0,
            provider=provider_name,
            detail={
                "provider_used": provider_name,
                "model_used": target.model,
                "error": "",
            },
        )

    second = worker.run_cycle(limit=10, auto_activate=True)
    assert second["activated"] == 1
    active_row = _find_catalog_row(store, target.provider, target.model)
    assert active_row["status"] == "active"
