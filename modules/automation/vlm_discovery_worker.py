"""VLM 모델 카탈로그 동기화 워커."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .job_store import JobStore
from .time_utils import now_utc
from ..llm.vlm_router import VLM_MODEL_MATRIX


class VLMDiscoveryWorker:
    """공식 소스 우선 정책으로 VLM 카탈로그를 동기화한다."""

    def __init__(self, *, job_store: JobStore) -> None:
        self.job_store = job_store

    def sync_catalog(self) -> Dict[str, int]:
        """VLM 카탈로그를 동기화하고 통계를 반환한다."""
        before_rows = self.job_store.list_vlm_catalog_entries(limit=1000)
        before_pairs = {(row["provider"], row["model"]) for row in before_rows}

        entries = self._build_official_entries()
        stats = self.job_store.upsert_vlm_catalog_entries(entries)
        source_pairs: List[Tuple[str, str]] = [(item["provider"], item["model"]) for item in entries]
        deprecated = self.job_store.mark_missing_vlm_models_deprecated(source_pairs)

        after_rows = self.job_store.list_vlm_catalog_entries(limit=1000)
        after_map = {(row["provider"], row["model"]): row for row in after_rows}
        inserted_pairs = [pair for pair in after_map.keys() if pair not in before_pairs]

        for provider, model in inserted_pairs:
            self.job_store.record_vlm_discovery_event(
                event_type="discovered",
                provider=provider,
                model=model,
                detail={"source": "official_matrix"},
            )

        if deprecated > 0:
            # deprecated된 모델 수는 요약 이벤트로 기록한다.
            self.job_store.record_vlm_discovery_event(
                event_type="deprecated",
                provider="system",
                model="bulk",
                detail={"count": deprecated},
            )

        self.job_store.set_system_setting("vlm_last_discovery_sync_at", now_utc())
        return {
            "inserted": int(stats.get("inserted", 0)),
            "updated": int(stats.get("updated", 0)),
            "unchanged": int(stats.get("unchanged", 0)),
            "deprecated": int(deprecated),
        }

    def _build_official_entries(self) -> List[Dict[str, Any]]:
        """기본 VLM 모델 매트릭스를 카탈로그 엔트리로 변환한다."""
        entries: List[Dict[str, Any]] = []
        for spec in VLM_MODEL_MATRIX:
            entries.append(
                {
                    "provider": spec.provider,
                    "client_provider": spec.client_provider,
                    "model": spec.model,
                    "key_id": spec.key_id,
                    "label": spec.label,
                    "status": "discovered",
                    "supports_image": bool(spec.supports_image),
                    "include_in_competition": False,
                    "quality_score": float(spec.quality_score),
                    "reliability_score": float(spec.reliability_score),
                    "scoring_bias_offset": float(spec.scoring_bias_offset),
                    "input_cost_per_1m": float(spec.input_cost_per_1m_usd),
                    "output_cost_per_1m": float(spec.output_cost_per_1m_usd),
                    "currency": "USD",
                    "max_image_resolution": "",
                    "vision_context_window": 0,
                    "error_rate_24h": 0.0,
                    "avg_latency_ms": 0.0,
                    "metadata_json": {
                        "source": "official_matrix",
                        "source_priority": "official",
                    },
                }
            )
        return entries
