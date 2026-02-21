"""런타임 이미지 생성기 팩토리."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ..automation.job_store import JobStore
from ..config import AppConfig
from ..llm.llm_router import LLMRouter
from ..llm.provider_factory import create_client as create_llm_client
from .fal_image_client import FalFluxImageClient
from .image_generator import ImageGenerator
from .openai_image_client import OpenAIImageClient
from .pexels_client import PexelsImageClient
from .pollinations_client import PollinationsImageClient
from .together_client import TogetherImageClient

logger = logging.getLogger(__name__)


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
    if (not image_enabled) or images_per_post <= 0:
        logger.info(
            "Image pipeline disabled by router settings (enabled=%s, images_per_post=%s)",
            image_enabled,
            images_per_post,
        )
        return None

    image_engine = str(image_plan.get("engine", saved.get("image_engine", "pexels"))).strip().lower()
    strategy_override = str(os.getenv("IMAGE_CONTENT_STRATEGY_OVERRIDE", "")).strip().lower() or None
    disable_stock = str(os.getenv("IMAGE_DISABLE_STOCK", "false")).strip().lower() in {"1", "true", "yes", "on"}
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

    return ImageGenerator(
        client=primary_client,
        fallback_clients=fallback_clients,
        stock_client=stock_client,
        thumbnail_style=app_config.images.thumbnail_style,
        content_style=app_config.images.content_style,
        thumbnail_size=app_config.images.thumbnail_size,
        content_size=app_config.images.content_size,
        max_content_images=images_per_post,
        prompt_translator=prompt_translator,
        parallel=True,
        topic_mode=topic_mode,
        content_strategy_override=strategy_override,
    )
