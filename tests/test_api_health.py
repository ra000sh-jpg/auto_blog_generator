import asyncio
from typing import Dict, Optional

from modules.config import LLMConfig
from modules.llm import api_health


class FakeClient:
    """API 상태 점검 테스트용 가짜 클라이언트."""

    def __init__(self, provider: str, model: str, should_fail: bool = False):
        self._provider = provider
        self.model = model
        self.should_fail = should_fail
        self.closed = False

    @property
    def provider_name(self) -> str:
        return self._provider

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1,
    ):
        del system_prompt, user_prompt, temperature, max_tokens
        if self.should_fail:
            raise RuntimeError(f"{self._provider} down")
        return {"ok": True}

    async def close(self) -> None:
        self.closed = True


def test_check_all_providers_returns_status_rows(monkeypatch):
    clients: Dict[str, FakeClient] = {
        "qwen": FakeClient("qwen", "qwen-plus", should_fail=False),
        "deepseek": FakeClient("deepseek", "deepseek-chat", should_fail=True),
    }

    def fake_create_client(
        provider: str,
        model: Optional[str] = None,
        timeout_sec: float = 3.0,
        max_tokens: int = 1,
        api_key: Optional[str] = None,
    ):
        del model, timeout_sec, max_tokens, api_key
        return clients[provider]

    monkeypatch.setattr(api_health, "create_client", fake_create_client)

    config = LLMConfig(
        primary_provider="qwen",
        primary_model="qwen-plus",
        secondary_provider="deepseek",
        secondary_model="deepseek-chat",
        tertiary_providers="",
        tertiary_models="",
    )
    rows = asyncio.run(api_health.check_all_providers(skip_expensive=True, llm_config=config))

    assert len(rows) == 2
    by_provider = {row["provider"]: row for row in rows}
    assert by_provider["qwen"]["status"] == "OK"
    assert by_provider["deepseek"]["status"] == "FAIL"
    assert "message" in by_provider["deepseek"]
    assert clients["qwen"].closed is True
    assert clients["deepseek"].closed is True


def test_check_api_health_legacy_map_compatible():
    primary = FakeClient("qwen", "qwen-plus", should_fail=False)
    secondary = FakeClient("deepseek", "deepseek-chat", should_fail=True)

    result = asyncio.run(api_health.check_api_health(primary, secondary))

    assert "primary" in result
    assert "secondary" in result
    assert "qwen" in result
    assert "deepseek" in result
    assert result["primary"]["ok"] is True
    assert result["secondary"]["ok"] is False
    assert result["secondary"]["status"] == "FAIL"
