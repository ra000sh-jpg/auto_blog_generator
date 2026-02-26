"""런타임 이미지 생성기 팩토리."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ..automation.job_store import JobStore
from ..config import AppConfig
from ..llm.llm_router import LLMRouter, normalize_image_ai_quota
from ..llm.provider_factory import create_client as create_llm_client
from .fal_image_client import FalFluxImageClient
from .image_generator import ImageGenerator
from .openai_image_client import OpenAIImageClient
from .pexels_client import PexelsImageClient
from .pollinations_client import PollinationsImageClient
from .styles import TOPIC_CONTENT_STYLE, TOPIC_THUMBNAIL_STYLE
from .together_client import TogetherImageClient

logger = logging.getLogger(__name__)


def _normalize_image_engine_id(engine_id: str) -> str:
    """레거시 엔진 식별자를 현재 엔진 ID로 정규화한다."""
    normalized = str(engine_id or "").strip().lower()
    alias_map = {
        "mixed": "together_flux",
        "ai_only": "together_flux",
        "dalle3": "openai_dalle3",
    }
    return alias_map.get(normalized, normalized or "pexels")


def _build_primary_image_client(
    *,
    engine_id: str,
    image_api_keys: dict[str, str],
    text_api_keys: dict[str, str],
    app_config: AppConfig,
) -> Optional[Any]:
    """라우터 엔진 선택값에 맞는 primary 이미지 클라이언트를 생성한다."""
    common_kwargs = {
        "timeout_sec": app_config.llm.timeout_sec,
        "output_dir": app_config.images.output_dir,
    }

    if engine_id == "pexels":
        key = str(image_api_keys.get("pexels", "")).strip()
        if not key:
            return None
        return PexelsImageClient(api_key=key, **common_kwargs)

    if engine_id == "together_flux":
        key = str(image_api_keys.get("together", "")).strip()
        if not key:
            return None
        return TogetherImageClient(api_key=key, **common_kwargs)

    if engine_id == "fal_flux":
        key = str(image_api_keys.get("fal", "")).strip()
        if not key:
            return None
        return FalFluxImageClient(api_key=key, **common_kwargs)

    if engine_id == "openai_dalle3":
        key = str(image_api_keys.get("openai_image", "")).strip() or str(
            text_api_keys.get("openai", "")
        ).strip()
        if not key:
            return None
        return OpenAIImageClient(api_key=key, model="dall-e-3", **common_kwargs)

    return None


def _resolve_ai_quota_for_topic(
    *,
    saved: dict[str, Any],
    image_plan: dict[str, Any],
    topic_mode: str,
) -> str:
    """topic_mode override를 반영한 AI 이미지 quota를 계산한다."""
    base_quota = normalize_image_ai_quota(
        image_plan.get("ai_quota", saved.get("image_ai_quota", "0")),
        default="0",
    )
    raw_overrides = saved.get("image_topic_quota_overrides", {})
    if not isinstance(raw_overrides, dict):
        return base_quota
    topic_key = str(topic_mode or "").strip().lower()
    if not topic_key:
        return base_quota
    if topic_key not in raw_overrides:
        return base_quota
    return normalize_image_ai_quota(raw_overrides.get(topic_key), default=base_quota)


def _resolve_base_ai_quota(
    *,
    saved: dict[str, Any],
    image_plan: dict[str, Any],
) -> str:
    """저장 설정 기준 기본 AI quota를 반환한다."""
    return normalize_image_ai_quota(
        image_plan.get("ai_quota", saved.get("image_ai_quota", "0")),
        default="0",
    )


def build_runtime_image_generator(
    *,
    app_config: AppConfig,
    job_store: JobStore,
    topic_mode: str = "cafe",
) -> Optional[ImageGenerator]:
    """DB 라우터 설정을 반영한 런타임 ImageGenerator를 생성한다."""
    if not app_config.images.enabled:
        logger.info("Image pipeline disabled by config.images.enabled=false")
        return None

    router = LLMRouter(job_store=job_store, llm_config=app_config.llm)
    saved = router.get_saved_settings()
    plan = router.build_plan()
    image_plan = dict(plan.get("image", {}))

    image_enabled = bool(image_plan.get("enabled", True))
    images_per_post = int(image_plan.get("images_per_post", app_config.images.max_content_images) or 0)
    ai_quota = _resolve_base_ai_quota(saved=saved, image_plan=image_plan)
    effective_quota_for_init_topic = _resolve_ai_quota_for_topic(
        saved=saved,
        image_plan=image_plan,
        topic_mode=topic_mode,
    )
    logger.info(
        "Runtime image quota resolved",
        extra={
            "topic_mode": topic_mode,
            "base_ai_quota": ai_quota,
            "effective_ai_quota": effective_quota_for_init_topic,
        },
    )
    if (not image_enabled) or images_per_post <= 0:
        logger.info(
            "Image pipeline disabled by router settings (enabled=%s, images_per_post=%s)",
            image_enabled,
            images_per_post,
        )
        return None

    image_engine = _normalize_image_engine_id(
        str(
            image_plan.get(
                "ai_engine",
                saved.get(
                    "image_ai_engine",
                    image_plan.get("engine", saved.get("image_engine", "pexels")),
                ),
            )
        )
    )
    strategy_override = str(os.getenv("IMAGE_CONTENT_STRATEGY_OVERRIDE", "")).strip().lower() or None
    disable_stock = str(os.getenv("IMAGE_DISABLE_STOCK", "false")).strip().lower() in {"1", "true", "yes", "on"}
    raw_daily_limit = str(os.getenv("IMAGE_FREE_TIER_DAILY_LIMIT", "0")).strip()
    try:
        free_tier_daily_limit = int(raw_daily_limit or 0)
    except ValueError:
        free_tier_daily_limit = 0
    text_api_keys = dict(saved.get("text_api_keys", {}))
    image_api_keys = dict(saved.get("image_api_keys", {}))

    primary_client = _build_primary_image_client(
        engine_id=image_engine,
        image_api_keys=image_api_keys,
        text_api_keys=text_api_keys,
        app_config=app_config,
    )
    if primary_client is None:
        # 키 누락/엔진 미지원 시에도 공정이 멈추지 않도록 무료 엔진으로 폴백한다.
        primary_client = PollinationsImageClient(
            model=app_config.images.model,
            timeout_sec=app_config.llm.timeout_sec,
            output_dir=app_config.images.output_dir,
        )
        logger.info("Image primary client fallback to Pollinations (engine=%s)", image_engine)

    fallback_clients: list[Any] = []
    if not isinstance(primary_client, PollinationsImageClient):
        fallback_clients.append(
            PollinationsImageClient(
                model=app_config.images.model,
                timeout_sec=app_config.llm.timeout_sec,
                output_dir=app_config.images.output_dir,
            )
        )

    together_key = str(image_api_keys.get("together", "")).strip()
    if together_key and not isinstance(primary_client, TogetherImageClient):
        fallback_clients.append(
            TogetherImageClient(
                api_key=together_key,
                timeout_sec=app_config.llm.timeout_sec,
                output_dir=app_config.images.output_dir,
            )
        )

    stock_key = str(image_api_keys.get("pexels", "")).strip()
    stock_client = None
    if stock_key and not disable_stock:
        stock_client = PexelsImageClient(
            api_key=stock_key,
            timeout_sec=app_config.llm.timeout_sec,
            output_dir=app_config.images.output_dir,
        )
    elif disable_stock:
        logger.info("Stock image client disabled by IMAGE_DISABLE_STOCK=true")

    prompt_translator = None
    if app_config.llm.gemini_image_prompt_translation:
        gemini_key = str(text_api_keys.get("gemini", "")).strip()
        if gemini_key:
            try:
                prompt_translator = create_llm_client(
                    provider="gemini",
                    model=app_config.llm.gemini_model,
                    timeout_sec=30.0,
                    api_key=gemini_key,
                )
            except Exception as exc:
                logger.warning("Gemini prompt translator skipped: %s", exc)

    # 토픽 모드별 스타일 자동 선택
    # config(환경변수)에서 명시적으로 레거시 스타일이 지정된 경우 그대로 사용,
    # 그렇지 않으면 토픽 모드에 맞는 새 스타일로 자동 매핑한다.
    _legacy_thumb_styles = {"van_gogh_duotone", "neo_impressionist", "stylized_oil"}
    _legacy_content_styles = {"monet_soft", "watercolor_gentle", "minimal_illustration"}

    resolved_thumbnail_style = app_config.images.thumbnail_style
    if resolved_thumbnail_style in _legacy_thumb_styles or resolved_thumbnail_style == "van_gogh_duotone":
        resolved_thumbnail_style = TOPIC_THUMBNAIL_STYLE.get(
            topic_mode, TOPIC_THUMBNAIL_STYLE["default"]
        )

    resolved_content_style = app_config.images.content_style
    if resolved_content_style in _legacy_content_styles or resolved_content_style == "monet_soft":
        resolved_content_style = TOPIC_CONTENT_STYLE.get(
            topic_mode, TOPIC_CONTENT_STYLE["default"]
        )

    logger.info(
        "Image styles resolved: thumbnail=%s content=%s (topic=%s)",
        resolved_thumbnail_style,
        resolved_content_style,
        topic_mode,
    )

    return ImageGenerator(
        client=primary_client,
        fallback_clients=fallback_clients,
        stock_client=stock_client,
        thumbnail_style=resolved_thumbnail_style,
        content_style=resolved_content_style,
        thumbnail_size=app_config.images.thumbnail_size,
        content_size=app_config.images.content_size,
        max_content_images=images_per_post,
        prompt_translator=prompt_translator,
        parallel=True,
        topic_mode=topic_mode,
        content_strategy_override=strategy_override,
        ai_image_quota=ai_quota,
        ai_topic_quota_overrides=dict(saved.get("image_topic_quota_overrides", {})),
        ai_engine_id=image_engine,
        free_tier_daily_limit=free_tier_daily_limit,
        job_store=job_store,
    )
