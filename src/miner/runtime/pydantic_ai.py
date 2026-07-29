"""Pydantic AI adapter for runtime-neutral Miner tasks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic_ai import (
    Agent,
    ModelRetry,
    RunContext,
    ToolOutput,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness import FileSystem

from ..configs import (
    MINER_AST_GREP_MAX_PARALLEL_RUNS,
    MINER_AST_GREP_MAX_REQUESTS_PER_ANCHOR,
    MINER_FS_MAX_FIND_RESULTS,
    MINER_FS_MAX_READ_LINES,
    MINER_FS_MAX_SEARCH_RESULTS,
)
from ..core.capabilities import (
    AgentCapability,
    cache_stability_capability,
    commit_history_capability,
    compaction_capability,
    local_skill_capability,
    overflow_capability,
    web_fetch_capability,
    web_search_capability,
)
from ..core.context import MinerContext
from ..core.validation import (
    aggregate_anchor_synthesis_runs,
    validate_anchor_synthesis_request,
    validate_anchor_synthesis_run,
    validate_vas_core_synthesis,
)
from ..tools.ast_grep import run_ast_grep
from ..tools.cve import fetch_cve
from ..tools.github import fetch_github_issue, parse_commit
from ..tools.repo import clone_repo, read_fixed_diff
from ..utils.llm import get_llm
from ..utils.models import (
    AnchorIntent,
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
)
from .contracts import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    OutputT,
    RuntimeArtifacts,
    RuntimeCapability,
    RuntimeUsage,
)

_READ_ONLY_FILE_TOOLS = frozenset({"read_file", "list_directory", "search_files", "find_files", "file_info"})
_SKILL_REFERENCE_TOOLS = frozenset({"read_file"})
_WRITABLE_CASE_TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_directory", "file_info"})
_SYNTHESIZER_INSTRUCTIONS = (
    Path(__file__).resolve().parents[1] / "instructions" / "ast_grep_synthesizer.md"
).read_text(encoding="utf-8")

_OUTPUT_TOOL_NAMES = {
    AgentPhase.ISSUE_COLLECTION: "submit_issue_collection",
    AgentPhase.ROOT_CAUSE: "submit_root_cause",
    AgentPhase.RULE_GENERATION: "submit_vas_core",
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


def _read_only_files(
    root: Path,
    prefix: str,
    *,
    tool_names: frozenset[str] = _READ_ONLY_FILE_TOOLS,
    allowed_patterns: Sequence[str] = (),
    use_miner_limits: bool = True,
) -> AbstractToolset[MinerContext]:
    limits = (
        {
            "max_read_lines": MINER_FS_MAX_READ_LINES,
            "max_search_results": MINER_FS_MAX_SEARCH_RESULTS,
            "max_find_results": MINER_FS_MAX_FIND_RESULTS,
        }
        if use_miner_limits
        else {}
    )
    return (
        FileSystem[MinerContext](root_dir=root, allowed_patterns=allowed_patterns, **limits)
        .get_toolset()
        .filtered(lambda _ctx, tool: tool.name in tool_names)
        .prefixed(prefix)
    )


def _writable_case_files(root: Path) -> AbstractToolset[MinerContext]:
    return (
        FileSystem[MinerContext](
            root_dir=root,
            allowed_patterns=("case*.*",),
            denied_patterns=("*/*",),
            max_read_lines=MINER_FS_MAX_READ_LINES,
            max_search_results=MINER_FS_MAX_SEARCH_RESULTS,
            max_find_results=MINER_FS_MAX_FIND_RESULTS,
        )
        .get_toolset()
        .filtered(lambda _ctx, tool: tool.name in _WRITABLE_CASE_TOOLS)
        .prefixed("cases")
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
        anchor_synthesis_run_request=context.anchor_run_request,
    )


_OPERATIONAL_BINDINGS = {
    AgentPhase.ISSUE_COLLECTION: """
## Pydantic AI operational bindings

- Primary evidence tools: `fetch_cve`, `fetch_github_issue`, and `parse_commit`.
- Checkout tool: `clone_repo`.
- Optional web and commit-history tools are loaded through `load_capability`
  only when the domain workflow calls for them.
- Submit the final object through the active structured-output contract.
""",
    AgentPhase.ROOT_CAUSE: """
## Pydantic AI operational bindings

- Source navigation tools are prefixed `source_`.
- `read_fixed_diff` is present only for issue intake with a verified fixed revision.
- Case artifact tools are prefixed `cases_`; write only top-level `caseN.ext` or `caseN_varM.ext` files.
- Submit the final object through the active structured-output contract.
""",
    AgentPhase.RULE_GENERATION: """
## Pydantic AI operational bindings

- Read the RCA-declared case artifacts with the read-only `cases_` tools.
- Delegate every query through the typed `synthesize_ast_grep_anchors` tool.
- The tool fans out one isolated Synthesizer per intent and returns only a deterministically finalized batch.
- Submit one complete VASCoreInfo using the returned anchors exactly.
""",
}


def _compiled_instructions(task: AgentTask[Any]) -> str:
    binding = _OPERATIONAL_BINDINGS[task.phase]
    return task.instructions.rstrip() + "\n\n" + binding.strip() + "\n"


class PydanticAIRuntime:
    """Execute every Miner phase through its existing Pydantic AI tool harness."""

    runtime_id = "pydantic-ai"
    capabilities = frozenset(RuntimeCapability)

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
            source_root = _require_path(task.context.source_root, "source_root", task.phase)
            cases_dir = _require_path(task.context.cases_dir, "cases_dir", task.phase)
            if RuntimeCapability.FIXED_DIFF in task.required_capabilities:
                _require_path(task.context.repo_path, "repo_path", task.phase)
                tools.append(read_fixed_diff)
            toolsets.extend((_read_only_files(source_root, "source"), _writable_case_files(cases_dir)))
            capabilities.extend((overflow_capability(), compaction_capability(), cache_stability_capability()))
        elif task.phase is AgentPhase.RULE_GENERATION:
            source_root = _require_path(task.context.source_root, "source_root", task.phase)
            cases_dir = _require_path(task.context.cases_dir, "cases_dir", task.phase)
            root_cause = task.context.root_cause
            analysis_subject = task.context.analysis_subject
            if root_cause is None or analysis_subject is None:
                raise PydanticAIRuntimeConfigurationError(
                    "rule generation requires root_cause and analysis_subject"
                )
            skill = next((item for item in task.skills if item.name == "ast-grep"), None)
            if skill is None:
                raise PydanticAIRuntimeConfigurationError("rule generation requires the ast-grep skill")
            ast_grep_skill = local_skill_capability(
                skill.root / "SKILL.md",
                defer_loading=False,
                toolsets=[
                    _read_only_files(
                        skill.root,
                        "skill",
                        tool_names=_SKILL_REFERENCE_TOOLS,
                        allowed_patterns=("references/**",),
                        use_miner_limits=False,
                    ),
                    FunctionToolset([run_ast_grep]),
                ],
            )
            synthesizer = Agent(
                name="AST-Grep Synthesizer",
                description="Compiles one immutable anchor intent into one validated ast-grep anchor.",
                model=model,
                deps_type=MinerContext,
                instructions=_SYNTHESIZER_INSTRUCTIONS,
                toolsets=(
                    _read_only_files(source_root, "source"),
                    _read_only_files(cases_dir, "cases"),
                ),
                capabilities=(
                    ast_grep_skill,
                    overflow_capability(),
                    compaction_capability(),
                    cache_stability_capability(),
                ),
                model_settings={"parallel_tool_calls": False},
                output_type=ToolOutput(
                    AnchorSynthesisRunResult,
                    name="submit_anchor_synthesis_run",
                    strict=False,
                ),
                retries={"output": 2},
            )

            @synthesizer.output_validator
            def validate_synthesis_run(
                ctx: RunContext[MinerContext],
                output: AnchorSynthesisRunResult,
            ) -> AnchorSynthesisRunResult:
                errors = validate_anchor_synthesis_run(
                    output,
                    source_root=source_root,
                    cases_dir=cases_dir,
                    root_cause=root_cause,
                    run_request=ctx.deps.anchor_synthesis_run_request,
                    analysis_subject=analysis_subject,
                )
                if errors:
                    raise ModelRetry(
                        "Deterministic anchor validation failed:\n- " + "\n- ".join(errors)
                    )
                return output

            async def synthesize_ast_grep_anchors(
                ctx: RunContext[MinerContext],
                request: AnchorSynthesisRequest,
            ) -> str:
                """Delegate every immutable intent and finalize the validated batch."""

                ctx.deps.anchor_synthesis_request = None
                ctx.deps.anchor_synthesis = None
                request_errors = validate_anchor_synthesis_request(
                    request,
                    root_cause=root_cause,
                )
                if request_errors:
                    raise ModelRetry(
                        "Invalid anchor synthesis request:\n- "
                        + "\n- ".join(request_errors)
                    )
                ctx.deps.anchor_synthesis_request = request
                semaphore = asyncio.Semaphore(MINER_AST_GREP_MAX_PARALLEL_RUNS)

                async def synthesize_one(
                    intent: AnchorIntent,
                ) -> tuple[AnchorSynthesisRunResult, RunUsage]:
                    run_request = AnchorSynthesisRunRequest(
                        root_cause=request.root_cause,
                        summary=request.summary,
                        anchor_intent=intent,
                    )
                    child_context = MinerContext(
                        workspace_root=ctx.deps.workspace_root,
                        source_root=source_root,
                        repo_path=task.context.repo_path,
                        cases_dir=cases_dir,
                        root_cause=root_cause,
                        analysis_subject=analysis_subject,
                        anchor_synthesis_run_request=run_request,
                    )
                    child_usage = RunUsage()
                    child_input = json.dumps(
                        {
                            "anchor_synthesis_run_request": run_request.model_dump(mode="json"),
                            "available_directories": {
                                "source_root": source_root.resolve().as_posix(),
                                "cases": cases_dir.resolve().as_posix(),
                            },
                        }
                    )
                    async with semaphore:
                        child_result = await synthesizer.run(
                            child_input,
                            deps=child_context,
                            usage=child_usage,
                            usage_limits=UsageLimits(
                                request_limit=MINER_AST_GREP_MAX_REQUESTS_PER_ANCHOR
                            ),
                        )
                    return child_result.output, child_usage

                child_results = await asyncio.gather(
                    *(synthesize_one(intent) for intent in request.anchor_intents)
                )
                for _, child_usage in child_results:
                    ctx.usage.incr(child_usage)
                try:
                    synthesis = aggregate_anchor_synthesis_runs(
                        request,
                        [output for output, _ in child_results],
                        source_root=source_root,
                        cases_dir=cases_dir,
                        root_cause=root_cause,
                        analysis_subject=analysis_subject,
                    )
                except ValueError as exc:
                    raise ModelRetry(str(exc)) from exc
                ctx.deps.anchor_synthesis = synthesis
                return synthesis.model_dump_json(by_alias=True)

            tools.append(synthesize_ast_grep_anchors)
            toolsets.append(_read_only_files(cases_dir, "cases"))
            capabilities.append(overflow_capability())
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
            if task.phase is AgentPhase.RULE_GENERATION:
                errors.extend(
                    validate_vas_core_synthesis(
                        output,  # type: ignore[arg-type]
                        request=ctx.deps.anchor_synthesis_request,
                        synthesis=ctx.deps.anchor_synthesis,
                    )
                )
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
                    "subagent_events": (
                        [
                            {
                                "intent_id": intent.id,
                                "runtime_id": self.runtime_id,
                                "model_id": _model_id(model),
                                "status": "validated",
                            }
                            for intent in deps.anchor_synthesis_request.anchor_intents
                        ]
                        if deps.anchor_synthesis_request is not None
                        else []
                    ),
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
