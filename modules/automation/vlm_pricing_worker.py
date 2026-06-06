"""VLM 가격/환율 동기화 워커."""

from __future__ import annotations

from typing import Dict, Tuple

from .job_store import JobStore
from .time_utils import now_utc
from ..llm.vlm_router import VLM_MODEL_MATRIX


class VLMPricingWorker:
    """카탈로그 단가를 동기화하고 변경 이력을 기록한다."""

    def __init__(self, *, job_store: JobStore, usd_to_krw: float = 1400.0) -> None:
        self.job_store = job_store
        self.usd_to_krw = float(usd_to_krw or 1400.0)

    def sync_prices(self) -> Dict[str, int]:
        """가격 동기화를 수행한다."""
        price_map: Dict[Tuple[str, str], Tuple[float, float]] = {
            (spec.provider, spec.model): (float(spec.input_cost_per_1m_usd), float(spec.output_cost_per_1m_usd))
            for spec in VLM_MODEL_MATRIX
        }

        changed = 0
        unchanged = 0
        skipped = 0
        catalog_rows = self.job_store.list_vlm_catalog_entries(limit=1000)
        for row in catalog_rows:
            pair = (str(row.get("provider", "")), str(row.get("model", "")))
            if pair not in price_map:
                skipped += 1
                continue
            input_cost, output_cost = price_map[pair]
            updated = self.job_store.update_vlm_catalog_pricing(
                provider=pair[0],
                model=pair[1],
                input_cost_per_1m=input_cost,
                output_cost_per_1m=output_cost,
                currency="USD",
                source="official",
                fx_rate=self.usd_to_krw,
            )
            if updated:
                changed += 1
                self.job_store.record_vlm_discovery_event(
                    event_type="price_changed",
                    provider=pair[0],
                    model=pair[1],
                    detail={
                        "input_cost_per_1m": input_cost,
                        "output_cost_per_1m": output_cost,
                        "currency": "USD",
                    },
                )
            else:
                unchanged += 1

        self.job_store.set_system_setting("vlm_last_price_sync_at", now_utc())
        return {
            "changed": changed,
            "unchanged": unchanged,
            "skipped": skipped,
        }
