"""Pydantic AI agent definitions for the deterministic miner pipeline."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic_ai import Agent, AgentRunResult, ModelRetry, RunContext, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai_harness import FileSystem

from ..configs import (
    MINER_AST_GREP_MAX_PARALLEL_RUNS,
    MINER_FS_MAX_FIND_RESULTS,
    MINER_FS_MAX_READ_LINES,
    MINER_FS_MAX_SEARCH_RESULTS,
    MINER_SRC_DIR,
    make_anchor_synthesis_usage_limits,
)
from ..tools.ast_grep import run_ast_grep
from ..tools.cve import fetch_cve
from ..tools.github import (
    fetch_github_issue,
    parse_commit,
)
from ..tools.output import (
    finalize_anchor_synthesis_run,
    finalize_issue_info,
    finalize_root_cause,
    finalize_vas_core,
)
from ..tools.repo import (
    clone_repo,
    read_fixed_diff,
)
from ..utils.llm import get_llm
from ..utils.models import (
    AnchorIntent,
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
    IssueCollectionInfo,
    RootCauseAnalysis,
    VASCoreInfo,
)
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
from .validation import (
    aggregate_anchor_synthesis_runs,
    validate_anchor_synthesis_request,
)

_INSTRUCTIONS_DIR = MINER_SRC_DIR / "instructions"
_SKILLS_DIR = MINER_SRC_DIR / "skills"

ISSUE_COLLECTOR_INSTRUCTIONS = (_INSTRUCTIONS_DIR / "issue_collector.md").read_text(encoding="utf-8")
RULE_GENERATOR_INSTRUCTIONS = (_INSTRUCTIONS_DIR / "rule_generator.md").read_text(encoding="utf-8")
ROOT_CAUSE_ANALYZER_INSTRUCTIONS = (_INSTRUCTIONS_DIR / "root_cause_analyzer.md").read_text(encoding="utf-8")
AST_GREP_SYNTHESIZER_INSTRUCTIONS = (_INSTRUCTIONS_DIR / "ast_grep_synthesizer.md").read_text(encoding="utf-8")

_READ_ONLY_FILE_TOOLS = frozenset({"read_file", "list_directory", "search_files", "find_files", "file_info"})
_SKILL_REFERENCE_TOOLS = frozenset({"read_file"})
_WRITABLE_CASE_TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_directory", "file_info"})
_STRUCTURED_OUTPUT_RETRIES = {"output": 3}


def _model(model: Model | str | None) -> Model | str:
    return model if model is not None else get_llm()


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


def _writable_files(root: Path, prefix: str) -> AbstractToolset[MinerContext]:
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
        .prefixed(prefix)
    )


def make_issue_collector(
    *,
    model: Model | str | None = None,
    additional_capabilities: Sequence[AgentCapability] = (),
) -> Agent[MinerContext, IssueCollectionInfo]:
    """Create the issue collection agent with deferred fallback capabilities."""
    return Agent(
        name="Issue Collector",
        description="Collects issue evidence, resolves commits, and prepares the repository checkout.",
        model=_model(model),
        deps_type=MinerContext,
        instructions=ISSUE_COLLECTOR_INSTRUCTIONS,
        tools=[
            fetch_cve,
            fetch_github_issue,
            parse_commit,
            clone_repo,
        ],
        capabilities=[
            *additional_capabilities,
            web_search_capability(),
            web_fetch_capability(),
            commit_history_capability(),
            overflow_capability(),
        ],
        output_type=ToolOutput(
            finalize_issue_info,
            name="submit_issue_collection",
            strict=False,
        ),
        retries=_STRUCTURED_OUTPUT_RETRIES,
    )


def make_root_cause_analyzer(
    repo_path: Path,
    cases_dir: Path,
    *,
    model: Model | str | None = None,
    additional_capabilities: Sequence[AgentCapability] = (),
) -> Agent[MinerContext, RootCauseAnalysis]:
    """Create the RCA agent over a read-only repo and writable persistent cases."""
    return Agent(
        name="Root Cause Analyzer",
        description="Finds the code-level root cause and writes persistent minimal cases.",
        model=_model(model),
        deps_type=MinerContext,
        instructions=ROOT_CAUSE_ANALYZER_INSTRUCTIONS,
        tools=[read_fixed_diff],
        toolsets=[
            _read_only_files(repo_path, "repo"),
            _writable_files(cases_dir, "cases"),
        ],
        capabilities=[
            *additional_capabilities,
            overflow_capability(),
            compaction_capability(),
            cache_stability_capability(),
        ],
        output_type=ToolOutput(
            finalize_root_cause,
            name="submit_root_cause",
            strict=False,
        ),
        retries=_STRUCTURED_OUTPUT_RETRIES,
    )


def make_ast_grep_synthesizer(
    repo_path: Path,
    cases_dir: Path,
    *,
    model: Model | str | None = None,
    additional_capabilities: Sequence[AgentCapability] = (),
) -> Agent[MinerContext, AnchorSynthesisRunResult]:
    """Create the typed subagent used independently for each anchor intent."""
    skill_dir = _SKILLS_DIR / "ast-grep"
    ast_grep_skill = local_skill_capability(
        skill_dir / "SKILL.md",
        defer_loading=False,
        toolsets=[
            _read_only_files(
                skill_dir,
                "skill",
                tool_names=_SKILL_REFERENCE_TOOLS,
                allowed_patterns=("references/**",),
                use_miner_limits=False,
            ),
            FunctionToolset([run_ast_grep]),
        ],
    )
    return Agent(
        name="AST-Grep Synthesizer",
        description="Compiles queryless anchor intents into validated ast-grep anchors.",
        model=_model(model),
        deps_type=MinerContext,
        instructions=AST_GREP_SYNTHESIZER_INSTRUCTIONS,
        toolsets=[
            _read_only_files(repo_path, "repo"),
            _read_only_files(cases_dir, "cases"),
        ],
        capabilities=[
            *additional_capabilities,
            ast_grep_skill,
            overflow_capability(),
            compaction_capability(),
            cache_stability_capability(),
        ],
        model_settings={"parallel_tool_calls": False},
        output_type=ToolOutput(
            finalize_anchor_synthesis_run,
            name="submit_anchor_synthesis_run",
            strict=False,
        ),
        retries=_STRUCTURED_OUTPUT_RETRIES,
    )


def make_rule_generator(
    cases_dir: Path,
    synthesizer: Agent[MinerContext, AnchorSynthesisRunResult],
    *,
    model: Model | str | None = None,
    additional_capabilities: Sequence[AgentCapability] = (),
) -> Agent[MinerContext, VASCoreInfo]:
    """Create the rule generator with a typed Synthesizer subagent tool."""
    resolved_model = _model(model)

    async def synthesize_ast_grep_anchors(
        ctx: RunContext[MinerContext],
        request: AnchorSynthesisRequest,
    ) -> str:
        """Compile isolated anchors and deterministically aggregate the validated batch."""
        ctx.deps.anchor_synthesis_request = None
        ctx.deps.anchor_synthesis = None
        root_cause = ctx.deps.root_cause
        if root_cause is None:
            raise RuntimeError("root_cause is missing from agent dependencies")
        request_errors = validate_anchor_synthesis_request(request, root_cause=root_cause)
        if request_errors:
            raise ModelRetry("Invalid anchor synthesis request:\n- " + "\n- ".join(request_errors))
        ctx.deps.anchor_synthesis_request = request
        if ctx.deps.repo_path is None:
            raise RuntimeError("repo_path is missing from agent dependencies")
        if ctx.deps.cases_dir is None:
            raise RuntimeError("cases_dir is missing from agent dependencies")
        repo_path = ctx.deps.repo_path
        cases_path = ctx.deps.cases_dir
        semaphore = asyncio.Semaphore(MINER_AST_GREP_MAX_PARALLEL_RUNS)

        async def synthesize_one(intent: AnchorIntent) -> AgentRunResult[AnchorSynthesisRunResult]:
            run_request = AnchorSynthesisRunRequest(
                root_cause=request.root_cause,
                summary=request.summary,
                anchor_intent=intent,
            )
            run_context = MinerContext(
                workspace_root=ctx.deps.workspace_root,
                repo_path=repo_path,
                cases_dir=cases_path,
                root_cause=root_cause,
                anchor_synthesis_run_request=run_request,
            )
            synthesis_input = {
                "anchor_synthesis_request": run_request.model_dump(mode="json"),
                "available_directories": {
                    "repository": repo_path.resolve().as_posix(),
                    "cases": cases_path.resolve().as_posix(),
                },
            }
            async with semaphore:
                return await synthesizer.run(
                    json.dumps(synthesis_input),
                    deps=run_context,
                    usage_limits=make_anchor_synthesis_usage_limits(),
                )

        run_results = await asyncio.gather(
            *(synthesize_one(intent) for intent in request.anchor_intents),
        )
        for result in run_results:
            ctx.usage.incr(result.usage)

        try:
            synthesis = aggregate_anchor_synthesis_runs(
                request,
                [result.output for result in run_results],
                repo_path=repo_path,
                cases_dir=cases_path,
                root_cause=root_cause,
            )
        except ValueError as exc:
            raise ModelRetry(str(exc)) from exc
        ctx.deps.anchor_synthesis = synthesis
        return synthesis.model_dump_json(by_alias=True)

    return Agent(
        name="Rule Generator",
        description="Generalizes the RCA into one VAS rule and delegates all query work.",
        model=resolved_model,
        deps_type=MinerContext,
        instructions=RULE_GENERATOR_INSTRUCTIONS,
        tools=[synthesize_ast_grep_anchors],
        toolsets=[_read_only_files(cases_dir, "cases")],
        capabilities=[
            *additional_capabilities,
            overflow_capability(),
        ],
        model_settings={"parallel_tool_calls": False},
        output_type=ToolOutput(
            finalize_vas_core,
            name="submit_vas_core",
            strict=False,
        ),
        retries=_STRUCTURED_OUTPUT_RETRIES,
    )
