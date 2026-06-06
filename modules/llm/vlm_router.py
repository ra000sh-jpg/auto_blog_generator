"""전략 기반 VLM 라우팅 유틸리티."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional

from .. import constants

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VLMModelSpec:
    """VLM 모델 스펙."""

    provider: str
    client_provider: str
    model: str
    label: str
    key_id: str
    input_cost_per_1m_usd: float
    output_cost_per_1m_usd: float
    quality_score: float
    reliability_score: float
    supports_image: bool = True
    active: bool = True
    scoring_bias_offset: float = 0.0


@dataclass(frozen=True)
class VLMRouteCandidate:
    """VLM 라우팅 후보."""

    provider: str
    client_provider: str
    model: str
    key_id: str
    quality_score: float
    reliability_score: float
    scoring_bias_offset: float
    estimated_cost_krw: float
    route_score: float


VLM_MODEL_MATRIX: List[VLMModelSpec] = [
    VLMModelSpec(
        provider="nvidia",
        client_provider="nvidia_vlm",
        model=constants.VLM_DEFAULT_MODEL,
        label="NVIDIA Llama 3.2 90B Vision",
        key_id="nvidia",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=92.0,
        reliability_score=90.0,
    ),
    VLMModelSpec(
        provider="gemini",
        client_provider="gemini_vlm",
        model="gemini-2.5-flash-lite",
        label="Gemini 2.5 Flash Lite",
        key_id="gemini",
        input_cost_per_1m_usd=0.10,
        output_cost_per_1m_usd=0.40,
        quality_score=88.0,
        reliability_score=89.0,
    ),
    VLMModelSpec(
        provider="gemini",
        client_provider="gemini_vlm",
        model="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.30,
        output_cost_per_1m_usd=2.50,
        quality_score=91.0,
        reliability_score=88.0,
    ),
    VLMModelSpec(
        provider="groq",
        client_provider="groq_vlm",
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        label="Groq Llama 4 Scout",
        key_id="groq",
        input_cost_per_1m_usd=0.11,
        output_cost_per_1m_usd=0.34,
        quality_score=84.0,
        reliability_score=90.0,
    ),
    VLMModelSpec(
        provider="groq",
        client_provider="groq_vlm",
        model="meta-llama/llama-4-maverick-17b-128e-instruct",
        label="Groq Llama 4 Maverick",
        key_id="groq",
        input_cost_per_1m_usd=0.20,
        output_cost_per_1m_usd=0.60,
        quality_score=87.0,
        reliability_score=88.0,
    ),
    VLMModelSpec(
        provider="openai",
        client_provider="openai_vlm",
        model="gpt-4.1-mini",
        label="OpenAI GPT-4.1 mini",
        key_id="openai",
        input_cost_per_1m_usd=0.40,
        output_cost_per_1m_usd=1.60,
        quality_score=91.0,
        reliability_score=92.0,
    ),
    VLMModelSpec(
        provider="qwen",
        client_provider="qwen_vlm",
        model="qwen-vl-plus",
        label="Qwen VL Plus",
        key_id="qwen",
        input_cost_per_1m_usd=0.11,
        output_cost_per_1m_usd=0.34,
        quality_score=85.0,
        reliability_score=87.0,
    ),
]


def normalize_vlm_strategy_mode(raw_value: str, fallback_strategy: str = "cost") -> str:
    """VLM 전략 모드를 정규화한다."""
    value = str(raw_value or "").strip().lower()
    if value in {"cost", "balanced", "quality"}:
        return value
    if value == "inherit":
        fallback = str(fallback_strategy or "").strip().lower()
        return fallback if fallback in {"cost", "balanced", "quality"} else "cost"
    return "cost"


class VLMRouter:
    """전략 기반 VLM 후보 체인을 구성한다."""

    def __init__(
        self,
        *,
        model_matrix: Optional[List[VLMModelSpec]] = None,
        usd_to_krw: float = 1400.0,
        job_store: Optional[Any] = None,
        prefer_catalog: bool = True,
        allowed_catalog_statuses: Optional[List[str]] = None,
    ) -> None:
        self.model_matrix = list(model_matrix or VLM_MODEL_MATRIX)
        self.usd_to_krw = float(usd_to_krw or 1400.0)
        self.job_store = job_store
        self.prefer_catalog = bool(prefer_catalog)
        normalized_statuses = allowed_catalog_statuses or ["active", "shadow", "discovered"]
        self.allowed_catalog_statuses = {
            str(status or "").strip().lower()
            for status in normalized_statuses
            if str(status or "").strip()
        }

    def build_route(
        self,
        *,
        strategy_mode: str,
        text_api_keys: Dict[str, str],
        preferred_model: str = "",
        estimated_input_tokens: int = constants.VLM_ROUTER_EST_INPUT_TOKENS,
        estimated_output_tokens: int = constants.VLM_ROUTER_EST_OUTPUT_TOKENS,
        quality_floor: float = constants.VLM_DEFAULT_QUALITY_FLOOR,
        max_cost_guard_krw: float = constants.VLM_DEFAULT_MAX_COST_GUARD_KRW,
        max_candidates: int = constants.VLM_ROUTER_MAX_CANDIDATES,
        circuit_breaker: Optional[Any] = None,
    ) -> List[VLMRouteCandidate]:
        """전략별 정렬된 VLM fallback chain을 반환한다."""
        normalized_strategy = normalize_vlm_strategy_mode(strategy_mode, fallback_strategy="cost")
        key_map = {str(key).strip().lower(): str(value or "").strip() for key, value in dict(text_api_keys).items()}
        model_specs = self._resolve_model_specs()

        filtered_specs: List[VLMModelSpec] = []
        for spec in model_specs:
            if not spec.active or not spec.supports_image:
                continue
            if spec.quality_score < float(quality_floor):
                continue
            api_key = key_map.get(spec.key_id, "")
            if not api_key:
                continue
            if circuit_breaker is not None:
                model_key = f"{spec.client_provider}:{spec.model}".lower()
                try:
                    if bool(circuit_breaker.is_open(model_key)):
                        continue
                except Exception:
                    pass
            estimated_cost = self._estimate_cost_krw(
                input_tokens=estimated_input_tokens,
                output_tokens=estimated_output_tokens,
                input_cost_per_1m=spec.input_cost_per_1m_usd,
                output_cost_per_1m=spec.output_cost_per_1m_usd,
            )
            if max_cost_guard_krw > 0 and estimated_cost > float(max_cost_guard_krw):
                continue
            filtered_specs.append(spec)

        if not filtered_specs:
            return []

        candidate_rows = [
            (
                spec,
                self._estimate_cost_krw(
                    input_tokens=estimated_input_tokens,
                    output_tokens=estimated_output_tokens,
                    input_cost_per_1m=spec.input_cost_per_1m_usd,
                    output_cost_per_1m=spec.output_cost_per_1m_usd,
                ),
            )
            for spec in filtered_specs
        ]

        min_cost = min(cost for _, cost in candidate_rows)
        max_cost = max(cost for _, cost in candidate_rows)

        candidates: List[VLMRouteCandidate] = []
        for spec, estimated_cost in candidate_rows:
            route_score = self._compute_route_score(
                strategy_mode=normalized_strategy,
                quality_score=spec.quality_score,
                reliability_score=spec.reliability_score,
                estimated_cost_krw=estimated_cost,
                min_cost_krw=min_cost,
                max_cost_krw=max_cost,
            )
            candidates.append(
                VLMRouteCandidate(
                    provider=spec.provider,
                    client_provider=spec.client_provider,
                    model=spec.model,
                    key_id=spec.key_id,
                    quality_score=spec.quality_score,
                    reliability_score=spec.reliability_score,
                    scoring_bias_offset=spec.scoring_bias_offset,
                    estimated_cost_krw=estimated_cost,
                    route_score=route_score,
                )
            )

        # 전략 우선 정렬
        if normalized_strategy == "cost":
            candidates.sort(key=lambda item: (item.estimated_cost_krw, -item.reliability_score, -item.quality_score))
        elif normalized_strategy == "quality":
            candidates.sort(key=lambda item: (-item.quality_score, -item.reliability_score, item.estimated_cost_krw))
        else:
            candidates.sort(key=lambda item: (-item.route_score, item.estimated_cost_krw))

        # 과거 단일 모델 선택값을 우선하려는 경우 첫 후보로 승격한다.
        preferred = str(preferred_model or "").strip().lower()
        if preferred:
            preferred_index = next((idx for idx, item in enumerate(candidates) if item.model.lower() == preferred), -1)
            if preferred_index > 0:
                preferred_item = candidates.pop(preferred_index)
                candidates.insert(0, preferred_item)

        return candidates[: max(1, int(max_candidates or 1))]

    def _resolve_model_specs(self) -> List[VLMModelSpec]:
        """카탈로그 우선으로 모델 스펙을 결정하고 실패 시 기본 매트릭스로 폴백한다."""
        if not self.prefer_catalog or self.job_store is None:
            return list(self.model_matrix)

        list_fn = getattr(self.job_store, "list_vlm_catalog_entries", None)
        if not callable(list_fn):
            return list(self.model_matrix)

        try:
            catalog_rows = list_fn(limit=1000)
        except Exception:
            logger.debug("VLM catalog load failed; fallback to static matrix", exc_info=True)
            return list(self.model_matrix)

        specs = self._build_specs_from_catalog_rows(catalog_rows)
        if specs:
            return specs
        return list(self.model_matrix)

    def _build_specs_from_catalog_rows(self, rows: List[Dict[str, Any]]) -> List[VLMModelSpec]:
        """DB 카탈로그 row를 라우터 스펙으로 변환한다."""
        specs: List[VLMModelSpec] = []
        seen_pairs = set()
        for row in rows:
            provider = str(row.get("provider", "")).strip().lower()
            model = str(row.get("model", "")).strip()
            status = str(row.get("status", "")).strip().lower()
            if not provider or not model:
                continue
            if self.allowed_catalog_statuses and status not in self.allowed_catalog_statuses:
                continue

            pair = (provider, model)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            client_provider = str(row.get("client_provider", "")).strip().lower() or f"{provider}_vlm"
            key_id = str(row.get("key_id", "")).strip().lower() or provider
            label = str(row.get("label", "")).strip() or model
            supports_image = bool(row.get("supports_image", True))
            quality_score = float(row.get("quality_score", 0.0) or 0.0)
            reliability_score = float(row.get("reliability_score", 0.0) or 0.0)
            scoring_bias_offset = float(row.get("scoring_bias_offset", 0.0) or 0.0)
            currency = str(row.get("currency", "USD") or "USD").strip().upper()

            input_cost = self._convert_cost_to_usd(float(row.get("input_cost_per_1m", 0.0) or 0.0), currency)
            output_cost = self._convert_cost_to_usd(float(row.get("output_cost_per_1m", 0.0) or 0.0), currency)

            specs.append(
                VLMModelSpec(
                    provider=provider,
                    client_provider=client_provider,
                    model=model,
                    label=label,
                    key_id=key_id,
                    input_cost_per_1m_usd=input_cost,
                    output_cost_per_1m_usd=output_cost,
                    quality_score=quality_score,
                    reliability_score=reliability_score,
                    supports_image=supports_image,
                    active=True,
                    scoring_bias_offset=scoring_bias_offset,
                )
            )
        return specs

    def _convert_cost_to_usd(self, value: float, currency: str) -> float:
        """카탈로그 통화 단가를 USD 기준으로 정규화한다."""
        normalized_value = max(0.0, float(value or 0.0))
        normalized_currency = str(currency or "USD").strip().upper()
        if normalized_currency == "USD":
            return normalized_value
        if normalized_currency == "KRW":
            if self.usd_to_krw <= 0:
                return 0.0
            return normalized_value / self.usd_to_krw
        return normalized_value

    def _estimate_cost_krw(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        input_cost_per_1m: float,
        output_cost_per_1m: float,
    ) -> float:
        """토큰 수와 단가로 KRW 비용을 추정한다."""
        input_cost = (max(0, int(input_tokens)) / 1_000_000.0) * max(0.0, float(input_cost_per_1m))
        output_cost = (max(0, int(output_tokens)) / 1_000_000.0) * max(0.0, float(output_cost_per_1m))
        return (input_cost + output_cost) * self.usd_to_krw

    def _compute_route_score(
        self,
        *,
        strategy_mode: str,
        quality_score: float,
        reliability_score: float,
        estimated_cost_krw: float,
        min_cost_krw: float,
        max_cost_krw: float,
    ) -> float:
        """전략별 스코어를 계산한다."""
        if strategy_mode == "cost":
            return -estimated_cost_krw
        if strategy_mode == "quality":
            return quality_score

        # balanced: 품질 + 비용 효율 + 신뢰도
        quality_norm = max(0.0, min(1.0, float(quality_score) / 100.0))
        reliability_norm = max(0.0, min(1.0, float(reliability_score) / 100.0))
        if max_cost_krw <= min_cost_krw:
            cost_efficiency = 1.0
        else:
            cost_efficiency = 1.0 - ((estimated_cost_krw - min_cost_krw) / (max_cost_krw - min_cost_krw))
            cost_efficiency = max(0.0, min(1.0, cost_efficiency))
        return (0.45 * quality_norm) + (0.35 * cost_efficiency) + (0.20 * reliability_norm)
