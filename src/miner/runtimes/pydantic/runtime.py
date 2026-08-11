"""Pydantic AI adapter for runtime-neutral Miner tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic_ai import (
    Agent,
    ModelRetry,
    RunContext,
    ToolOutput,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness import FileSystem

from ...agent.contracts import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    FileAccess,
    OutputT,
    RuntimeArtifacts,
    RuntimeCapability,
    RuntimeUsage,
)
from ...agent.instructions import compose_instructions
from .config import (
    MINER_FS_MAX_FIND_RESULTS,
    MINER_FS_MAX_READ_LINES,
    MINER_FS_MAX_SEARCH_RESULTS,
)
from ...models.anchors import AnchorSynthesisRequest, AnchorSynthesisRunResult
from ...tools.ast_grep import run_ast_grep
from ...utils.config import (
    MINER_AST_GREP_MAX_SAMPLE_SIZE,
    MINER_AST_GREP_SAMPLE_SIZE,
    MINER_AST_GREP_TIMEOUT_SECONDS,
)
from ...tools.cve import fetch_cve
from ...tools.github import fetch_github_issue, parse_commit
from .capabilities import (
    AgentCapability,
    cache_stability_capability,
    commit_history_capability,
    compaction_capability,
    local_skill_capability,
    overflow_capability,
    web_fetch_capability,
    web_search_capability,
)
from .context import MinerContext
from .llm import get_llm
from ..shared.synthesis import (
    AnchorSynthesisContext,
    AnchorSynthesisDelegator,
)
from .tools import clone_repo, read_patch_diff

_READ_ONLY_FILE_TOOLS = frozenset({"read_file", "search_files", "find_files"})
_WRITABLE_FILE_TOOLS = _READ_ONLY_FILE_TOOLS | {"write_file"}
_SKILL_REFERENCE_TOOLS = frozenset({"read_file"})
_OUTPUT_TOOL_NAMES = {
    AgentPhase.ISSUE_COLLECTION: "submit_issue_collection",
    AgentPhase.ROOT_CAUSE: "submit_root_cause",
    AgentPhase.RULE_GENERATION: "submit_vas_core",
    AgentPhase.AST_GREP_SYNTHESIS: "return_anchor_synthesis_run",
}


class PydanticAIRuntimeError(RuntimeError):
    """Base error raised by the in-process SDK adapter."""


class PydanticAIRuntimeConfigurationError(PydanticAIRuntimeError):
    """Raised when a task is missing paths or skills required by its phase."""


class PydanticAIOutputValidationError(PydanticAIRuntimeError):
    """Raised after all complete task repair attempts fail deterministic validation."""

    def __init__(self, task_id: str, errors: Sequence[str], *, attempts: int) -> None:
        self.task_id = task_id
        self.errors = tuple(errors)
        self.attempts = attempts
        super().__init__(
            f"Pydantic AI task {task_id!r} failed output validation after {attempts} attempts:\n- "
            + "\n- ".join(self.errors)
        )


def _workspace_files(
    root: Path,
    *,
    writable: bool,
    prefix: str | None = None,
) -> AbstractToolset[MinerContext]:
    toolset = (
        FileSystem[MinerContext](
            root_dir=root,
            max_read_lines=MINER_FS_MAX_READ_LINES,
            max_search_results=MINER_FS_MAX_SEARCH_RESULTS,
            max_find_results=MINER_FS_MAX_FIND_RESULTS,
        )
        .get_toolset()
        .filtered(lambda _ctx, tool: tool.name in (_WRITABLE_FILE_TOOLS if writable else _READ_ONLY_FILE_TOOLS))
    )
    return toolset.prefixed(prefix) if prefix else toolset


def _skill_reference_files(root: Path) -> AbstractToolset[MinerContext]:
    return (
        FileSystem[MinerContext](
            root_dir=root,
            allowed_patterns=("references/**",),
        )
        .get_toolset()
        .filtered(lambda _ctx, tool: tool.name in _SKILL_REFERENCE_TOOLS)
        .prefixed("skill")
    )


def _require_path(path: Path | None, label: str, phase: AgentPhase) -> Path:
    if path is None:
        raise PydanticAIRuntimeConfigurationError(f"{label} is required for phase {phase.value!r}")
    return path


def _model_id(model: Model | str) -> str:
    if isinstance(model, str):
        return model
    return str(getattr(model, "model_id", None) or getattr(model, "model_name", None) or type(model).__name__)


def _deps(task: AgentTask[Any]) -> MinerContext:
    context = task.context
    return MinerContext(
        workspace_root=context.workspace_root,
        source_root=context.source_root,
        repo_path=context.repo_path,
        cases_dir=context.cases_dir,
        root_cause=context.root_cause,
        analysis_subject=context.analysis_subject,
    )


def _runtime_binding(task: AgentTask[Any]) -> str:
    if task.phase is AgentPhase.ISSUE_COLLECTION:
        return """# Runtime Binding

## Pydantic AI

- Use `fetch_cve`, `fetch_github_issue`, and `parse_commit` for primary
  evidence.
- Use `clone_repo` to prepare the verified checkout.
- Load optional web and commit-history capabilities only when the shared
  workflow identifies a concrete evidence gap.
- Submit the final object through the active structured-output contract.
"""
    if task.phase is AgentPhase.ROOT_CAUSE:
        diff_binding = (
            "- Use `read_patch_diff` for the verified buggy-to-fixed diff."
            if RuntimeCapability.FIXED_DIFF in task.required_capabilities
            else "- `read_patch_diff` is not available for this task."
        )
        return f"""# Runtime Binding

## Pydantic AI

- Locate files or matching lines with `search_files` and `find_files`, then use
  `read_file` with the smallest useful `offset` and `limit`.
- Paths are relative to the VAS workspace: source code is under `src/`, and
  generated examples are under `cases/`.
- Create complete case files with `write_file`.
{diff_binding}
- Submit the final object through the active structured-output contract.
"""
    if task.phase is AgentPhase.RULE_GENERATION:
        return """# Runtime Binding

## Pydantic AI

- Use `find_files`, `search_files`, and `read_file` only within the cases root.
  The workspace source area is `src/`, but it is not exposed to the Rule
  Generator.
- Submit one complete `AnchorSynthesisRequest` through
  `synthesize_ast_grep_anchors`. The tool returns typed results in request
  order.
- Submit the final `VASCoreInfo` through the active structured-output contract.
"""
    if task.phase is AgentPhase.AST_GREP_SYNTHESIS:
        return """# Runtime Binding

## Pydantic AI

- Inspect any read-only file in the corresponding VAS workspace with
  `workspace_find_files`, `workspace_search_files`, and `workspace_read_file`.
  Paths passed to these tools are relative to the VAS workspace root.
- Inspect `src` only with `src_find_files`, `src_search_files`, and
  `src_read_file`. Inspect generated cases only with the corresponding
  `cases_*` tools.
- Read detailed ast-grep syntax references with `skill_read_file`.
- Execute queries only with `run_ast_grep_query`, selecting the logical target
  `cases` or `src`. The tool accepts no shell command or filesystem path.
- Submit one result through the active structured-output contract.
"""
    raise PydanticAIRuntimeConfigurationError(f"unsupported phase: {task.phase.value}")


def _compiled_instructions(task: AgentTask[Any]) -> str:
    return compose_instructions(
        task.instructions,
        input_policy=task.input_instructions,
        runtime_binding=_runtime_binding(task),
    )


class PydanticAIRuntime:
    """Execute every Miner phase through its existing Pydantic AI tool harness."""

    runtime_id = "pydantic-ai"
    capabilities = frozenset(
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

    def __init__(
        self,
        *,
        model: Model | str | None = None,
        additional_capabilities: Sequence[AgentCapability] = (),
    ) -> None:
        self._model = model
        self._additional_capabilities = tuple(additional_capabilities)

    def _resolve_model(self, task: AgentTask[Any]) -> Model | str:
        if task.model_hint is not None:
            return task.model_hint
        if self._model is not None:
            return self._model
        return get_llm()

    def model_id_for(self, task: AgentTask[Any]) -> str:
        """Return the configured SDK model identity before execution."""

        return _model_id(self._resolve_model(task))

    def build_agent(
        self,
        task: AgentTask[OutputT],
        *,
        model: Model | str,
        validation_state: list[tuple[str, ...]] | None = None,
    ) -> Agent[MinerContext, OutputT]:
        """Construct a fresh stateless SDK Agent for one task contract."""

        if task.workspace.cwd.resolve() != task.context.workspace_root.resolve():
            raise PydanticAIRuntimeConfigurationError(
                "Pydantic AI working directory must equal the task workspace root"
            )
        expected_access = {
            AgentPhase.ISSUE_COLLECTION: FileAccess.NONE,
            AgentPhase.ROOT_CAUSE: FileAccess.READ_WRITE,
            AgentPhase.RULE_GENERATION: FileAccess.READ_ONLY,
            AgentPhase.AST_GREP_SYNTHESIS: FileAccess.READ_ONLY,
        }[task.phase]
        if task.workspace.native_workspace_access is not expected_access:
            raise PydanticAIRuntimeConfigurationError(
                f"{task.phase.value} requires native workspace access " f"{expected_access.value!r}"
            )

        tools: list[Any] = []
        toolsets: list[AbstractToolset[MinerContext]] = []
        capabilities: list[AgentCapability] = [*self._additional_capabilities]
        model_settings: dict[str, Any] | None = None

        if task.phase is AgentPhase.ISSUE_COLLECTION:
            tools.extend((fetch_cve, fetch_github_issue, parse_commit, clone_repo))
            capabilities.extend(
                (
                    web_search_capability(),
                    web_fetch_capability(),
                    commit_history_capability(),
                    overflow_capability(),
                )
            )
        elif task.phase is AgentPhase.ROOT_CAUSE:
            _require_path(task.context.source_root, "source_root", task.phase)
            _require_path(task.context.cases_dir, "cases_dir", task.phase)
            if RuntimeCapability.FIXED_DIFF in task.required_capabilities:
                _require_path(task.context.repo_path, "repo_path", task.phase)
                tools.append(read_patch_diff)
            toolsets.append(_workspace_files(task.context.workspace_root, writable=True))
            capabilities.extend((overflow_capability(), compaction_capability(), cache_stability_capability()))
        elif task.phase is AgentPhase.RULE_GENERATION:
            _require_path(task.context.source_root, "source_root", task.phase)
            cases_dir = _require_path(task.context.cases_dir, "cases_dir", task.phase)
            root_cause = task.context.root_cause
            analysis_subject = task.context.analysis_subject
            if root_cause is None or analysis_subject is None:
                raise PydanticAIRuntimeConfigurationError("rule generation requires root_cause and analysis_subject")
            try:
                synthesis_context = AnchorSynthesisContext.from_task(task)
            except ValueError as exc:
                raise PydanticAIRuntimeConfigurationError(str(exc)) from exc
            synthesis_delegator = AnchorSynthesisDelegator(
                context=synthesis_context,
                execute=self.run,
            )

            async def synthesize_ast_grep_anchors(
                ctx: RunContext[MinerContext],
                request: AnchorSynthesisRequest,
            ) -> list[AnchorSynthesisRunResult]:
                """Delegate every intent and return structurally typed child results."""

                batch = await synthesis_delegator.synthesize(request)
                ctx.deps.subagent_events.extend(batch.events)
                return list(batch.results)

            tools.append(synthesize_ast_grep_anchors)
            toolsets.append(_workspace_files(cases_dir, writable=False))
            capabilities.append(overflow_capability())
            model_settings = {"parallel_tool_calls": False}
        elif task.phase is AgentPhase.AST_GREP_SYNTHESIS:
            source_root = _require_path(
                task.context.source_root,
                "source_root",
                task.phase,
            )
            cases_dir = _require_path(
                task.context.cases_dir,
                "cases_dir",
                task.phase,
            )
            skill = next(
                (item for item in task.skills if item.name == "ast-grep"),
                None,
            )
            if skill is None:
                raise PydanticAIRuntimeConfigurationError(
                    "AST-Grep synthesis requires the ast-grep skill"
                )

            async def run_ast_grep_query(
                target: Literal["src", "cases"],
                language: str,
                query_type: Literal["pattern", "rule"],
                query: str,
                output: Literal["count", "sample", "full"] = "sample",
                sample_size: int = MINER_AST_GREP_SAMPLE_SIZE,
            ) -> dict[str, Any]:
                """Run one bounded query against an approved logical root."""

                if sample_size < 1 or sample_size > MINER_AST_GREP_MAX_SAMPLE_SIZE:
                    raise ValueError(
                        "sample_size must be between 1 and "
                        f"{MINER_AST_GREP_MAX_SAMPLE_SIZE}"
                    )
                target_root = source_root if target == "src" else cases_dir
                return await asyncio.to_thread(
                    run_ast_grep,
                    target_root,
                    language=language,
                    query_type=query_type,
                    query=query,
                    output=output,
                    sample_size=sample_size,
                    timeout_seconds=MINER_AST_GREP_TIMEOUT_SECONDS,
                )

            tools.append(run_ast_grep_query)
            toolsets.extend(
                (
                    _workspace_files(
                        task.context.workspace_root,
                        writable=False,
                        prefix="workspace",
                    ),
                    _workspace_files(
                        source_root,
                        writable=False,
                        prefix="src",
                    ),
                    _workspace_files(
                        cases_dir,
                        writable=False,
                        prefix="cases",
                    ),
                )
            )
            capabilities.extend(
                (
                    local_skill_capability(
                        skill.root / "SKILL.md",
                        defer_loading=False,
                        toolsets=[_skill_reference_files(skill.root)],
                    ),
                    overflow_capability(),
                    compaction_capability(),
                    cache_stability_capability(),
                )
            )
            model_settings = {"parallel_tool_calls": False}
        else:  # pragma: no cover - exhaustive enum guard for future phases.
            raise PydanticAIRuntimeConfigurationError(f"unsupported phase: {task.phase.value}")

        agent = Agent(
            name=task.agent_name,
            description=task.description,
            model=model,
            deps_type=MinerContext,
            instructions=_compiled_instructions(task),
            tools=tools,
            toolsets=toolsets,
            capabilities=capabilities,
            model_settings=model_settings,
            output_type=ToolOutput(
                task.output_type,
                name=_OUTPUT_TOOL_NAMES[task.phase],
                strict=False,
            ),
            retries={"output": task.limits.output_retries},
        )

        @agent.output_validator
        def validate_output(ctx: RunContext[MinerContext], output: OutputT) -> OutputT:
            errors = list(task.validate_output(output))
            if errors:
                if validation_state is not None:
                    validation_state[:] = [tuple(errors)]
                raise ModelRetry("Deterministic output validation failed:\n- " + "\n- ".join(errors))
            return output

        return agent

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        """Execute one SDK run using Pydantic AI's native output retry loop."""

        model = self._resolve_model(task)
        validation_state: list[tuple[str, ...]] = []
        agent = self.build_agent(task, model=model, validation_state=validation_state)
        usage = RunUsage()
        usage_limits = (
            UsageLimits(request_limit=task.limits.request_limit) if task.limits.request_limit is not None else None
        )

        async def execute() -> AgentRunResult[OutputT]:
            deps = _deps(task)
            try:
                result = await agent.run(
                    task.prompt,
                    deps=deps,
                    usage=usage,
                    usage_limits=usage_limits,
                )
            except UnexpectedModelBehavior as exc:
                raise PydanticAIOutputValidationError(
                    task.task_id,
                    validation_state[-1] if validation_state else (str(exc),),
                    attempts=1 + task.limits.output_retries,
                ) from exc
            return AgentRunResult(
                output=result.output,
                runtime_id=self.runtime_id,
                model_id=_model_id(model),
                usage=RuntimeUsage(
                    requests=usage.requests,
                    turns=usage.requests,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_input_tokens=usage.cache_write_tokens,
                    cache_read_input_tokens=usage.cache_read_tokens,
                ),
                artifacts=RuntimeArtifacts(
                    final_text=result.output.model_dump_json(indent=2, by_alias=True),
                ),
                attempts=1,
                metadata={
                    "run_id": result.run_id,
                    "subagent_events": list(deps.subagent_events),
                },
            )

        if task.limits.timeout_seconds is None:
            return await execute()
        try:
            async with asyncio.timeout(task.limits.timeout_seconds):
                return await execute()
        except TimeoutError as exc:
            raise PydanticAIRuntimeError(
                f"Pydantic AI task {task.task_id!r} exceeded {task.limits.timeout_seconds} seconds"
            ) from exc


__all__ = [
    "PydanticAIOutputValidationError",
    "PydanticAIRuntime",
    "PydanticAIRuntimeConfigurationError",
    "PydanticAIRuntimeError",
]
