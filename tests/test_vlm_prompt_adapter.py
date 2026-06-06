from __future__ import annotations

from modules.evaluation.vlm_prompt_adapter import build_vlm_prompt, build_vlm_prompt_parts


def test_build_vlm_prompt_appends_json_guard() -> None:
    prompt = build_vlm_prompt(provider="gemini_vlm", model="gemini-2.5-flash", base_prompt="BASE")
    assert "BASE" in prompt
    assert "JSON" in prompt.upper()


def test_build_vlm_prompt_parts_normalizes_provider_suffix() -> None:
    parts = build_vlm_prompt_parts(provider="qwen_vlm", model="qwen-vl-plus", base_prompt="A")
    assert parts["response_guard"] == "json_only"
    assert "json" in parts["user_prompt_suffix"].lower()
