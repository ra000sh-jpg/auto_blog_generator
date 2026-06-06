"""제로-설정 라우터/견적 API."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from modules.llm.llm_router import LLMRouter
from server.dependencies import get_llm_router

router = APIRouter()


class RouterSettingsPayload(BaseModel):
    """라우터 저장/미리보기 요청."""

    strategy_mode: str = "cost"
    text_api_keys: Dict[str, str] = Field(default_factory=dict)
    image_api_keys: Dict[str, str] = Field(default_factory=dict)
    cost_strict_mode: bool = True
    cost_free_only_fallback: bool = True
    cost_max_fallback_usd_per_1m: float = Field(default=1.0, ge=0.0)
    cost_retry_max_retries: int = Field(default=6, ge=1, le=12)
    cost_retry_base_delay_sec: float = Field(default=2.0, ge=0.0)
    cost_retry_max_delay_sec: float = Field(default=20.0, ge=0.0)
    cost_lock_quality_provider: bool = True
    image_engine: str = "pexels"
    image_ai_engine: str = "together_flux"
    image_ai_quota: str = "0"
    image_topic_quota_overrides: Dict[str, str] = Field(default_factory=dict)
    traffic_feedback_strong_mode: bool = False
    image_enabled: bool = True
    images_per_post: int = Field(default=1, ge=0, le=4)
    images_per_post_min: int = Field(default=0, ge=0, le=4)
    images_per_post_max: int = Field(default=4, ge=0, le=4)
    vlm_enabled: bool = False
    vlm_model: str = "meta/llama-3.2-90b-vision-instruct"
    vlm_strategy_mode: str = "inherit"
    vlm_eval_sampling_rate: float = Field(default=0.5, ge=0.0, le=1.0)
    vlm_quality_floor: float = Field(default=65.0, ge=0.0, le=100.0)
    vlm_max_cost_guard_krw: float = Field(default=30.0, ge=0.0)
    challenger_model: str = ""


class RouterQuoteResponse(BaseModel):
    """라우터 견적 응답."""

    strategy_mode: str
    roles: Dict[str, Any]
    estimate: Dict[str, Any]
    image: Dict[str, Any]
    available_text_models: list[Dict[str, Any]]


class RouterSettingsResponse(BaseModel):
    """라우터 설정 조회 응답."""

    settings: Dict[str, Any]
    quote: Dict[str, Any]
    roles: Dict[str, Any]
    competition: Dict[str, Any]
    matrix: Dict[str, Any]


@router.get(
    "/router-settings",
    response_model=RouterSettingsResponse,
    summary="제로-설정 라우터 현재 상태 조회",
)
def get_router_settings(
    llm_router: LLMRouter = Depends(get_llm_router),
) -> RouterSettingsResponse:
    """현재 저장값과 즉시 견적 정보를 조회한다."""
    payload = llm_router.export_for_ui()
    return RouterSettingsResponse(
        settings=payload["settings"],
        quote=payload["quote"],
        roles=payload["roles"],
        competition=payload.get("competition", {}),
        matrix=payload["matrix"],
    )


@router.post(
    "/router-settings/quote",
    response_model=RouterQuoteResponse,
    summary="실시간 견적 미리보기",
)
def quote_router_settings(
    request: RouterSettingsPayload,
    llm_router: LLMRouter = Depends(get_llm_router),
) -> RouterQuoteResponse:
    """저장 없이 조합 견적을 계산한다."""
    plan = llm_router.build_plan(
        overrides={
            "strategy_mode": request.strategy_mode,
            "text_api_keys": request.text_api_keys,
            "image_api_keys": request.image_api_keys,
            "cost_strict_mode": request.cost_strict_mode,
            "cost_free_only_fallback": request.cost_free_only_fallback,
            "cost_max_fallback_usd_per_1m": request.cost_max_fallback_usd_per_1m,
            "cost_retry_max_retries": request.cost_retry_max_retries,
            "cost_retry_base_delay_sec": request.cost_retry_base_delay_sec,
            "cost_retry_max_delay_sec": request.cost_retry_max_delay_sec,
            "cost_lock_quality_provider": request.cost_lock_quality_provider,
            "image_engine": request.image_engine,
            "image_ai_engine": request.image_ai_engine,
            "image_ai_quota": request.image_ai_quota,
            "image_topic_quota_overrides": request.image_topic_quota_overrides,
            "traffic_feedback_strong_mode": request.traffic_feedback_strong_mode,
            "image_enabled": request.image_enabled,
            "images_per_post": request.images_per_post,
            "images_per_post_min": request.images_per_post_min,
            "images_per_post_max": request.images_per_post_max,
            "vlm_enabled": request.vlm_enabled,
            "vlm_model": request.vlm_model,
            "vlm_strategy_mode": request.vlm_strategy_mode,
            "vlm_eval_sampling_rate": request.vlm_eval_sampling_rate,
            "vlm_quality_floor": request.vlm_quality_floor,
            "vlm_max_cost_guard_krw": request.vlm_max_cost_guard_krw,
        }
    )
    return RouterQuoteResponse(
        strategy_mode=plan["strategy_mode"],
        roles=plan["roles"],
        estimate=plan["estimate"],
        image=plan["image"],
        available_text_models=plan["available_text_models"],
    )


@router.post(
    "/router-settings/save",
    response_model=RouterSettingsResponse,
    summary="제로-설정 라우터 저장",
)
def save_router_settings(
    request: RouterSettingsPayload,
    llm_router: LLMRouter = Depends(get_llm_router),
) -> RouterSettingsResponse:
    """사용자 라우팅 설정을 저장하고 최신 견적을 반환한다."""
    llm_router.save_settings(
        {
            "strategy_mode": request.strategy_mode,
            "text_api_keys": request.text_api_keys,
            "image_api_keys": request.image_api_keys,
            "cost_strict_mode": request.cost_strict_mode,
            "cost_free_only_fallback": request.cost_free_only_fallback,
            "cost_max_fallback_usd_per_1m": request.cost_max_fallback_usd_per_1m,
            "cost_retry_max_retries": request.cost_retry_max_retries,
            "cost_retry_base_delay_sec": request.cost_retry_base_delay_sec,
            "cost_retry_max_delay_sec": request.cost_retry_max_delay_sec,
            "cost_lock_quality_provider": request.cost_lock_quality_provider,
            "image_engine": request.image_engine,
            "image_ai_engine": request.image_ai_engine,
            "image_ai_quota": request.image_ai_quota,
            "image_topic_quota_overrides": request.image_topic_quota_overrides,
            "traffic_feedback_strong_mode": request.traffic_feedback_strong_mode,
            "image_enabled": request.image_enabled,
            "images_per_post": request.images_per_post,
            "images_per_post_min": request.images_per_post_min,
            "images_per_post_max": request.images_per_post_max,
            "vlm_enabled": request.vlm_enabled,
            "vlm_model": request.vlm_model,
            "vlm_strategy_mode": request.vlm_strategy_mode,
            "vlm_eval_sampling_rate": request.vlm_eval_sampling_rate,
            "vlm_quality_floor": request.vlm_quality_floor,
            "vlm_max_cost_guard_krw": request.vlm_max_cost_guard_krw,
            "challenger_model": request.challenger_model,
        }
    )
    payload = llm_router.export_for_ui()
    return RouterSettingsResponse(
        settings=payload["settings"],
        quote=payload["quote"],
        roles=payload["roles"],
        competition=payload.get("competition", {}),
        matrix=payload["matrix"],
    )
