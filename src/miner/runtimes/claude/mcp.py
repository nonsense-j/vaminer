"""Phase-scoped typed MCP tools for the Claude Code Adapter."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from ...models.anchors import AnchorPlan, AnchorSynthesisResult
from ...tools.ast_grep import AstGrepQueryError, AstGrepRunnerError, run_ast_grep
from ...tools.cases import list_case_artifacts as list_cases_impl
from ...tools.cases import read_case_artifact as read_case_impl
from ...tools.cases import write_case_artifact as write_case_impl
from ...tools.cve import fetch_cve as fetch_cve_plain
from ...tools.github import (
    fetch_github_issue as fetch_github_issue_plain,
)
from ...tools.github import (
    parse_commit as parse_commit_plain,
)
from ...tools.github import (
    search_commit_by_tag as search_commit_by_tag_plain,
)
from ...tools.github import (
    search_commit_by_time as search_commit_by_time_plain,
)
from ...tools.repo import (
    clone_repository,
    read_patch_diff_from_repo,
)
from ...tools.src import list_src_files as list_src_impl
from ...tools.src import read_src_file as read_src_impl
from ...tools.src import search_src_files as search_src_impl
from ...tools.skills import list_skill_resources as list_skills_impl
from ...tools.skills import read_skill_resource as read_skill_impl
from ...utils.config import (
    MINER_AST_GREP_MAX_SAMPLE_SIZE,
    MINER_AST_GREP_SAMPLE_SIZE,
    MINER_AST_GREP_TIMEOUT_SECONDS,
)
from ...utils.log import mirror_run_log_file
from ...utils.telemetry import (
    flush_tracing,
    use_propagated_trace_environment,
)
from ...utils.workspace import atomic_write_json
from .process import clip, redact

SERVER_NAME = "vaminer"
PROFILE_ENV = "VAMINER_MCP_PROFILE"
WORKSPACE_ROOT_ENV = "VAMINER_MCP_WORKSPACE_ROOT"
REPO_PATH_ENV = "VAMINER_MCP_REPO_PATH"
SOURCE_ROOT_ENV = "VAMINER_MCP_SOURCE_ROOT"
CASES_DIR_ENV = "VAMINER_MCP_CASES_DIR"
SKILL_ROOT_ENV = "VAMINER_MCP_SKILL_ROOT"
FIXED_DIFF_ENV = "VAMINER_MCP_FIXED_DIFF_ENABLED"
GITHUB_MIRROR_ENV = "VAMINER_MCP_GITHUB_MIRROR_ENABLED"
SYNTHESIS_CONTEXT_ENV = "VAMINER_MCP_SYNTHESIS_CONTEXT"
SYNTHESIS_LOG_ENV = "VAMINER_MCP_SYNTHESIS_LOG"
TOOL_FAILURE_ENV = "VAMINER_MCP_TOOL_FAILURE"


class MCPProfile(StrEnum):
    ISSUE = "issue"
    ROOT_CAUSE = "root_cause"
    RULE_GENERATION = "rule_generation"
    AST_GREP_SYNTHESIS = "ast_grep_synthesis"

    @classmethod
    def parse(cls, value: str) -> MCPProfile:
        aliases = {"issue_collection": cls.ISSUE, **{item.value: item for item in cls}}
        try:
            return aliases[value.strip().lower()]
        except KeyError as exc:
            raise ValueError(f"unsupported MCP profile: {value!r}") from exc


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable is missing: {name}")
    return value


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _directory(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label} is not an existing directory: {path}")
    return path


def _scoped(value: str, label: str, workspace: Path) -> Path:
    path = _directory(value, label)
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the active workspace: {path}") from exc
    return path


@dataclass(frozen=True, slots=True)
class MCPServerSettings:
    profile: MCPProfile
    workspace_root: Path
    source_root: Path | None = None
    cases_dir: Path | None = None
    repo_path: Path | None = None
    skill_root: Path | None = None
    fixed_diff: bool = False
    github_mirror: bool = True
    synthesis_context_path: Path | None = None
    tool_failure_path: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MCPServerSettings:
        values = os.environ if env is None else env
        profile = MCPProfile.parse(_required(values, PROFILE_ENV))
        workspace = _directory(_required(values, WORKSPACE_ROOT_ENV), "workspace root")
        source = cases = repo = skill = context = tool_failure = None
        fixed_diff = _bool(values.get(FIXED_DIFF_ENV))
        if profile in {MCPProfile.ROOT_CAUSE, MCPProfile.RULE_GENERATION, MCPProfile.AST_GREP_SYNTHESIS}:
            source = _scoped(_required(values, SOURCE_ROOT_ENV), "source root", workspace)
            cases = _scoped(_required(values, CASES_DIR_ENV), "cases directory", workspace)
        if profile is MCPProfile.ROOT_CAUSE and fixed_diff:
            repo = _scoped(_required(values, REPO_PATH_ENV), "repository path", workspace)
        if profile is MCPProfile.AST_GREP_SYNTHESIS:
            skill = _directory(_required(values, SKILL_ROOT_ENV), "skill root")
            if not (skill / "SKILL.md").is_file():
                raise ValueError("skill root does not contain SKILL.md")
            tool_failure = Path(_required(values, TOOL_FAILURE_ENV)).expanduser().resolve()
            if not tool_failure.parent.is_dir():
                raise ValueError("tool failure receipt parent is not an existing directory")
        if profile is MCPProfile.RULE_GENERATION:
            context = Path(_required(values, SYNTHESIS_CONTEXT_ENV)).expanduser().resolve()
            if not context.is_file():
                raise ValueError(f"synthesis context is not an existing file: {context}")
        return cls(
            profile=profile,
            workspace_root=workspace,
            source_root=source,
            cases_dir=cases,
            repo_path=repo,
            skill_root=skill,
            fixed_diff=fixed_diff,
            github_mirror=_bool(values.get(GITHUB_MIRROR_ENV), True),
            synthesis_context_path=context,
            tool_failure_path=tool_failure,
        )


def _load_mcp_factory() -> Callable[[str], Any]:
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _register(server: Any, name: str, function: Callable[..., Any]) -> None:
    server.tool(name=name)(function)


def _register_src_tools(server: Any, root: Path) -> None:
    root_note = f"\n\nBound src root: `{root.as_posix()}`. All `path` arguments are relative to this root."

    def list_src_files(
        path: str | None = None,
        glob: str | None = None,
        max_results: int = 500,
    ) -> dict[str, object]:
        return list_src_impl(root, path=path, glob=glob, max_results=max_results)

    list_src_files.__doc__ = f"{list_src_impl.__doc__}{root_note}"

    def search_src_files(
        pattern: str,
        path: str | None = None,
        mode: Literal["literal", "regex"] = "literal",
        glob: str | None = None,
        max_results: int = 100,
    ) -> dict[str, object]:
        return search_src_impl(root, pattern, path=path, mode=mode, glob=glob, max_results=max_results)

    search_src_files.__doc__ = f"{search_src_impl.__doc__}{root_note}"

    def read_src_file(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, object]:
        return read_src_impl(root, path, start_line=start_line, end_line=end_line)

    read_src_file.__doc__ = f"{read_src_impl.__doc__}{root_note}"

    _register(server, "list_src_files", list_src_files)
    _register(server, "search_src_files", search_src_files)
    _register(server, "read_src_file", read_src_file)


def _register_case_tools(server: Any, cases_dir: Path, *, writable: bool) -> None:
    def list_case_artifacts() -> list[str]:
        return list_cases_impl(cases_dir)

    def read_case_artifact(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        return read_case_impl(cases_dir, path, start_line=start_line, end_line=end_line)

    _register(server, "list_case_artifacts", list_case_artifacts)
    _register(server, "read_case_artifact", read_case_artifact)
    if writable:
        def write_case_artifact(path: str, content: str) -> dict[str, Any]:
            return write_case_impl(cases_dir, path, content)

        _register(server, "write_case_artifact", write_case_artifact)


def _register_issue_tools(server: Any, settings: MCPServerSettings) -> None:
    def fetch_cve(cve_id: str) -> dict[str, Any]:
        return _json_value(fetch_cve_plain(cve_id))

    def fetch_github_issue(issue_url: str, fetch_extra_notes: bool = False) -> dict[str, Any]:
        return _json_value(fetch_github_issue_plain(issue_url, fetch_extra_notes))

    def parse_commit(commit_url: str) -> dict[str, Any]:
        return _json_value(parse_commit_plain(commit_url))

    def clone_repo(repo_url: str, buggy_sha: str, fixed_sha: str | None = None) -> dict[str, Any]:
        """Clone the buggy revision and optional fixed revision into the task workspace."""
        return _json_value(
            clone_repository(
                settings.workspace_root,
                repo_url,
                buggy_sha,
                fixed_sha,
                github_mirror_enabled=settings.github_mirror,
            )
        )

    for name, function in (
        ("fetch_cve", fetch_cve),
        ("fetch_github_issue", fetch_github_issue),
        ("parse_commit", parse_commit),
        ("clone_repo", clone_repo),
        ("search_commit_by_tag", search_commit_by_tag_plain),
        ("search_commit_by_time", search_commit_by_time_plain),
    ):
        _register(server, name, function)


def _register_root_cause_tools(server: Any, settings: MCPServerSettings) -> None:
    assert settings.source_root is not None and settings.cases_dir is not None
    _register_src_tools(server, settings.source_root)
    _register_case_tools(server, settings.cases_dir, writable=True)
    if settings.fixed_diff:
        assert settings.repo_path is not None

        def read_patch_diff(path: str | None = None) -> str:
            assert settings.repo_path is not None
            return read_patch_diff_from_repo(settings.repo_path, path)

        root_note = (
            f"\n\nBound repository root: `{settings.repo_path.as_posix()}`. "
            "The `path` argument is relative to this root."
        )
        read_patch_diff.__doc__ = f"{read_patch_diff_from_repo.__doc__}{root_note}"

        _register(server, "read_patch_diff", read_patch_diff)


RuleSynthesisHandler = Callable[[AnchorPlan], Awaitable[list[AnchorSynthesisResult]]]


def _default_synthesis_handler(settings: MCPServerSettings) -> RuleSynthesisHandler:
    assert settings.synthesis_context_path is not None
    handler: RuleSynthesisHandler | None = None

    async def synthesize(plan: AnchorPlan) -> list[AnchorSynthesisResult]:
        nonlocal handler
        if handler is None:
            from .synthesis import load_claude_synthesis_handler

            handler = load_claude_synthesis_handler(settings.synthesis_context_path)
        # The official Claude Langfuse plugin already records this MCP call.
        # A second tool observation becomes a sibling trace branch and makes
        # the Rule Generator continuation look as if it disappeared.
        return await handler(plan)

    return synthesize


def _register_rule_tools(
    server: Any,
    settings: MCPServerSettings,
    handler: RuleSynthesisHandler | None,
) -> None:
    assert settings.cases_dir is not None
    _register_case_tools(server, settings.cases_dir, writable=False)
    synthesize = handler or _default_synthesis_handler(settings)

    async def synthesize_anchor_plan(plan: AnchorPlan) -> list[AnchorSynthesisResult]:
        return await synthesize(plan)

    _register(server, "synthesize_anchor_plan", synthesize_anchor_plan)


def _register_synthesis_tools(server: Any, settings: MCPServerSettings) -> None:
    assert settings.source_root is not None and settings.cases_dir is not None and settings.skill_root is not None
    _register_src_tools(server, settings.source_root)
    _register_case_tools(server, settings.cases_dir, writable=False)

    def list_skill_resources(max_files: int = 100) -> dict[str, object]:
        return list_skills_impl({"ast-grep": settings.skill_root}, "ast-grep", max_files=max_files)

    def read_skill_resource(resource: str, start_line: int = 1, end_line: int | None = None) -> dict[str, object]:
        return read_skill_impl(
            {"ast-grep": settings.skill_root},
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
    ) -> dict[str, Any]:
        if sample_size < 1 or sample_size > MINER_AST_GREP_MAX_SAMPLE_SIZE:
            raise ValueError(f"sample_size must be between 1 and {MINER_AST_GREP_MAX_SAMPLE_SIZE}")
        root = settings.source_root if target == "src" else settings.cases_dir
        try:
            return await asyncio.to_thread(
                run_ast_grep,
                root,
                language=language,
                query_type=query_type,
                query=query,
                output=output,
                sample_size=sample_size,
                timeout_seconds=MINER_AST_GREP_TIMEOUT_SECONDS,
            )
        except AstGrepQueryError:
            raise
        except AstGrepRunnerError as exc:
            if settings.tool_failure_path is not None:
                atomic_write_json(
                    settings.tool_failure_path,
                    {
                        "type": type(exc).__name__,
                        "message": redact(clip(str(exc), 2_000)),
                    },
                )
            raise

    _register(server, "list_skill_resources", list_skill_resources)
    _register(server, "read_skill_resource", read_skill_resource)
    _register(server, "run_ast_grep_query", run_ast_grep_query)


def build_server(
    *,
    settings: MCPServerSettings | None = None,
    env: Mapping[str, str] | None = None,
    fast_mcp_factory: Callable[[str], Any] | None = None,
    rule_synthesis_handler: RuleSynthesisHandler | None = None,
) -> Any:
    resolved = settings or MCPServerSettings.from_env(env)
    server = (fast_mcp_factory or _load_mcp_factory())(SERVER_NAME)
    if resolved.profile is MCPProfile.ISSUE:
        _register_issue_tools(server, resolved)
    elif resolved.profile is MCPProfile.ROOT_CAUSE:
        _register_root_cause_tools(server, resolved)
    elif resolved.profile is MCPProfile.RULE_GENERATION:
        _register_rule_tools(server, resolved, rule_synthesis_handler)
    else:
        _register_synthesis_tools(server, resolved)
    return server


def main() -> None:
    try:
        settings = MCPServerSettings.from_env()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"VAMiner MCP configuration error: {exc}") from exc
    raw_log_path = os.getenv(SYNTHESIS_LOG_ENV)
    log_path = Path(raw_log_path) if raw_log_path else None
    with (
        use_propagated_trace_environment(),
        mirror_run_log_file(log_path),
    ):
        try:
            build_server(settings=settings).run(transport="stdio")
        finally:
            flush_tracing()


if __name__ == "__main__":
    main()


__all__ = [
    "CASES_DIR_ENV",
    "FIXED_DIFF_ENV",
    "GITHUB_MIRROR_ENV",
    "PROFILE_ENV",
    "REPO_PATH_ENV",
    "SERVER_NAME",
    "SKILL_ROOT_ENV",
    "SOURCE_ROOT_ENV",
    "SYNTHESIS_CONTEXT_ENV",
    "SYNTHESIS_LOG_ENV",
    "TOOL_FAILURE_ENV",
    "WORKSPACE_ROOT_ENV",
    "MCPProfile",
    "MCPServerSettings",
    "build_server",
    "main",
]
