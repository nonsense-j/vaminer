"""Small process configuration for the Claude Code Adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...utils.config import PROJECT_ROOT

COMMAND = (os.getenv("CLAUDE_CODE_COMMAND") or "claude").strip()
MODEL = (os.getenv("CLAUDE_CODE_MODEL") or "").strip() or None
EFFORT = (os.getenv("CLAUDE_CODE_EFFORT") or "").strip() or None
NAME = (os.getenv("CLAUDE_CODE_NAME") or "Claude").strip()
TIMEOUT_SECONDS = int(os.getenv("CLAUDE_CODE_TIMEOUT_SECONDS") or "1800")
MAX_OUTPUT_BYTES = int(os.getenv("CLAUDE_CODE_MAX_OUTPUT_BYTES") or str(16 * 1024 * 1024))
LANGFUSE_CLAUDE_PLUGIN_ID = "langfuse-observability@langfuse-observability"
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def is_codeagent_executable(executable: str | Path) -> bool:
    return "codeagent" in Path(executable).stem.casefold()


def default_config_dir_name(executable: str | Path) -> str:
    return ".cac" if is_codeagent_executable(executable) else ".claude"


@dataclass(frozen=True, slots=True)
class ClaudeCodeConfig:
    executable: str | Path = "claude"
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    project_root: Path = PROJECT_ROOT
    mcp_python: Path | None = None
    default_timeout_seconds: float = 600.0
    terminate_grace_seconds: float = 2.0
    max_stdout_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 2 * 1024 * 1024
    max_repair_attempts: int = 2
    max_repair_payload_chars: int = 50_000
    display_name: str = NAME

    @property
    def is_codeagent(self) -> bool:
        return is_codeagent_executable(self.executable)

    @property
    def config_dir_name(self) -> str:
        return default_config_dir_name(self.executable)

    def __post_init__(self) -> None:
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
        if self.effort is not None and self.effort not in _EFFORTS:
            raise ValueError(f"unsupported Claude effort: {self.effort!r}")
        display_name = self.display_name.strip()
        if not display_name or "\n" in display_name or "\r" in display_name:
            raise ValueError("display_name must be non-empty and single-line")
        object.__setattr__(self, "display_name", display_name)
        project_root = self.project_root.expanduser().resolve()
        if not project_root.is_dir():
            raise ValueError(f"project_root is not an existing directory: {project_root}")
        object.__setattr__(self, "project_root", project_root)
        if self.mcp_python is not None:
            object.__setattr__(self, "mcp_python", self.mcp_python.expanduser().absolute())
