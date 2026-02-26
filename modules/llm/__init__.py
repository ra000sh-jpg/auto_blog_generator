"""PipelineService에서 사용하는 LLM 생성기 어댑터."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from ..automation.job_store import JobStore
from ..automation.job_store import Job
from ..config import LLMConfig, SEOConfig
from .circuit_breaker import ProviderCircuitBreaker
from ..seo.tag_generator import TagGenerator
from .content_generator import ContentGenerator, ContentResult
from .llm_router import LLMRouter
from .provider_factory import create_client

logger = logging.getLogger(__name__)

_generator: Optional[ContentGenerator] = None
_tag_generator: Optional[TagGenerator] = None
_runtime_config: Optional[LLMConfig] = None
_runtime_job_store: Optional[JobStore] = None
_runtime_notifier: Optional[Any] = None


def _build_generator(
    config: LLMConfig,
    *,
    job_store: Optional[JobStore] = None,
    notifier: Optional[Any] = None,
    job: Optional[Job] = None,
) -> ContentGenerator:
    """설정 기반 ContentGenerator 인스턴스를 생성한다."""
    router = LLMRouter(job_store=job_store, llm_config=config)
    if job is None:
        generation_plan = router.build_generation_plan()
    else:
        generation_plan = router.build_generation_plan_for_job(job=job)

    def _build_client_from_spec(spec: Dict[str, Any]) -> Optional[Any]:
        provider = str(spec.get("provider", "")).strip().lower()
        model = str(spec.get("model", "")).strip()
        api_key = str(spec.get("api_key", "")).strip()
        if not provider or not model:
            return None
        try:
            return create_client(
                provider=provider,
                model=model,
                timeout_sec=config.timeout_sec,
                max_tokens=config.max_tokens,
                api_key=api_key or None,
            )
        except Exception as exc:
            logger.warning("LLM router spec skipped: %s/%s (%s)", provider, model, exc)
            return None

    quality_step = dict(generation_plan.get("quality_step", {}))
    voice_step = dict(generation_plan.get("voice_step", {}))

    primary_client = _build_client_from_spec(quality_step)
    if primary_client is None:
        primary_client = create_client(
            provider=config.primary_provider,
            model=config.primary_model,
            timeout_sec=config.timeout_sec,
            max_tokens=config.max_tokens,
        )

    fallback_specs = list(quality_step.get("fallback_chain", []))
    fallback_clients = []
    for item in fallback_specs:
        built = _build_client_from_spec(dict(item))
        if built is None:
            continue
        if any(existing.provider_name == built.provider_name for existing in fallback_clients):
            continue
        fallback_clients.append(built)

    secondary_client = fallback_clients[0] if fallback_clients else None
    if secondary_client is None:
        try:
            secondary_client = create_client(
                provider=config.secondary_provider,
                model=config.secondary_model,
                timeout_sec=config.timeout_sec,
                max_tokens=config.max_tokens,
            )
        except Exception:
            secondary_client = primary_client

    # 3순위 폴백 클라이언트 목록 (라우터 체인 + 정적 tertiary 설정 결합)
    additional_clients = fallback_clients[1:]
    providers = [p.strip() for p in config.tertiary_providers.split(",") if p.strip()]
    models = [m.strip() for m in config.tertiary_models.split(",") if m.strip()]
    for idx, provider in enumerate(providers):
        model = models[idx] if idx < len(models) else None
        try:
            client = create_client(
                provider=provider,
                model=model,
                timeout_sec=config.timeout_sec,
                max_tokens=config.max_tokens,
            )
            if any(existing.provider_name == client.provider_name for existing in additional_clients):
                continue
            additional_clients.append(client)
        except Exception as exc:
            logger.warning("Tertiary provider %s skipped: %s", provider, exc)

    voice_client = _build_client_from_spec(voice_step) or secondary_client

    def _fallback_alert(payload: Dict[str, Any]) -> None:
        if not notifier:
            return
        message = str(payload.get("message", "")).strip()
        if not message:
            return
        send_background = getattr(notifier, "send_message_background", None)
        if callable(send_background):
            send_background(message, disable_notification=False)

    circuit_breaker = ProviderCircuitBreaker(
        job_store=job_store,
        notifier=notifier,
    )
    circuit_breaker.load_all_from_db(
        ["qwen", "deepseek", "gemini", "openai", "claude", "groq", "cerebras"]
    )

    return ContentGenerator(
        primary_client=primary_client,
        secondary_client=secondary_client,
        voice_client=voice_client,
        additional_clients=additional_clients,
        enable_quality_check=config.enable_quality_check,
        enable_seo_optimization=config.enable_seo_optimization,
        enable_fact_check=config.enable_fact_check,
        use_multistep=config.use_multistep,
        max_rewrites=config.max_rewrites,
        min_quality_score=config.min_quality_score,
        temperature=config.temperature,
        fallback_to_secondary=config.fallback_to_secondary,
        max_tokens=config.max_tokens,
        enable_voice_rewrite=config.enable_voice_rewrite,
        db_path=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        fallback_alert_fn=_fallback_alert,
        circuit_breaker=circuit_breaker,
    )


def get_generator(
    config: Optional[LLMConfig] = None,
    *,
    job_store: Optional[JobStore] = None,
    notifier: Optional[Any] = None,
    job: Optional[Job] = None,
) -> ContentGenerator:
    """싱글톤 ContentGenerator를 반환한다."""
    global _generator, _runtime_config, _runtime_job_store, _runtime_notifier

    resolved_config = config if config is not None else _runtime_config or LLMConfig()
    if job_store is not None:
        _runtime_job_store = job_store
    if notifier is not None:
        _runtime_notifier = notifier
    _runtime_config = resolved_config

    if job is not None:
        # 작업별 모델 라우팅(챔피언/도전자)을 반영하기 위해 매 호출 시 동적 생성
        return _build_generator(
            resolved_config,
            job_store=job_store or _runtime_job_store,
            notifier=notifier or _runtime_notifier,
            job=job,
        )

    if _generator is None:
        _generator = _build_generator(
            resolved_config,
            job_store=job_store or _runtime_job_store,
            notifier=notifier or _runtime_notifier,
        )
    return _generator


def reset_generator() -> None:
    """테스트를 위해 싱글톤을 초기화한다."""
    global _generator, _tag_generator, _runtime_config, _runtime_job_store, _runtime_notifier
    _generator = None
    _tag_generator = None
    _runtime_config = None
    _runtime_job_store = None
    _runtime_notifier = None


def get_tag_generator(config: Optional[SEOConfig] = None) -> TagGenerator:
    """싱글톤 TagGenerator를 반환한다.

    SEOConfig의 tag_llm_provider/tag_llm_model을 사용해 LLM 클라이언트를 생성한다.
    """
    global _tag_generator

    if _tag_generator is None:
        resolved_config = config if config is not None else SEOConfig()

        if not resolved_config.enable_tag_generation:
            # 태그 생성 비활성화 시 LLM 없이 폴백 모드로 생성
            _tag_generator = TagGenerator(llm_client=None)
        else:
            try:
                llm_client = create_client(
                    provider=resolved_config.tag_llm_provider,
                    model=resolved_config.tag_llm_model,
                    timeout_sec=60.0,
                    max_tokens=500,
                )
                _tag_generator = TagGenerator(llm_client=llm_client)
                logger.info(
                    "TagGenerator initialized with %s/%s",
                    resolved_config.tag_llm_provider,
                    resolved_config.tag_llm_model,
                )
            except Exception as exc:
                logger.warning("TagGenerator LLM init failed, using fallback: %s", exc)
                _tag_generator = TagGenerator(llm_client=None)

    return _tag_generator


async def llm_generate_fn(job: Job) -> Dict[str, Any]:
    """PipelineService.generate_fn과 호환되는 LLM 생성 함수."""
    try:
        generator = get_generator(job=job)
    except TypeError:
        # 테스트에서 get_generator를 단순 람다로 대체한 기존 패턴과 호환
        generator = get_generator()
    result: ContentResult = await generator.generate(job)
    return {
        "final_content": result.final_content,
        "quality_gate": result.quality_gate,
        "quality_snapshot": result.quality_snapshot,
        "seo_snapshot": result.seo_snapshot,
        "image_prompts": result.image_prompts,
        "image_placements": result.image_placements,
        "image_slots": result.image_slots,
        "raw_content": result.raw_content,
        "voice_rewrite_applied": result.voice_rewrite_applied,
        "llm_calls_used": result.llm_calls_used,
        "provider_used": result.provider_used,
        "provider_model": result.provider_model,
        "provider_fallback_from": result.provider_fallback_from,
        "generation_method": result.generation_method,
        "rewrite_count": result.rewrite_count,
        "fact_check_applied": result.fact_check_applied,
        "rag_context": result.rag_context,
        "llm_token_usage": result.llm_token_usage,
    }


__all__ = ["get_generator", "get_tag_generator", "llm_generate_fn", "reset_generator"]
