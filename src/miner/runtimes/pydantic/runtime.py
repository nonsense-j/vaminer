"""Pydantic AI Adapter for the closed Miner Phase Authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel
from pydantic_ai import (
    Agent,
    ModelRetry,
    RunContext,
    ToolOutput,
    UnexpectedModelBehavior,
)
from pydantic_ai.capabilities import AbstractCapability, Hooks
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from ...agent.contracts import (
    AgentPhase,
    AgentRunResult,
    AgentSession,
    AgentTask,
    AnchorSynthesisAuthority,
    OutputT,
    RootCauseAuthority,
    RuleGenerationAuthority,
    RuntimeIdentity,
    RuntimeUsage,
)
from ...mining.synthesis import (
    AnchorSynthesisAcceptanceError,
    AnchorSynthesisSession,
)
from ...mining.validation.analysis import finalize_root_cause_cases
from ...models.anchors import AnchorPlanRequest, AnchorSynthesisResult
from ...models.vas import RuleGenerationDraft
from ...tools.ast_grep import run_ast_grep
from ...tools.errors import ToolInputError
from ...tools.cases import list_case_artifacts as list_cases_impl
from ...tools.cases import read_case_artifact as read_case_impl
from ...tools.cases import write_case_artifact as write_case_impl
from ...tools.cve import fetch_cve
from ...tools.github import fetch_github_issue, parse_commit
from ...tools.repo import read_patch_diff_from_repo
from ...tools.skills import list_skill_resources as list_skills_impl
from ...tools.skills import read_skill_resource as read_skill_impl
from ...tools.src import list_src_files as list_src_impl
from ...tools.src import read_src_file as read_src_impl
from ...tools.src import search_src_files as search_src_impl
from ...utils.config import (
    MINER_AST_GREP_MAX_SAMPLE_SIZE,
    MINER_AST_GREP_SAMPLE_SIZE,
    MINER_AST_GREP_TIMEOUT_SECONDS,
)
from .capabilities import (
    cache_stability_capability,
    commit_history_capability,
    compaction_capability,
    tool_feedback_capability,
    web_fetch_capability,
    web_search_capability,
)
from .context import MinerContext
from .llm import get_llm
from .telemetry import instrument_tracing
from .tools import clone_repo

_OUTPUT_TOOL_NAMES = {
    AgentPhase.ISSUE_COLLECTION: "submit_issue_collection",
    AgentPhase.ROOT_CAUSE: "submit_root_cause",
    AgentPhase.RULE_GENERATION: "submit_rule_generation_draft",
    AgentPhase.AST_GREP_SYNTHESIS: "return_anchor_synthesis_delta",
}


class PydanticAIRuntimeError(RuntimeError):
    pass


class PydanticAIRuntimeConfigurationError(PydanticAIRuntimeError):
    pass


class _PydanticAgentSession:
    """A single Pydantic AI Agent conversation with resumable message history."""

    def __init__(self, runtime: PydanticAIRuntime, task: AgentTask[Any]) -> None:
        self._runtime = runtime
        self._task = task
        self._model = runtime._resolve_model()
        self._final_state: list[BaseModel] = []
        self._attempt_state: list[int] = []
        self._agent = runtime.build_agent(
            task,
            model=self._model,
            final_state=self._final_state,
            attempt_state=self._attempt_state,
        )
        self._usage = RunUsage()
        self._usage_limits = (
            UsageLimits(request_limit=task.limits.request_limit)
            if task.limits.request_limit
            else None
        )
        self._message_history: list[Any] | None = None
        self._closed = False

    async def send(self, prompt: str) -> AgentRunResult[Any]:
        if self._closed:
            raise PydanticAIRuntimeError("Pydantic AI session is already closed")
        self._final_state.clear()
        self._attempt_state.clear()

        async def execute() -> AgentRunResult[Any]:
            try:
                result = await self._agent.run(
                    prompt,
                    deps=MinerContext(self._task.workspace_root),
                    usage=self._usage,
                    usage_limits=self._usage_limits,
                    message_history=self._message_history,
                )
            except UnexpectedModelBehavior as exc:
                raise PydanticAIRuntimeError(f"Pydantic AI task {self._task.task_id!r} failed: {exc}") from exc
            self._message_history = result.all_messages()
            output = cast(Any, self._final_state[-1] if self._final_state else result.output)
            return AgentRunResult(
                output=output,
                identity=RuntimeIdentity(
                    runtime_id=self._runtime.runtime_id,
                    model_id=_model_id(self._model),
                ),
                usage=RuntimeUsage(
                    requests=self._usage.requests,
                    turns=self._usage.requests,
                    input_tokens=self._usage.input_tokens,
                    output_tokens=self._usage.output_tokens,
                    cache_creation_input_tokens=self._usage.cache_write_tokens,
                    cache_read_input_tokens=self._usage.cache_read_tokens,
                ),
                attempts=self._attempt_state[-1] if self._attempt_state else 1,
            )

        if self._task.limits.timeout_seconds is None:
            return await execute()
        try:
            async with asyncio.timeout(self._task.limits.timeout_seconds):
                return await execute()
        except TimeoutError as exc:
            raise PydanticAIRuntimeError(
                f"Pydantic AI task {self._task.task_id!r} exceeded "
                f"{self._task.limits.timeout_seconds} seconds"
            ) from exc

    async def close(self) -> None:
        self._closed = True


def _model_id(model: Model | str) -> str:
    if isinstance(model, str):
        return model
    return str(getattr(model, "model_id", None) or getattr(model, "model_name", None) or type(model).__name__)


def _runtime_binding(task: AgentTask[Any]) -> str:
    if task.phase is AgentPhase.ISSUE_COLLECTION:
        detail = "Use the issue evidence, web fallback, and checkout tools named in the active tool catalog."
    elif task.phase is AgentPhase.ROOT_CAUSE:
        detail = (
            "Use list/search/read Src tools for source evidence and only the typed Case Artifact tools for cases."
        )
    elif task.phase is AgentPhase.RULE_GENERATION:
        detail = "Read only Case Artifacts and submit complete plans through `synthesize_anchor_plan`."
    else:
        detail = "Use scoped source/case/skill reads and execute queries only with `run_ast_grep_query`."
    return f"""# Runtime Binding

## Pydantic AI

- {detail}
- Generic filesystem, shell, and undeclared delegation tools are unavailable.
- Tool input errors are feedback: correct the arguments within the remaining turn budget.
- Submit the final typed object through the active structured-output tool.
"""


class PydanticAIRuntime:
    runtime_id = "pydanic-sdk"

    def __init__(
        self,
        *,
        model: Model | str | None = None,
        hooks: Hooks[MinerContext] | None = None,
    ) -> None:
        self._model = model
        self._hooks = hooks

    def _resolve_model(self) -> Model | str:
        return self._model if self._model is not None else get_llm()

    @property
    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(runtime_id=self.runtime_id, model_id=_model_id(self._resolve_model()))

    @staticmethod
    def _src_tools(root: Path) -> list[Any]:
        root_note = f"\n\nBound src root: `{root.as_posix()}`. All `path` arguments are relative to this root."

        async def list_src_files(
            path: str | None = None,
            glob: str | None = None,
            max_results: int = 500,
        ) -> str:
            return await asyncio.to_thread(
                list_src_impl,
                root,
                path=path,
                glob=glob,
                max_results=max_results,
            )

        list_src_files.__doc__ = f"{list_src_impl.__doc__}{root_note}"

        async def search_src_files(
            pattern: str,
            path: str | None = None,
            mode: Literal["literal", "regex"] = "literal",
            glob: str | None = None,
            max_results: int = 100,
        ) -> str:
            return await asyncio.to_thread(
                search_src_impl,
                root,
                pattern,
                path=path,
                mode=mode,
                glob=glob,
                max_results=max_results,
            )

        search_src_files.__doc__ = f"{search_src_impl.__doc__}{root_note}"

        def read_src_file(
            path: str,
            start_line: int = 1,
            end_line: int | None = None,
            full_file: bool = False,
        ) -> str:
            return read_src_impl(
                root,
                path,
                start_line=start_line,
                end_line=end_line,
                full_file=full_file,
            )

        read_src_file.__doc__ = f"{read_src_impl.__doc__}{root_note}"

        return [list_src_files, search_src_files, read_src_file]

    @staticmethod
    def _case_tools(cases_dir: Path, *, writable: bool) -> list[Any]:
        def list_case_artifacts() -> str:
            return list_cases_impl(cases_dir)

        def read_case_artifact(
            path: str,
            start_line: int = 1,
            end_line: int | None = None,
        ) -> str:
            return read_case_impl(cases_dir, path, start_line=start_line, end_line=end_line)

        tools: list[Any] = [list_case_artifacts, read_case_artifact]
        if writable:
            def write_case_artifact(path: str, content: str) -> str:
                """Write a Case Artifact named ``caseN.<ext>`` or ``caseN_varM.<ext>``.

                Invalid filenames are rejected before writing. Correct the
                filename and retry when the tool reports a naming error.
                """

                return write_case_impl(cases_dir, path, content)

            tools.append(write_case_artifact)
        return tools

    def build_agent(
        self,
        task: AgentTask[Any],
        *,
        model: Model | str,
        final_state: list[BaseModel],
        attempt_state: list[int] | None = None,
    ) -> Agent[MinerContext, Any]:
        tools: list[Any] = []
        capabilities: list[AbstractCapability[MinerContext]] = [tool_feedback_capability()]
        if self._hooks is not None:
            capabilities.append(self._hooks)
        model_settings: dict[str, Any] | None = None
        session: AnchorSynthesisSession | None = None

        if task.phase is AgentPhase.ISSUE_COLLECTION:
            tools.extend((fetch_cve, fetch_github_issue, parse_commit, clone_repo))
            capabilities.extend((web_search_capability(), web_fetch_capability(), commit_history_capability()))
        elif task.phase is AgentPhase.ROOT_CAUSE:
            authority = cast(RootCauseAuthority, task.authority)
            tools.extend(self._src_tools(authority.source_root))
            tools.extend(self._case_tools(authority.cases_dir, writable=True))
            if authority.fixed_diff:
                if authority.repo_path is None:
                    raise PydanticAIRuntimeConfigurationError("fixed diff requires repo_path")

                def read_patch_diff(path: str | None = None) -> str:
                    assert authority.repo_path is not None
                    return read_patch_diff_from_repo(authority.repo_path, path)

                root_note = (
                    f"\n\nBound repository root: `{authority.repo_path.as_posix()}`. "
                    "The `path` argument is relative to this root."
                )
                read_patch_diff.__doc__ = f"{read_patch_diff_from_repo.__doc__}{root_note}"

                tools.append(read_patch_diff)
            capabilities.extend((compaction_capability(), cache_stability_capability()))
        elif task.phase is AgentPhase.RULE_GENERATION:
            authority = cast(RuleGenerationAuthority, task.authority)
            session = AnchorSynthesisSession(authority, workspace_root=task.workspace_root, runtime=self)

            async def synthesize_anchor_plan(plan: AnchorPlanRequest) -> list[AnchorSynthesisResult]:
                """Submit the complete Anchor set: full intents synthesize, reuse_anchor_id entries preserve results."""
                assert session is not None
                return await session.synthesize(plan)

            tools.extend(self._case_tools(authority.cases_dir, writable=False))
            tools.append(synthesize_anchor_plan)
            model_settings = {"parallel_tool_calls": False}
        elif task.phase is AgentPhase.AST_GREP_SYNTHESIS:
            authority = cast(AnchorSynthesisAuthority, task.authority)
            tools.extend(self._src_tools(authority.source_root))
            tools.extend(self._case_tools(authority.cases_dir, writable=False))

            def list_skill_resources(max_files: int = 100) -> str:
                return list_skills_impl(
                    {"ast-grep": authority.skill_root},
                    "ast-grep",
                    max_files=max_files,
                )

            def read_skill_resource(
                resource: str,
                start_line: int = 1,
                end_line: int | None = None,
            ) -> str:
                return read_skill_impl(
                    {"ast-grep": authority.skill_root},
                    "ast-grep",
                    resource,
                    start_line=start_line,
                    end_line=end_line,
                )

            async def run_ast_grep_query(
                target: Literal["src", "cases"],
                language: str,
                query_type: Literal["pattern", "rule"],
                query: str,
                output: Literal["count", "sample", "full"] = "sample",
                sample_size: int = MINER_AST_GREP_SAMPLE_SIZE,
                debug_query: Literal["pattern", "ast", "cst", "sexp"] | None = None,
            ) -> str:
                """Test a raw pattern or YAML rule against a bound target.

                Full output includes metavariable captures. debug_query exposes
                the native pattern/AST/CST/S-expression tree on verbatim stderr
                and applies only to raw pattern queries.
                """

                if target not in {"src", "cases"}:
                    raise ToolInputError("target must be 'src' or 'cases'")
                if (
                    not isinstance(sample_size, int)
                    or isinstance(sample_size, bool)
                    or sample_size < 1
                    or sample_size > MINER_AST_GREP_MAX_SAMPLE_SIZE
                ):
                    raise ToolInputError(f"sample_size must be between 1 and {MINER_AST_GREP_MAX_SAMPLE_SIZE}")
                root = authority.source_root if target == "src" else authority.cases_dir
                return await asyncio.to_thread(
                    run_ast_grep,
                    root,
                    language=language,
                    query_type=query_type,
                    query=query,
                    output=output,
                    sample_size=sample_size,
                    debug_query=debug_query,
                    timeout_seconds=MINER_AST_GREP_TIMEOUT_SECONDS,
                )

            tools.extend((list_skill_resources, read_skill_resource, run_ast_grep_query))
            capabilities.extend((compaction_capability(), cache_stability_capability()))
            model_settings = {"parallel_tool_calls": False}
        else:  # pragma: no cover
            raise PydanticAIRuntimeConfigurationError(f"unsupported phase: {task.phase.value}")

        model_output_type = task.model_output_type
        agent = Agent(
            name=task.agent_name,
            description=task.description,
            model=model,
            deps_type=MinerContext,
            instructions=task.instructions.render(_runtime_binding(task)),
            tools=tools,
            capabilities=capabilities,
            model_settings=model_settings,
            output_type=ToolOutput(model_output_type, name=_OUTPUT_TOOL_NAMES[task.phase], strict=False),
            # The SDK requires a retry ceiling; share the request ceiling so
            # tools and output repair are stopped by max-turns, not earlier.
            retries=task.limits.request_limit or UsageLimits().request_limit or 50,
        )

        @agent.output_validator
        def validate_output(ctx: RunContext[MinerContext], output: BaseModel) -> BaseModel:
            if attempt_state is not None:
                attempt_state[:] = [ctx.retry + 1]
            try:
                if session is not None:
                    final = session.finalize(cast(RuleGenerationDraft, output))
                else:
                    final = output
                    if task.phase is AgentPhase.ROOT_CAUSE:
                        authority = cast(RootCauseAuthority, task.authority)
                        finalize_root_cause_cases(cast(Any, output), cases_dir=authority.cases_dir)
                errors = list(task.validate_output(cast(Any, final)))
            except AnchorSynthesisAcceptanceError as exc:
                errors = [str(exc)]
                final = output
            if errors:
                raise ModelRetry("Deterministic output validation failed:\n- " + "\n- ".join(errors))
            final_state[:] = [final]
            return output

        return agent

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        session = self.open_session(task)
        try:
            return await session.send(task.prompt)
        finally:
            await session.close()

    def open_session(self, task: AgentTask[OutputT]) -> AgentSession[OutputT]:
        """Open one Agent conversation whose follow-ups reuse message history."""

        instrument_tracing()
        return cast(AgentSession[OutputT], _PydanticAgentSession(self, task))


__all__ = [
    "PydanticAIRuntime",
    "PydanticAIRuntimeConfigurationError",
    "PydanticAIRuntimeError",
]
