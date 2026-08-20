"""Ephemeral Claude MCP context for host-owned Anchor synthesis."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from ...agent.contracts import AgentTask, RuleGenerationAuthority
from ...models.analysis import GroundingPolicy, RootCauseAnalysis
from ...models.anchors import AnchorPlan, AnchorSynthesisResult
from ...mining.synthesis import (
    AnchorPlanError,
    AnchorSynthesisLimitError,
    AnchorSynthesisSession,
)
from ...utils.log import RuntimeLog
from ...utils.workspace import atomic_write_json
from .config import ClaudeCodeConfig
from .process import clip, redact

ClaudeSynthesisHandler = Callable[[AnchorPlan], Awaitable[list[AnchorSynthesisResult]]]


class ClaudeSynthesisHostContext(BaseModel):
    """Minimal private payload needed to launch same-runtime child Agents."""

    model_config = ConfigDict(extra="forbid")

    workspace_root: Path
    source_root: Path
    cases_dir: Path
    grounding_policy: GroundingPolicy
    root_cause: RootCauseAnalysis
    receipt_path: Path
    failure_path: Path
    executable: str
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    display_name: str
    project_root: Path
    mcp_python: Path | None = None
    default_timeout_seconds: float
    terminate_grace_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_repair_attempts: int
    max_repair_payload_chars: int

    @classmethod
    def from_parent(
        cls,
        task: AgentTask[Any],
        config: ClaudeCodeConfig,
        *,
        receipt_path: Path,
        failure_path: Path,
        executable: str,
        model_id: str,
    ) -> ClaudeSynthesisHostContext:
        authority = cast(RuleGenerationAuthority, task.authority)
        return cls(
            workspace_root=task.workspace_root,
            source_root=authority.source_root,
            cases_dir=authority.cases_dir,
            grounding_policy=authority.grounding_policy,
            root_cause=authority.root_cause,
            receipt_path=receipt_path,
            failure_path=failure_path,
            executable=executable,
            model=model_id,
            effort=config.effort,
            display_name=config.display_name,
            project_root=config.project_root,
            mcp_python=config.mcp_python,
            default_timeout_seconds=config.default_timeout_seconds,
            terminate_grace_seconds=config.terminate_grace_seconds,
            max_stdout_bytes=config.max_stdout_bytes,
            max_stderr_bytes=config.max_stderr_bytes,
            max_repair_attempts=config.max_repair_attempts,
            max_repair_payload_chars=config.max_repair_payload_chars,
        )

    def runtime_config(self) -> ClaudeCodeConfig:
        return ClaudeCodeConfig(
            executable=self.executable,
            model=self.model,
            effort=self.effort,
            display_name=self.display_name,
            project_root=self.project_root,
            mcp_python=self.mcp_python,
            default_timeout_seconds=self.default_timeout_seconds,
            terminate_grace_seconds=self.terminate_grace_seconds,
            max_stdout_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
            max_repair_attempts=self.max_repair_attempts,
            max_repair_payload_chars=self.max_repair_payload_chars,
        )


def load_claude_synthesis_handler(path: Path) -> ClaudeSynthesisHandler:
    try:
        context = ClaudeSynthesisHostContext.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid Claude synthesis context: {exc}") from exc

    from .runtime import ClaudeCodeRuntime

    runtime = ClaudeCodeRuntime(
        context.runtime_config(),
        runtime_log=RuntimeLog(emit_console=False, ansi_transport=True),
    )
    authority = RuleGenerationAuthority(
        source_root=context.source_root,
        cases_dir=context.cases_dir,
        grounding_policy=context.grounding_policy,
        root_cause=context.root_cause,
    )
    session = AnchorSynthesisSession(authority, workspace_root=context.workspace_root, execute=runtime.run)

    async def synthesize(plan: AnchorPlan) -> list[AnchorSynthesisResult]:
        try:
            results = await session.synthesize(plan)
        except (AnchorPlanError, AnchorSynthesisLimitError):
            raise
        except Exception as exc:
            atomic_write_json(
                context.failure_path,
                {
                    "type": type(exc).__name__,
                    "message": redact(clip(str(exc), 2_000)),
                },
            )
            raise
        assert session.receipt is not None
        atomic_write_json(context.receipt_path, session.receipt.model_dump(mode="json"))
        return results

    return synthesize


__all__ = ["ClaudeSynthesisHandler", "ClaudeSynthesisHostContext", "load_claude_synthesis_handler"]
