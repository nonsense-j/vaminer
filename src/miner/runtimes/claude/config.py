"""Environment defaults owned by the Claude adapter."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from ...agent.contracts import RuntimeCapability
from ...utils.config import MINER_AST_GREP_MAX_PARALLEL_RUNS, PROJECT_ROOT

COMMAND = (os.getenv("CLAUDE_CODE_COMMAND") or "claude").strip()
MODEL = (os.getenv("CLAUDE_CODE_MODEL") or "").strip() or None
TIMEOUT_SECONDS = int(os.getenv("CLAUDE_CODE_TIMEOUT_SECONDS") or "1800")
MAX_OUTPUT_BYTES = int(os.getenv("CLAUDE_CODE_MAX_OUTPUT_BYTES") or str(16 * 1024 * 1024))

ClaudeOutputFormat = Literal["json", "stream-json"]

_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
    "SHELL",
    "XDG_CONFIG_HOME",
    "CLAUDE_CONFIG_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "GITHUB_TOKEN",
)
FORBIDDEN_MODEL_AUTH_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_REASONING_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)


@dataclass(frozen=True)
class ClaudeCodeConfig:
    """Process, isolation, and protocol settings for the Claude adapter."""

    executable: str | Path = "claude"
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    output_format: ClaudeOutputFormat = "stream-json"
    project_root: Path = PROJECT_ROOT
    mcp_python: Path | None = None
    plugin_name: str = "vaminer"
    permission_mode: Literal["dontAsk"] = "dontAsk"
    default_timeout_seconds: float = 600.0
    terminate_grace_seconds: float = 2.0
    max_stdout_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 2 * 1024 * 1024
    max_repair_attempts: int = 2
    max_repair_payload_chars: int = 50_000
    artifact_root: Path | None = None
    max_subagents_per_session: int = 8
    max_concurrent_subagents: int = MINER_AST_GREP_MAX_PARALLEL_RUNS
    max_subagent_depth: int = 1
    environment: Mapping[str, str] = field(default_factory=dict)
    capabilities: frozenset[RuntimeCapability] = frozenset(
        {
            RuntimeCapability.STRUCTURED_OUTPUT,
            RuntimeCapability.ISSUE_RESEARCH,
            RuntimeCapability.WEB_RESEARCH,
            RuntimeCapability.REPOSITORY_CHECKOUT,
            RuntimeCapability.WORKSPACE_READ,
            RuntimeCapability.WORKSPACE_WRITE,
            RuntimeCapability.FIXED_DIFF,
            RuntimeCapability.AST_GREP,
            RuntimeCapability.SKILLS,
            RuntimeCapability.AGENT_DELEGATION,
        }
    )

    def __post_init__(self) -> None:
        forbidden_environment = sorted(FORBIDDEN_MODEL_AUTH_ENV.intersection(self.environment))
        if forbidden_environment:
            raise ValueError(
                "Claude model provider and authentication must come from the Claude user session; "
                f"custom environment contains forbidden keys: {', '.join(forbidden_environment)}"
            )
        if not _PLUGIN_NAME_RE.fullmatch(self.plugin_name):
            raise ValueError("plugin_name must contain only lowercase letters, digits, and hyphens")
        if self.plugin_name != "vaminer":
            raise ValueError("the invocation-scoped plugin name is fixed as 'vaminer'")
        if self.default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        if self.terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds must be non-negative")
        if self.max_stdout_bytes < 1 or self.max_stderr_bytes < 1:
            raise ValueError("stdout and stderr limits must be positive")
        if self.max_repair_attempts < 0 or self.max_repair_attempts > 2:
            raise ValueError("max_repair_attempts must be between zero and two")
        if self.max_repair_payload_chars < 1:
            raise ValueError("max_repair_payload_chars must be positive")
        if self.max_subagents_per_session < 1:
            raise ValueError("max_subagents_per_session must be positive")
        if self.max_concurrent_subagents < 1:
            raise ValueError("max_concurrent_subagents must be positive")
        if self.max_subagent_depth != 1:
            raise ValueError("VAMiner permits exactly one subagent layer")
        project_root = self.project_root.expanduser().resolve()
        if not project_root.is_dir():
            raise ValueError(f"project_root is not an existing directory: {project_root}")
        object.__setattr__(self, "project_root", project_root)
        if self.mcp_python is not None:
            object.__setattr__(self, "mcp_python", self.mcp_python.expanduser().absolute())
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
