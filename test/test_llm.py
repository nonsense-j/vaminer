"""Provider-level tests for the shared Miner model."""

from __future__ import annotations

import pytest

from src.miner.utils import llm
from src.miner.utils.llm import get_llm


@pytest.fixture(autouse=True)
def clear_model_cache():
    get_llm.cache_clear()
    yield
    get_llm.cache_clear()


@pytest.mark.parametrize(
    ("provider", "model_name", "expected_system"),
    [
        ("deepseek", "deepseek-chat", "deepseek"),
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


@pytest.mark.parametrize(
    ("provider", "model_name", "base_url", "message"),
    [
        ("", "model", "https://example.invalid/v1", "LLM_PROVIDER is required"),
        ("openai", None, None, "LLM_MODEL is required"),
        ("openai-compatible", "model", None, "OPENAI_BASE_URL is required"),
        ("unsupported", "model", None, "Unsupported LLM_PROVIDER"),
    ],
)
def test_invalid_model_configuration_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_name: str | None,
    base_url: str | None,
    message: str,
):
    monkeypatch.setattr(llm, "LLM_PROVIDER", provider)
    monkeypatch.setattr(llm, "LLM_MODEL", model_name)
    monkeypatch.setattr(llm, "OPENAI_BASE_URL", base_url)

    with pytest.raises(RuntimeError, match=message):
        get_llm()
