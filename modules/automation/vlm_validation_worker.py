"""VLM 카나리아/섀도우 검증 워커."""

from __future__ import annotations

import json
from typing import Any, Dict

from .job_store import JobStore
from .time_utils import now_utc


class VLMValidationWorker:
    """카탈로그 후보를 canary/shadow 단계로 검증한다."""

    def __init__(self, *, job_store: JobStore) -> None:
        self.job_store = job_store

    def run_cycle(self, *, limit: int = 10, auto_activate: bool = True) -> Dict[str, int]:
        """검증 사이클을 수행한다."""
        candidates = self.job_store.list_vlm_validation_candidates(limit=limit)
        moved_shadow = 0
        activated = 0
        rejected = 0
        observed = 0

        for candidate in candidates:
            provider = str(candidate.get("provider", "")).strip().lower()
            model = str(candidate.get("model", "")).strip()
            status = str(candidate.get("status", "")).strip().lower()
            if not provider or not model or not status:
                continue

            if status == "discovered":
                canary = self.run_canary(provider=provider, model=model, candidate=candidate)
                if bool(canary.get("passed", False)):
                    metadata_update = {"canary": canary, "canary_checked_at": now_utc()}
                    self.job_store.update_vlm_catalog_status(
                        provider=provider,
                        model=model,
                        status="shadow",
                        metadata_update=metadata_update,
                    )
                    self.job_store.record_vlm_discovery_event(
                        event_type="validated",
                        provider=provider,
                        model=model,
                        detail=canary,
                    )
                    moved_shadow += 1
                else:
                    observed += 1
                continue

            if status == "shadow":
                shadow = self.run_shadow_window(provider=provider, model=model, hours=24)
                if bool(shadow.get("passed", False)) and auto_activate:
                    metadata_update = {"shadow": shadow, "activated_at": now_utc()}
                    self.job_store.update_vlm_catalog_status(
                        provider=provider,
                        model=model,
                        status="active",
                        metadata_update=metadata_update,
                    )
                    self.job_store.record_vlm_discovery_event(
                        event_type="activated",
                        provider=provider,
                        model=model,
                        detail=shadow,
                    )
                    activated += 1
                elif bool(shadow.get("failed_hard", False)):
                    self.job_store.update_vlm_catalog_status(
                        provider=provider,
                        model=model,
                        status="rejected",
                        metadata_update={"shadow": shadow, "rejected_at": now_utc()},
                    )
                    self.job_store.record_vlm_discovery_event(
                        event_type="rejected",
                        provider=provider,
                        model=model,
                        detail=shadow,
                    )
                    rejected += 1
                else:
                    observed += 1

        return {
            "moved_shadow": moved_shadow,
            "activated": activated,
            "rejected": rejected,
            "observed": observed,
        }

    def run_canary(self, *, provider: str, model: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """정적 카나리아 게이트를 평가한다."""
        supports_image = bool(candidate.get("supports_image", False))
        quality_score = float(candidate.get("quality_score", 0.0) or 0.0)
        reliability_score = float(candidate.get("reliability_score", 0.0) or 0.0)

        # API 키가 없으면 활성화 대상에서 제외한다.
        has_key = self._has_provider_key(str(candidate.get("key_id", provider)).strip().lower())
        passed = bool(supports_image and quality_score >= 70.0 and reliability_score >= 70.0 and has_key)
        reason = "ok" if passed else "insufficient_quality_or_key"
        return {
            "passed": passed,
            "reason": reason,
            "supports_image": supports_image,
            "quality_score": quality_score,
            "reliability_score": reliability_score,
            "has_key": has_key,
            "provider": provider,
            "model": model,
            "checked_at": now_utc(),
        }

    def run_shadow_window(self, *, provider: str, model: str, hours: int = 24) -> Dict[str, Any]:
        """최근 실측치 기반 섀도우 판정을 수행한다."""
        metrics = self.job_store.get_vlm_recent_metrics(
            provider=f"{provider}_vlm",
            model=model,
            hours=hours,
        )
        total = int(metrics.get("total", 0) or 0)
        success_rate = float(metrics.get("success_rate", 0.0) or 0.0)
        avg_latency_ms = float(metrics.get("avg_latency_ms", 0.0) or 0.0)

        passed = total >= 20 and success_rate >= 0.90
        failed_hard = total >= 10 and success_rate < 0.50
        return {
            "passed": passed,
            "failed_hard": failed_hard,
            "total": total,
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency_ms,
            "hours": int(hours),
            "checked_at": now_utc(),
        }

    def _has_provider_key(self, key_id: str) -> bool:
        """router_text_api_keys에 키가 등록됐는지 확인한다."""
        raw = self.job_store.get_system_setting("router_text_api_keys", "{}")
        try:
            decoded = json.loads(str(raw or "{}"))
            if not isinstance(decoded, dict):
                decoded = {}
        except Exception:
            decoded = {}
        return bool(str(decoded.get(str(key_id).strip().lower(), "")).strip())
