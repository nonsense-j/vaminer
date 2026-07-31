"""Claude-native configuration for delegated AST-Grep Synthesizer CLI runs."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...agent.contracts import AgentTask, RuntimeCapability
from ...models.anchors import AnchorSynthesisRequest, AnchorSynthesisRunResult
from ..shared.synthesis import (
    AnchorSynthesisContext,
    AnchorSynthesisDelegator,
)
from .config import DEFAULT_ENV_ALLOWLIST, ClaudeCodeConfig

ClaudeSynthesisHandler = Callable[
    [AnchorSynthesisRequest],
    Awaitable[list[AnchorSynthesisRunResult]],
]


class ClaudeSynthesisHostContext(BaseModel):
    """Private payload used by the Rule Generator MCP delegation host."""

    model_config = ConfigDict(extra="forbid")

    synthesis: AnchorSynthesisContext
    executable: str
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    output_format: Literal["json", "stream-json"] = "stream-json"
    project_root: Path
    mcp_python: Path | None = None
    default_timeout_seconds: float
    terminate_grace_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_repair_attempts: int
    max_repair_payload_chars: int
    artifact_root: Path | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    capabilities: list[RuntimeCapability]

    @classmethod
    def from_parent(
        cls,
        task: AgentTask[Any],
        config: ClaudeCodeConfig,
        *,
        executable: str | None = None,
        model_id: str | None = None,
    ) -> "ClaudeSynthesisHostContext":
        """Capture the selected Claude runtime without provider-specific secrets."""

        environment = {
            name: value
            for name in DEFAULT_ENV_ALLOWLIST
            if name != "GITHUB_TOKEN"
            and (value := os.environ.get(name)) is not None
        }
        environment.update(config.environment)
        environment.pop("GITHUB_TOKEN", None)
        return cls(
            synthesis=AnchorSynthesisContext.from_task(task),
            executable=executable or os.fspath(config.executable),
            model=model_id or task.model_hint or config.model,
            effort=config.effort,
            output_format=config.output_format,
            project_root=config.project_root,
            mcp_python=config.mcp_python,
            default_timeout_seconds=config.default_timeout_seconds,
            terminate_grace_seconds=config.terminate_grace_seconds,
            max_stdout_bytes=config.max_stdout_bytes,
            max_stderr_bytes=config.max_stderr_bytes,
            max_repair_attempts=config.max_repair_attempts,
            max_repair_payload_chars=config.max_repair_payload_chars,
            artifact_root=config.artifact_root,
            environment=environment,
            capabilities=sorted(config.capabilities, key=lambda item: item.value),
        )

    def runtime_config(self) -> ClaudeCodeConfig:
        """Recreate only Claude adapter configuration for child CLI processes."""

        return ClaudeCodeConfig(
            executable=self.executable,
            model=self.model,
            effort=self.effort,
            output_format=self.output_format,
            project_root=self.project_root,
            mcp_python=self.mcp_python,
            default_timeout_seconds=self.default_timeout_seconds,
            terminate_grace_seconds=self.terminate_grace_seconds,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
            max_repair_attempts=self.max_repair_attempts,
            max_repair_payload_chars=self.max_repair_payload_chars,
            artifact_root=self.artifact_root,
            environment=self.environment,
            capabilities=frozenset(self.capabilities),
        )


def load_claude_synthesis_handler(path: Path) -> ClaudeSynthesisHandler:
    """Load one private host payload and return a Claude-CLI-only handler."""

    try:
        context = ClaudeSynthesisHostContext.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid Claude synthesis context: {exc}") from exc

    # Imported lazily to avoid the runtime -> policy -> MCP import cycle.
    from .runtime import ClaudeCodeRuntime

    runtime = ClaudeCodeRuntime(context.runtime_config())
    delegator = AnchorSynthesisDelegator(
        context=context.synthesis,
        execute=runtime.run,
    )

    async def synthesize(
        request: AnchorSynthesisRequest,
    ) -> list[AnchorSynthesisRunResult]:
        batch = await delegator.synthesize(request)
        return list(batch.results)

    return synthesize


__all__ = [
    "ClaudeSynthesisHandler",
    "ClaudeSynthesisHostContext",
    "load_claude_synthesis_handler",
]
