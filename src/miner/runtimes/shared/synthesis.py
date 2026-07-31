"""Runtime-neutral asynchronous delegation for AST-Grep Synthesizer tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ...agent.contracts import (
    AgentRunResult,
    AgentTask,
    RunLimits,
    SkillSpec,
)
from ...mining.tasks import make_ast_grep_synthesis_task
from ...models.analysis import AnalysisSubject, RootCauseAnalysis
from ...models.anchors import AnchorIntent, AnchorSynthesisRequest, AnchorSynthesisRunResult
from ...utils.config import MINER_AST_GREP_MAX_PARALLEL_RUNS

SynthesisExecutor = Callable[
    [AgentTask[AnchorSynthesisRunResult]],
    Awaitable[AgentRunResult[AnchorSynthesisRunResult]],
]


class AnchorSynthesisContext(BaseModel):
    """Serializable parent-owned context for runtime-native child tasks."""

    model_config = ConfigDict(extra="forbid")

    workspace_root: Path
    output_root: Path | None = None
    input_id: str | None = None
    trace_id: str | None = None
    source_root: Path
    repo_path: Path | None = None
    cases_dir: Path
    root_cause: RootCauseAnalysis
    analysis_subject: AnalysisSubject
    skill_root: Path
    model_hint: str | None = None
    request_limit: int | None = None
    timeout_seconds: float | None = None
    output_retries: int = 2

    @classmethod
    def from_task(cls, task: AgentTask[Any]) -> "AnchorSynthesisContext":
        """Extract the authority needed to construct per-intent child tasks."""

        context = task.context
        if (
            context.source_root is None
            or context.cases_dir is None
            or context.root_cause is None
            or context.analysis_subject is None
        ):
            raise ValueError(
                "rule generation requires source, cases, RCA, and "
                "analysis-subject context"
            )
        skill = next((item for item in task.skills if item.name == "ast-grep"), None)
        if skill is None:
            raise ValueError("rule generation requires the ast-grep skill")
        configured_limits = task.metadata.get("synthesizer_limits", {})
        request_limit = configured_limits.get("request_limit")
        timeout_seconds = configured_limits.get("timeout_seconds")
        output_retries = configured_limits.get("output_retries", 2)
        return cls(
            workspace_root=context.workspace_root.resolve(),
            output_root=(
                context.output_root.resolve()
                if context.output_root is not None
                else None
            ),
            input_id=context.input_id,
            trace_id=context.trace_id,
            source_root=context.source_root.resolve(),
            repo_path=(
                context.repo_path.resolve()
                if context.repo_path is not None
                else None
            ),
            cases_dir=context.cases_dir.resolve(),
            root_cause=context.root_cause,
            analysis_subject=context.analysis_subject,
            skill_root=skill.root.resolve(),
            model_hint=task.model_hint,
            request_limit=request_limit,
            timeout_seconds=timeout_seconds,
            output_retries=output_retries,
        )

    def validate_directories(self) -> None:
        """Reject stale or out-of-workspace task roots before delegation."""

        workspace = self.workspace_root.resolve()
        for label, value in (
            ("workspace_root", workspace),
            ("source_root", self.source_root.resolve()),
            ("cases_dir", self.cases_dir.resolve()),
            ("skill_root", self.skill_root.resolve()),
        ):
            if not value.is_dir():
                raise ValueError(f"{label} is not an existing directory: {value}")
        for label, value in (
            ("source_root", self.source_root.resolve()),
            ("cases_dir", self.cases_dir.resolve()),
        ):
            try:
                value.relative_to(workspace)
            except ValueError as exc:
                raise ValueError(
                    f"{label} must stay inside workspace_root: {value}"
                ) from exc

    def child_task(
        self,
        request: AnchorSynthesisRequest,
        intent: AnchorIntent,
    ) -> AgentTask[AnchorSynthesisRunResult]:
        """Construct the same semantic child task for any selected runtime."""

        child_trace_id = (
            f"{self.trace_id}-{intent.id}"
            if self.trace_id
            else f"ast-grep-synthesis-{intent.id}"
        )
        return make_ast_grep_synthesis_task(
            request,
            intent,
            workspace_root=self.workspace_root,
            source_root=self.source_root,
            repo_path=self.repo_path,
            cases_dir=self.cases_dir,
            analysis_subject=self.analysis_subject,
            output_root=self.output_root,
            input_id=self.input_id,
            trace_id=child_trace_id,
            model_hint=self.model_hint,
            skill=SkillSpec(name="ast-grep", root=self.skill_root),
            limits=RunLimits(
                request_limit=self.request_limit,
                timeout_seconds=self.timeout_seconds,
                output_retries=self.output_retries,
            ),
        )


@dataclass(frozen=True)
class AnchorSynthesisBatch:
    """Ordered synthesis outputs and their runtime-native child run records."""

    results: tuple[AnchorSynthesisRunResult, ...]
    runs: tuple[AgentRunResult[AnchorSynthesisRunResult], ...]
    events: tuple[dict[str, str], ...]


class AnchorSynthesisDelegator:
    """Fan out child tasks through one injected runtime executor."""

    def __init__(
        self,
        *,
        context: AnchorSynthesisContext,
        execute: SynthesisExecutor,
        max_parallel: int = MINER_AST_GREP_MAX_PARALLEL_RUNS,
    ) -> None:
        context.validate_directories()
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self._context = context
        self._execute = execute
        self._max_parallel = max_parallel

    async def synthesize(
        self,
        request: AnchorSynthesisRequest,
    ) -> AnchorSynthesisBatch:
        """Execute one runtime-native child task per intent and preserve order."""

        semaphore = asyncio.Semaphore(self._max_parallel)

        async def synthesize_one(
            intent: AnchorIntent,
        ) -> AgentRunResult[AnchorSynthesisRunResult]:
            task = self._context.child_task(request, intent)
            async with semaphore:
                return await self._execute(task)

        runs = tuple(
            await asyncio.gather(
                *(synthesize_one(intent) for intent in request.anchor_intents)
            )
        )
        results = tuple(run.output for run in runs)
        events = tuple(
            {
                "intent_id": intent.id,
                "runtime_id": run.runtime_id,
                "model_id": run.model_id,
                "status": "completed",
            }
            for intent, run in zip(request.anchor_intents, runs, strict=True)
        )
        return AnchorSynthesisBatch(
            results=results,
            runs=runs,
            events=events,
        )


__all__ = [
    "AnchorSynthesisBatch",
    "AnchorSynthesisContext",
    "AnchorSynthesisDelegator",
    "SynthesisExecutor",
]
