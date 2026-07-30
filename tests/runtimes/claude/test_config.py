"""Tests for Claude adapter configuration."""

import pytest

from src.miner.runtimes.claude.config import DEFAULT_ENV_ALLOWLIST, ClaudeCodeConfig
from src.miner.runtimes.claude.policy import PolicyCompiler


def test_model_provider_auth_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")

    environment = PolicyCompiler(ClaudeCodeConfig()).build_environment()

    assert "HOME" in DEFAULT_ENV_ALLOWLIST
    assert "CLAUDE_CONFIG_DIR" in DEFAULT_ENV_ALLOWLIST
    assert "ANTHROPIC_API_KEY" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert "CLAUDE_CODE_USE_BEDROCK" not in environment


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ],
)
def test_model_provider_auth_environment_overrides_are_rejected(name: str):
    with pytest.raises(ValueError, match="Claude user session"):
        ClaudeCodeConfig(environment={name: "forbidden"})
