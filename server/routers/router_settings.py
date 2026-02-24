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
    image_engine: str = "pexels"
    image_ai_engine: str = "together_flux"
    image_ai_quota: str = "0"
    image_topic_quota_overrides: Dict[str, str] = Field(default_factory=dict)
    image_enabled: bool = True
    images_per_post: int = Field(default=1, ge=0, le=4)
    images_per_post_min: int = Field(default=0, ge=0, le=4)
    images_per_post_max: int = Field(default=4, ge=0, le=4)


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
            "image_engine": request.image_engine,
            "image_ai_engine": request.image_ai_engine,
            "image_ai_quota": request.image_ai_quota,
            "image_topic_quota_overrides": request.image_topic_quota_overrides,
            "image_enabled": request.image_enabled,
            "images_per_post": request.images_per_post,
            "images_per_post_min": request.images_per_post_min,
            "images_per_post_max": request.images_per_post_max,
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
            "image_engine": request.image_engine,
            "image_ai_engine": request.image_ai_engine,
            "image_ai_quota": request.image_ai_quota,
            "image_topic_quota_overrides": request.image_topic_quota_overrides,
            "image_enabled": request.image_enabled,
            "images_per_post": request.images_per_post,
            "images_per_post_min": request.images_per_post_min,
            "images_per_post_max": request.images_per_post_max,
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
