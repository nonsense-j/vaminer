"""Provider-level tests for the shared Miner LLM."""

from __future__ import annotations

import pytest

from src.miner.runtimes.pydantic import llm
from src.miner.runtimes.pydantic.llm import get_llm


@pytest.fixture(autouse=True)
def clear_model_cache():
    get_llm.cache_clear()
    yield
    get_llm.cache_clear()


@pytest.mark.parametrize(
    ("provider", "model_name", "expected_system"),
    [
        ("openai", "gpt-5.2", "openai"),
        ("openai-compatible", "compatible-model", "openai"),
    ],
)
def test_supported_providers_build_one_reused_model(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_name: str,
    expected_system: str,
):
    monkeypatch.setattr(llm, "LLM_PROVIDER", provider)
    monkeypatch.setattr(llm, "LLM_MODEL", model_name)
    monkeypatch.setattr(llm, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "OPENAI_BASE_URL", "https://example.invalid/v1")

    model = get_llm()

    assert model is get_llm()
    assert model.system == expected_system
    assert model.model_name == model_name
    if provider == "openai-compatible":
        assert model.profile["openai_supports_strict_tool_definition"] is False
        assert model.profile["openai_chat_supports_multiple_system_messages"] is False
    elif provider == "openai":
        assert model.profile.get("openai_supports_strict_tool_definition") is not False
