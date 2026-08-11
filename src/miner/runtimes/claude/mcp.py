"""Least-privilege stdio MCP adapter for Claude phase tools."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from ...tools.cve import fetch_cve as fetch_cve_plain
from ...tools.github import (
    fetch_github_issue as fetch_github_issue_plain,
    parse_commit as parse_commit_plain,
    search_commit_by_tag as search_commit_by_tag_plain,
    search_commit_by_time as search_commit_by_time_plain,
)
from ...tools.ast_grep import run_ast_grep
from ...tools.repo import clone_repository, read_patch_diff_from_repo
from ...models.anchors import AnchorSynthesisRequest, AnchorSynthesisRunResult
from ...utils.config import (
    MINER_AST_GREP_MAX_SAMPLE_SIZE,
    MINER_AST_GREP_SAMPLE_SIZE,
    MINER_AST_GREP_TIMEOUT_SECONDS,
)
from ...utils.log import mirror_run_log_file
from ...utils.telemetry import (
    flush_tracing,
    trace_tool_observation,
    use_propagated_trace_environment,
)

SERVER_NAME = "vaminer"
PROFILE_ENV = "VAMINER_MCP_PROFILE"
WORKSPACE_ROOT_ENV = "VAMINER_MCP_WORKSPACE_ROOT"
REPO_PATH_ENV = "VAMINER_MCP_REPO_PATH"
SOURCE_ROOT_ENV = "VAMINER_MCP_SOURCE_ROOT"
CASES_DIR_ENV = "VAMINER_MCP_CASES_DIR"
FIXED_DIFF_ENV = "VAMINER_MCP_FIXED_DIFF_ENABLED"
GITHUB_MIRROR_ENV = "VAMINER_MCP_GITHUB_MIRROR_ENABLED"
SYNTHESIS_CONTEXT_ENV = "VAMINER_MCP_SYNTHESIS_CONTEXT"
SYNTHESIS_LOG_ENV = "VAMINER_MCP_SYNTHESIS_LOG"


class MCPProfile(StrEnum):
    """Tool exposure profiles selected once for one phase subprocess."""

    ISSUE = "issue"
    ROOT_CAUSE = "root_cause"
    RULE_GENERATION = "rule_generation"
    AST_GREP_SYNTHESIS = "ast_grep_synthesis"

    @classmethod
    def parse(cls, value: str) -> MCPProfile:
        aliases = {
            "issue": cls.ISSUE,
            "issue_collection": cls.ISSUE,
            "root_cause": cls.ROOT_CAUSE,
            "rule_generation": cls.RULE_GENERATION,
            "ast_grep_synthesis": cls.AST_GREP_SYNTHESIS,
        }
        try:
            return aliases[value.strip().lower()]
        except KeyError as exc:
            allowed = ", ".join(profile.value for profile in cls)
            raise ValueError(f"unsupported MCP profile {value!r}; expected one of: {allowed}") from exc


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable is missing: {name}")
    return value


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _existing_directory(value: str, *, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label} is not an existing directory: {path}")
    return path


def _scoped_directory(value: str, *, label: str, workspace_root: Path) -> Path:
    path = _existing_directory(value, label=label)
    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the active workspace: {path}") from exc
    return path


@dataclass(frozen=True)
class MCPServerSettings:
    """Validated environment-derived configuration for one MCP subprocess."""

    profile: MCPProfile
    workspace_root: Path
    repo_path: Path | None = None
    source_root: Path | None = None
    cases_dir: Path | None = None
    fixed_diff_enabled: bool = False
    github_mirror_enabled: bool = True
    synthesis_context_path: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MCPServerSettings:
        values = os.environ if env is None else env
        profile = MCPProfile.parse(_required_env(values, PROFILE_ENV))
        workspace_root = _existing_directory(
            _required_env(values, WORKSPACE_ROOT_ENV),
            label="workspace root",
        )
        repo_path: Path | None = None
        fixed_diff_enabled = _parse_bool(values.get(FIXED_DIFF_ENV), default=False)

        if profile is MCPProfile.ROOT_CAUSE:
            if fixed_diff_enabled:
                repo_path = _scoped_directory(
                    _required_env(values, REPO_PATH_ENV),
                    label="repository path",
                    workspace_root=workspace_root,
                )
        synthesis_context_path: Path | None = None
        if profile is MCPProfile.RULE_GENERATION:
            synthesis_context_path = Path(
                _required_env(values, SYNTHESIS_CONTEXT_ENV)
            ).expanduser().resolve()
            if not synthesis_context_path.is_file():
                raise ValueError(
                    "synthesis context is not an existing file: "
                    f"{synthesis_context_path}"
                )
        source_root: Path | None = None
        cases_dir: Path | None = None
        if profile is MCPProfile.AST_GREP_SYNTHESIS:
            source_root = _scoped_directory(
                _required_env(values, SOURCE_ROOT_ENV),
                label="source root",
                workspace_root=workspace_root,
            )
            cases_dir = _scoped_directory(
                _required_env(values, CASES_DIR_ENV),
                label="cases directory",
                workspace_root=workspace_root,
            )
        return cls(
            profile=profile,
            workspace_root=workspace_root,
            repo_path=repo_path,
            source_root=source_root,
            cases_dir=cases_dir,
            fixed_diff_enabled=fixed_diff_enabled,
            github_mirror_enabled=_parse_bool(values.get(GITHUB_MIRROR_ENV), default=True),
            synthesis_context_path=synthesis_context_path,
        )


def _load_mcp_factory() -> Callable[[str], Any]:
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as exc:
        raise RuntimeError("Claude MCP support requires the optional dependency `mcp[cli]`") from exc


def _json_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _register_tool(server: Any, name: str, function: Callable[..., Any]) -> None:
    server.tool(name=name)(function)


def _register_issue_tools(server: Any, settings: MCPServerSettings) -> None:
    def fetch_cve(cve_id: str) -> dict[str, Any]:
        """Fetch normalized CVE evidence from deterministic external sources."""
        return _json_value(fetch_cve_plain(cve_id))

    def fetch_github_issue(issue_url: str, fetch_extra_notes: bool = False) -> dict[str, Any]:
        """Fetch one GitHub issue and its directly linked evidence."""
        return _json_value(fetch_github_issue_plain(issue_url, fetch_extra_notes))

    def parse_commit(commit_url: str) -> dict[str, Any]:
        """Fetch normalized metadata for one GitHub commit URL."""
        return _json_value(parse_commit_plain(commit_url))

    def clone_repo(
        repo_url: str,
        buggy_sha: str,
        fixed_sha: str | None = None,
    ) -> dict[str, Any]:
        """Prepare the task-owned buggy checkout and optional fixed branch."""
        return _json_value(
            clone_repository(
                settings.workspace_root,
                repo_url,
                buggy_sha,
                fixed_sha,
                github_mirror_enabled=settings.github_mirror_enabled,
            )
        )

    def search_commit_by_tag(
        owner: str,
        repo: str,
        tag_prefix: str,
    ) -> list[dict[str, Any]] | str:
        """Search commits by the narrowest evidence-supported tag prefix."""
        return _json_value(search_commit_by_tag_plain(owner, repo, tag_prefix))

    def search_commit_by_time(
        owner: str,
        repo: str,
        since: str,
        until: str,
    ) -> list[dict[str, Any]] | str:
        """Search commits in a narrow ISO-8601 time range as a last resort."""
        return _json_value(search_commit_by_time_plain(owner, repo, since, until))

    _register_tool(server, "fetch_cve", fetch_cve)
    _register_tool(server, "fetch_github_issue", fetch_github_issue)
    _register_tool(server, "parse_commit", parse_commit)
    _register_tool(server, "clone_repo", clone_repo)
    _register_tool(server, "search_commit_by_tag", search_commit_by_tag)
    _register_tool(server, "search_commit_by_time", search_commit_by_time)


def _register_root_cause_tools(server: Any, settings: MCPServerSettings) -> None:
    if settings.fixed_diff_enabled:
        assert settings.repo_path is not None

        def read_patch_diff(path: str | None = None) -> str:
            """Read the verified buggy-to-fixed diff, optionally for one path."""
            assert settings.repo_path is not None
            return read_patch_diff_from_repo(settings.repo_path, path)

        _register_tool(server, "read_patch_diff", read_patch_diff)


RuleSynthesisHandler = Callable[
    [AnchorSynthesisRequest],
    Awaitable[list[AnchorSynthesisRunResult]],
]


def _default_rule_synthesis_handler(
    settings: MCPServerSettings,
) -> RuleSynthesisHandler:
    assert settings.synthesis_context_path is not None

    handler = None

    async def synthesize(
        request: AnchorSynthesisRequest,
    ) -> list[AnchorSynthesisRunResult]:
        nonlocal handler
        if handler is None:
            from .synthesis import load_claude_synthesis_handler

            handler = load_claude_synthesis_handler(
                settings.synthesis_context_path
            )
        with trace_tool_observation(
            name="synthesize_ast_grep_anchors",
            input=request.model_dump(mode="json"),
            metadata={
                "runtime": "claude-code",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "synthesize_ast_grep_anchors",
            },
        ) as tool_observation:
            results = await handler(request)
            if tool_observation is not None:
                try:
                    tool_observation.update(
                        output=[
                            result.model_dump(mode="json")
                            for result in results
                        ]
                    )
                except Exception:  # noqa: BLE001 - tracing is observe-only.
                    pass
            return results

    return synthesize


def _register_ast_grep_synthesis_tools(
    server: Any,
    settings: MCPServerSettings,
) -> None:
    assert settings.source_root is not None
    assert settings.cases_dir is not None

    async def run_ast_grep_query(
        target: Literal["src", "cases"],
        language: str,
        query_type: Literal["pattern", "rule"],
        query: str,
        output: Literal["count", "sample", "full"] = "sample",
        sample_size: int = MINER_AST_GREP_SAMPLE_SIZE,
    ) -> dict[str, Any]:
        """Run one bounded ast-grep query against an approved logical root."""

        if sample_size < 1 or sample_size > MINER_AST_GREP_MAX_SAMPLE_SIZE:
            raise ValueError(
                "sample_size must be between 1 and "
                f"{MINER_AST_GREP_MAX_SAMPLE_SIZE}"
            )
        target_root = (
            settings.source_root
            if target == "src"
            else settings.cases_dir
        )
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

    _register_tool(server, "run_ast_grep_query", run_ast_grep_query)


def _register_rule_generation_tools(
    server: Any,
    settings: MCPServerSettings,
    *,
    handler: RuleSynthesisHandler | None = None,
) -> None:
    synthesize = handler or _default_rule_synthesis_handler(settings)

    async def synthesize_ast_grep_anchors(
        request: AnchorSynthesisRequest,
    ) -> list[AnchorSynthesisRunResult]:
        """Compile every queryless intent from one complete authoritative plan.

        The host fans out isolated, contract-bound Synthesizers concurrently
        and returns results in the same order as ``request.anchor_intents``.
        """

        return await synthesize(request)

    _register_tool(
        server,
        "synthesize_ast_grep_anchors",
        synthesize_ast_grep_anchors,
    )


def build_server(
    *,
    settings: MCPServerSettings | None = None,
    env: Mapping[str, str] | None = None,
    fast_mcp_factory: Callable[[str], Any] | None = None,
    rule_synthesis_handler: RuleSynthesisHandler | None = None,
) -> Any:
    """Build one ``vaminer`` server exposing only the selected profile tools."""
    resolved_settings = settings or MCPServerSettings.from_env(env)
    factory = fast_mcp_factory or _load_mcp_factory()
    server = factory(SERVER_NAME)
    if resolved_settings.profile is MCPProfile.ISSUE:
        _register_issue_tools(server, resolved_settings)
    elif resolved_settings.profile is MCPProfile.ROOT_CAUSE:
        _register_root_cause_tools(server, resolved_settings)
    elif resolved_settings.profile is MCPProfile.RULE_GENERATION:
        _register_rule_generation_tools(
            server,
            resolved_settings,
            handler=rule_synthesis_handler,
        )
    else:
        _register_ast_grep_synthesis_tools(server, resolved_settings)
    return server


def main() -> None:
    """Run the selected MCP profile over stdio."""
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
            server = build_server(settings=settings)
            server.run(transport="stdio")
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
    "SOURCE_ROOT_ENV",
    "SYNTHESIS_CONTEXT_ENV",
    "SYNTHESIS_LOG_ENV",
    "WORKSPACE_ROOT_ENV",
    "MCPProfile",
    "MCPServerSettings",
    "build_server",
    "main",
]
