"""설정 조회 API."""

from __future__ import annotations

import os
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from modules.config import AppConfig
from modules.llm.llm_router import LLMRouter
from server.dependencies import get_app_config, get_llm_router

router = APIRouter()


class ApiKeyStatus(BaseModel):
    """API 키 상태."""

    provider: str
    env_var: str
    configured: bool
    masked: str


class PersonaOption(BaseModel):
    """페르소나 선택 옵션."""

    value: str
    label: str
    topic_mode: str


class TopicModeOption(BaseModel):
    """토픽 선택 옵션."""

    value: str
    label: str


class ConfigDefaults(BaseModel):
    """대시보드 기본값."""

    platform: str
    persona_id: str
    topic_mode: str
    api_base_url: str


class LLMRuntimeConfig(BaseModel):
    """LLM 실행 기본 설정."""

    primary_provider: str
    primary_model: str
    secondary_provider: str
    secondary_model: str


class ConfigResponse(BaseModel):
    """설정 조회 응답."""

    api_keys: List[ApiKeyStatus]
    personas: List[PersonaOption]
    topic_modes: List[TopicModeOption]
    defaults: ConfigDefaults
    llm: LLMRuntimeConfig


def _mask_secret(raw_value: str) -> str:
    """민감정보를 마스킹한다."""
    value = str(raw_value or "").strip()
    if not value:
        return ""

    if value.startswith("sk-"):
        tail = value[-4:] if len(value) > 7 else ""
        return f"sk-****{tail}" if tail else "sk-****"

    if len(value) <= 4:
        return "*" * len(value)

    return f"{value[:2]}****{value[-2:]}"


def _build_key_status(provider: str, env_var: str, router_value: str) -> ApiKeyStatus:
    """DB 또는 환경변수 기반 API 키 상태를 만든다."""
    raw_value = router_value.strip() if router_value.strip() else os.getenv(env_var, "").strip()
    configured = bool(raw_value)
    return ApiKeyStatus(
        provider=provider,
        env_var=env_var,
        configured=configured,
        masked=_mask_secret(raw_value) if configured else "",
    )


@router.get("/config", response_model=ConfigResponse, summary="대시보드 설정 조회")
def get_config(
    app_config: AppConfig = Depends(get_app_config),
    llm_router: LLMRouter = Depends(get_llm_router),
) -> ConfigResponse:
    """대시보드 표시용 읽기 전용 설정 정보를 반환한다."""
    saved_settings = llm_router.get_saved_settings()
    text_keys = saved_settings.get("text_api_keys", {})
    
    api_keys = [
        _build_key_status("openai", "OPENAI_API_KEY", str(text_keys.get("openai", ""))),
        _build_key_status("deepseek", "DEEPSEEK_API_KEY", str(text_keys.get("deepseek", ""))),
        _build_key_status("dashscope", "DASHSCOPE_API_KEY", str(text_keys.get("qwen", ""))),
        _build_key_status("anthropic", "ANTHROPIC_API_KEY", str(text_keys.get("claude", ""))),
    ]

    personas = [
        PersonaOption(value="P1", label="Cafe Creator (P1)", topic_mode="cafe"),
        PersonaOption(value="P2", label="Tech Blogger (P2)", topic_mode="it"),
        PersonaOption(value="P3", label="Parenting Writer (P3)", topic_mode="parenting"),
        PersonaOption(value="P4", label="Finance Insight (P4)", topic_mode="finance"),
    ]

    topic_modes = [
        TopicModeOption(value="cafe", label="Cafe"),
        TopicModeOption(value="parenting", label="Parenting"),
        TopicModeOption(value="it", label="IT"),
        TopicModeOption(value="finance", label="Finance"),
        TopicModeOption(value="economy", label="Economy (Alias)"),
    ]

    return ConfigResponse(
        api_keys=api_keys,
        personas=personas,
        topic_modes=topic_modes,
        defaults=ConfigDefaults(
            platform="naver",
            persona_id="P1",
            topic_mode="cafe",
            api_base_url="http://127.0.0.1:8000/api",
        ),
        llm=LLMRuntimeConfig(
            primary_provider=app_config.llm.primary_provider,
            primary_model=app_config.llm.primary_model,
            secondary_provider=app_config.llm.secondary_provider,
            secondary_model=app_config.llm.secondary_model,
        ),
    )
