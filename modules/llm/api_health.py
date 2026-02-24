"""LLM API 상태 점검 유틸."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List, Optional, Tuple

from ..config import LLMConfig, load_config
from .base_client import BaseLLMClient
from .provider_factory import create_client

PING_TIMEOUT_SEC = 3.0


def _build_targets(config: LLMConfig, skip_expensive: bool = True) -> List[Tuple[str, str]]:
    """헬스 체크 대상 provider/model 목록을 구성한다."""
    targets: List[Tuple[str, str]] = [
        (config.primary_provider, config.primary_model),
        (config.secondary_provider, config.secondary_model),
    ]

    if not skip_expensive:
        tertiary_providers = [item.strip() for item in config.tertiary_providers.split(",") if item.strip()]
        tertiary_models = [item.strip() for item in config.tertiary_models.split(",") if item.strip()]
        for idx, provider in enumerate(tertiary_providers):
            model = tertiary_models[idx] if idx < len(tertiary_models) else ""
            targets.append((provider, model))

    # 중복 provider/model 조합은 한 번만 검사한다.
    unique: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for provider, model in targets:
        key = (provider.lower(), model)
        if key in seen:
            continue
        seen.add(key)
        unique.append((provider, model))
    return unique


async def _maybe_close_client(client: BaseLLMClient) -> None:
    """클라이언트가 close 메서드를 제공하면 안전하게 닫는다."""
    close_fn = getattr(client, "close", None)
    if close_fn is None:
        return
    result = close_fn()
    if inspect.isawaitable(result):
        await result


async def _check_single(
    client: BaseLLMClient,
    timeout_sec: float = PING_TIMEOUT_SEC,
    close_client: bool = False,
) -> Dict[str, Any]:
    """단일 provider에 ping 요청을 보내 상태를 확인한다."""
    provider = client.provider_name
    model = str(getattr(client, "model", "unknown"))
    try:
        await asyncio.wait_for(
            client.generate(
                system_prompt="You are a health check assistant.",
                user_prompt="ping",
                temperature=0.0,
                max_tokens=1,
            ),
            timeout=timeout_sec,
        )
        return {
            "provider": provider,
            "model": model,
            "status": "OK",
            "message": "Ping success",
            "ok": True,
            "error": "",
        }
    except Exception as exc:
        error_message = f"{exc.__class__.__name__}: {exc}"
        return {
            "provider": provider,
            "model": model,
            "status": "FAIL",
            "message": error_message,
            "ok": False,
            "error": error_message,
        }
    finally:
        if close_client:
            await _maybe_close_client(client)


async def check_all_providers(
    skip_expensive: bool = True,
    llm_config: Optional[LLMConfig] = None,
    api_keys: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """등록된 provider들의 API 상태를 순회 점검한다."""
    config = llm_config or load_config().llm
    rows: List[Dict[str, str]] = []

    for provider, model in _build_targets(config=config, skip_expensive=skip_expensive):
        try:
            api_key = api_keys.get(provider.lower()) if api_keys else None
            client = create_client(
                provider=provider,
                model=model or None,
                timeout_sec=PING_TIMEOUT_SEC,
                max_tokens=1,
                api_key=api_key,
            )
        except Exception as exc:
            rows.append(
                {
                    "provider": provider,
                    "model": model or "unknown",
                    "status": "FAIL",
                    "message": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue

        result = await _check_single(client, timeout_sec=PING_TIMEOUT_SEC, close_client=True)
        rows.append(
            {
                "provider": str(result.get("provider", provider)),
                "model": str(result.get("model", model or "unknown")),
                "status": str(result.get("status", "FAIL")),
                "message": str(result.get("message", "")),
            }
        )

    return rows


async def check_api_health(
    primary_client: BaseLLMClient,
    secondary_client: BaseLLMClient,
) -> Dict[str, Dict[str, Any]]:
    """기존 호출부 호환을 위한 2-provider 헬스체크 함수."""
    primary_result = await _check_single(primary_client, close_client=False)
    secondary_result = await _check_single(secondary_client, close_client=False)

    return {
        "primary": primary_result,
        "secondary": secondary_result,
        primary_result["provider"]: primary_result,
        secondary_result["provider"]: secondary_result,
    }
