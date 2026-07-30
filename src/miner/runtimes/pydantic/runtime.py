"""Pydantic AI adapter for runtime-neutral Miner tasks."""

from __future__ import annotations

import asyncio
import json
import sys
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
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness import FileSystem, Shell

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
from ...utils.config import (
    MINER_AST_GREP_MAX_PARALLEL_RUNS,
    MINER_MAX_TURNS_PER_ANCHOR,
)
from .config import (
    MINER_FS_MAX_FIND_RESULTS,
    MINER_FS_MAX_READ_LINES,
    MINER_FS_MAX_SEARCH_RESULTS,
)
from ...models.anchors import (
    AnchorIntent,
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
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
from .tools import clone_repo, read_patch_diff

_READ_ONLY_FILE_TOOLS = frozenset({"read_file", "search_files", "find_files"})
_WRITABLE_FILE_TOOLS = _READ_ONLY_FILE_TOOLS | {"write_file"}
_SKILL_REFERENCE_TOOLS = frozenset({"read_file"})
_SYNTHESIZER_INSTRUCTIONS = (
    Path(__file__).resolve().parents[2] / "instructions" / "ast_grep_synthesizer.md"
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


def _workspace_files(
    root: Path,
    *,
    writable: bool,
) -> AbstractToolset[MinerContext]:
    return (
        FileSystem[MinerContext](
            root_dir=root,
            max_read_lines=MINER_FS_MAX_READ_LINES,
            max_search_results=MINER_FS_MAX_SEARCH_RESULTS,
            max_find_results=MINER_FS_MAX_FIND_RESULTS,
        )
        .get_toolset()
        .filtered(lambda _ctx, tool: tool.name in (_WRITABLE_FILE_TOOLS if writable else _READ_ONLY_FILE_TOOLS))
    )


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


def _runner_shell(root: Path) -> AbstractToolset[MinerContext]:
    return (
        Shell[MinerContext](
            cwd=root,
            allowed_commands=(sys.executable,),
            denied_commands=(),
            default_timeout=60,
            max_output_chars=50_000,
        )
        .get_toolset()
        .filtered(lambda _ctx, tool: tool.name == "run_command")
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

- Locate files or matching lines with `search_files` and `find_files`, then use `read_file`
  with the smallest useful `offset` and `limit`. Page only when required context crosses the current slice.
- `read_patch_diff` is present only for issue intake with a verified fixed revision.
- Create complete files with `write_file`; generated cases belong directly under `cases/`.
- Submit the final object through the active structured-output contract.
""",
    AgentPhase.RULE_GENERATION: """
## Pydantic AI operational bindings

- Locate files or matching lines with `search_files` and `find_files`, then use `read_file`
  with the smallest useful `offset` and `limit`. Page only when required context crosses the current slice.
- Delegate every query through the typed `synthesize_ast_grep_anchors` tool.
- The tool fans out one isolated Synthesizer per intent and returns typed run results in request order.
- Treat each `plan_suggestion` as optional advisory text. Ignore it unless the shared
  Rule Generator instructions justify one bounded plan-refinement attempt.
- Assemble one complete VASCoreInfo from those results. Do not author or repair query text.
- If final validation rejects a query, disable only that anchor with `query: ""`.
""",
}


def _compiled_instructions(task: AgentTask[Any]) -> str:
    sections = [task.instructions.strip()]
    if task.input_instructions.strip():
        sections.append(task.input_instructions.strip())
    sections.append(_OPERATIONAL_BINDINGS[task.phase].strip())
    return "\n\n".join(sections) + "\n"


def _compiled_synthesizer_instructions(
    task: AgentTask[Any],
    *,
    skill_root: Path,
) -> str:
    binding = f"""## Pydantic AI operational bindings

- Locate files or matching lines with `search_files` and `find_files`, then use `read_file`
  with the smallest useful `offset` and `limit`. Page only when required context crosses the current slice.
- Read detailed syntax references with `skill_read_file`.
- Run structural queries only with `run_command` using this exact prefix:
  `{sys.executable} {skill_root / "scripts" / "runner.py"}`
- Return exactly one complete result through the active structured-output contract.
"""
    sections = [_SYNTHESIZER_INSTRUCTIONS.strip()]
    if task.input_instructions.strip():
        sections.append(task.input_instructions.strip())
    sections.append(binding.strip())
    return "\n\n".join(sections) + "\n"


def _enforce_target_anchor_result(
    result: AnchorSynthesisRunResult,
    intent: AnchorIntent,
) -> AnchorSynthesisRunResult:
    """Disable a child query if it rewrites or returns the wrong immutable intent."""
    anchor = result.anchor
    mismatches = [
        field
        for field in ("id", "behavior_weight", "behavior", "inspect_hint")
        if getattr(anchor, field) != getattr(intent, field)
    ]
    if not mismatches:
        return result

    disabled_anchor = anchor.model_copy(
        update={
            "id": intent.id,
            "behavior_weight": intent.behavior_weight,
            "query_weight": min(anchor.query_weight, intent.behavior_weight),
            "query": "",
            "behavior": intent.behavior,
            "inspect_hint": intent.inspect_hint,
        }
    )
    adjustment = (
        "Disabled the query because the Synthesizer changed immutable target "
        f"fields: {', '.join(mismatches)}."
    )
    return result.model_copy(
        update={
            "anchor": disabled_anchor,
            "adjustments": [*result.adjustments, adjustment],
            "plan_suggestion": "",
        }
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
            source_root = _require_path(task.context.source_root, "source_root", task.phase)
            cases_dir = _require_path(task.context.cases_dir, "cases_dir", task.phase)
            root_cause = task.context.root_cause
            analysis_subject = task.context.analysis_subject
            if root_cause is None or analysis_subject is None:
                raise PydanticAIRuntimeConfigurationError("rule generation requires root_cause and analysis_subject")
            skill = next((item for item in task.skills if item.name == "ast-grep"), None)
            if skill is None:
                raise PydanticAIRuntimeConfigurationError("rule generation requires the ast-grep skill")
            ast_grep_skill = local_skill_capability(
                skill.root / "SKILL.md",
                defer_loading=False,
                toolsets=[
                    _skill_reference_files(skill.root),
                    _runner_shell(task.context.workspace_root),
                ],
            )
            synthesizer = Agent(
                name="AST-Grep Synthesizer",
                description="Compiles one immutable anchor intent into one ast-grep anchor.",
                model=model,
                deps_type=MinerContext,
                instructions=_compiled_synthesizer_instructions(task, skill_root=skill.root),
                toolsets=(_workspace_files(task.context.workspace_root, writable=False),),
                capabilities=(
                    ast_grep_skill,
                    overflow_capability(),
                    compaction_capability(),
                    cache_stability_capability(),
                ),
                model_settings={"parallel_tool_calls": False},
                output_type=ToolOutput(
                    AnchorSynthesisRunResult,
                    name="return_anchor_synthesis_run",
                    strict=False,
                ),
                retries={"output": 2},
            )

            async def synthesize_ast_grep_anchors(
                ctx: RunContext[MinerContext],
                request: AnchorSynthesisRequest,
            ) -> list[AnchorSynthesisRunResult]:
                """Delegate every intent and return structurally typed child results."""

                semaphore = asyncio.Semaphore(MINER_AST_GREP_MAX_PARALLEL_RUNS)

                async def synthesize_one(
                    intent: AnchorIntent,
                ) -> tuple[AnchorSynthesisRunResult, RunUsage]:
                    run_request = AnchorSynthesisRunRequest(
                        root_cause=request.root_cause,
                        summary=request.summary,
                        anchor_plan=request.anchor_intents,
                        target_anchor_id=intent.id,
                    )
                    child_context = MinerContext(
                        workspace_root=ctx.deps.workspace_root,
                        source_root=source_root,
                        repo_path=task.context.repo_path,
                        cases_dir=cases_dir,
                        root_cause=root_cause,
                        analysis_subject=analysis_subject,
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
                            usage_limits=UsageLimits(request_limit=MINER_MAX_TURNS_PER_ANCHOR),
                        )
                    return _enforce_target_anchor_result(child_result.output, intent), child_usage

                child_results = await asyncio.gather(*(synthesize_one(intent) for intent in request.anchor_intents))
                for _, child_usage in child_results:
                    ctx.usage.incr(child_usage)
                ctx.deps.subagent_events.extend(
                    {
                        "intent_id": intent.id,
                        "runtime_id": self.runtime_id,
                        "model_id": _model_id(model),
                        "status": "completed",
                    }
                    for intent in request.anchor_intents
                )
                return [output for output, _ in child_results]

            tools.append(synthesize_ast_grep_anchors)
            toolsets.append(_workspace_files(task.context.workspace_root, writable=False))
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
