"""멀티 프로바이더 VLM 프롬프트 어댑터."""

from __future__ import annotations

from typing import Dict


# 기본 가드 문구: JSON 강제 응답
_DEFAULT_JSON_GUARD = (
    "Return ONLY valid JSON. Do not include markdown fences, explanation text, or any extra keys."
)

# 프로바이더별 보정 문구
_PROVIDER_SUFFIX_GUARDS = {
    "qwen": "JSON only. No markdown code block. No prose.",
    "gemini": "Strictly output a single JSON object only.",
    "groq": "Output only one JSON object without backticks.",
    "openai": "Respond with valid JSON only.",
    "nvidia": "Return valid JSON only. Do not add <think> tags.",
}


def _normalize_provider(provider: str) -> str:
    """프로바이더 문자열을 정규화한다."""
    normalized = str(provider or "").strip().lower()
    if normalized.endswith("_vlm"):
        normalized = normalized[:-4]
    return normalized


def build_vlm_prompt_parts(provider: str, model: str, base_prompt: str) -> Dict[str, str]:
    """프로바이더별 프롬프트 보정 결과를 반환한다."""
    normalized_provider = _normalize_provider(provider)
    normalized_model = str(model or "").strip().lower()

    # 모델별 예외 룰이 필요할 때 확장 가능한 기본 구조
    suffix = _PROVIDER_SUFFIX_GUARDS.get(normalized_provider, _DEFAULT_JSON_GUARD)
    if "deepseek-r1" in normalized_model:
        suffix = "Return ONLY valid JSON and never include reasoning traces."

    return {
        "system_prompt": "",
        "user_prompt_suffix": suffix,
        "response_guard": "json_only",
    }


def build_vlm_prompt(provider: str, model: str, base_prompt: str) -> str:
    """평가용 텍스트 프롬프트를 최종 조합한다."""
    parts = build_vlm_prompt_parts(provider=provider, model=model, base_prompt=base_prompt)
    suffix = str(parts.get("user_prompt_suffix", "")).strip()
    if not suffix:
        return str(base_prompt or "").strip()
    return f"{str(base_prompt or '').strip()}\n\n{suffix}"
