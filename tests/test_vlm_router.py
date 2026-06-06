from __future__ import annotations

from pathlib import Path

from modules.automation.job_store import JobConfig, JobStore
from modules.llm.vlm_router import VLMRouter, normalize_vlm_strategy_mode


def build_store(tmp_path: Path, name: str = "test_vlm_router.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def test_normalize_vlm_strategy_mode_inherit() -> None:
    assert normalize_vlm_strategy_mode("inherit", fallback_strategy="balanced") == "balanced"
    assert normalize_vlm_strategy_mode("inherit", fallback_strategy="unknown") == "cost"


def test_vlm_router_cost_prefers_low_cost() -> None:
    router = VLMRouter(usd_to_krw=1400.0)
    chain = router.build_route(
        strategy_mode="cost",
        text_api_keys={"nvidia": "nv", "openai": "oa", "gemini": "gm", "groq": "gq", "qwen": "qw"},
        preferred_model="",
        quality_floor=0.0,
        max_cost_guard_krw=9999.0,
        max_candidates=3,
    )
    assert chain
    # 무료 후보가 먼저 와야 한다.
    assert chain[0].estimated_cost_krw <= chain[-1].estimated_cost_krw


def test_vlm_router_quality_prefers_high_quality() -> None:
    router = VLMRouter(usd_to_krw=1400.0)
    chain = router.build_route(
        strategy_mode="quality",
        text_api_keys={"nvidia": "nv", "openai": "oa", "gemini": "gm"},
        preferred_model="",
        quality_floor=0.0,
        max_cost_guard_krw=9999.0,
        max_candidates=3,
    )
    assert chain
    assert chain[0].quality_score >= chain[-1].quality_score


def test_vlm_router_prefers_catalog_over_static_matrix(tmp_path: Path) -> None:
    store = build_store(tmp_path, "vlm_router_catalog_priority.db")
    store.upsert_vlm_catalog_entries(
        [
            {
                "provider": "qwen",
                "client_provider": "qwen_vlm",
                "model": "qwen-vl-plus",
                "key_id": "qwen",
                "label": "Qwen VL Plus (Catalog)",
                "status": "active",
                "supports_image": True,
                "quality_score": 95.0,
                "reliability_score": 85.0,
                "scoring_bias_offset": 0.0,
                "input_cost_per_1m": 0.11,
                "output_cost_per_1m": 0.34,
                "currency": "USD",
            }
        ]
    )

    router = VLMRouter(job_store=store, usd_to_krw=1400.0)
    chain = router.build_route(
        strategy_mode="quality",
        text_api_keys={"qwen": "qw"},
        preferred_model="",
        quality_floor=90.0,
        max_cost_guard_krw=9999.0,
        max_candidates=3,
    )
    assert chain
    assert chain[0].provider == "qwen"
    assert chain[0].quality_score >= 95.0


def test_vlm_router_falls_back_to_static_matrix_when_catalog_empty(tmp_path: Path) -> None:
    store = build_store(tmp_path, "vlm_router_catalog_empty.db")
    router = VLMRouter(job_store=store, usd_to_krw=1400.0)
    chain = router.build_route(
        strategy_mode="quality",
        text_api_keys={"nvidia": "nv"},
        preferred_model="",
        quality_floor=80.0,
        max_cost_guard_krw=9999.0,
        max_candidates=3,
    )
    assert chain
    assert chain[0].provider == "nvidia"
