"""Least-privilege stdio MCP adapter for Claude phase tools."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ...tools.cve import fetch_cve as fetch_cve_plain
from ...tools.github import (
    fetch_github_issue as fetch_github_issue_plain,
    parse_commit as parse_commit_plain,
    search_commit_by_tag as search_commit_by_tag_plain,
    search_commit_by_time as search_commit_by_time_plain,
)
from ...tools.repo import clone_repository, read_patch_diff_from_repo

SERVER_NAME = "vaminer"
PROFILE_ENV = "VAMINER_MCP_PROFILE"
WORKSPACE_ROOT_ENV = "VAMINER_MCP_WORKSPACE_ROOT"
REPO_PATH_ENV = "VAMINER_MCP_REPO_PATH"
FIXED_DIFF_ENV = "VAMINER_MCP_FIXED_DIFF_ENABLED"
GITHUB_MIRROR_ENV = "VAMINER_MCP_GITHUB_MIRROR_ENABLED"


class MCPProfile(StrEnum):
    """Tool exposure profiles selected once for one phase subprocess."""

    ISSUE = "issue"
    ROOT_CAUSE = "root_cause"

    @classmethod
    def parse(cls, value: str) -> MCPProfile:
        aliases = {
            "issue": cls.ISSUE,
            "issue_collection": cls.ISSUE,
            "root_cause": cls.ROOT_CAUSE,
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
    fixed_diff_enabled: bool = False
    github_mirror_enabled: bool = True

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
        return cls(
            profile=profile,
            workspace_root=workspace_root,
            repo_path=repo_path,
            fixed_diff_enabled=fixed_diff_enabled,
            github_mirror_enabled=_parse_bool(values.get(GITHUB_MIRROR_ENV), default=True),
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


def build_server(
    *,
    settings: MCPServerSettings | None = None,
    env: Mapping[str, str] | None = None,
    fast_mcp_factory: Callable[[str], Any] | None = None,
) -> Any:
    """Build one ``vaminer`` server exposing only the selected profile tools."""
    resolved_settings = settings or MCPServerSettings.from_env(env)
    factory = fast_mcp_factory or _load_mcp_factory()
    server = factory(SERVER_NAME)
    {
        MCPProfile.ISSUE: _register_issue_tools,
        MCPProfile.ROOT_CAUSE: _register_root_cause_tools,
    }[resolved_settings.profile](server, resolved_settings)
    return server


def main() -> None:
    """Run the selected MCP profile over stdio."""
    try:
        server = build_server()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"VAMiner MCP configuration error: {exc}") from exc
    server.run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = [
    "FIXED_DIFF_ENV",
    "GITHUB_MIRROR_ENV",
    "PROFILE_ENV",
    "REPO_PATH_ENV",
    "SERVER_NAME",
    "WORKSPACE_ROOT_ENV",
    "MCPProfile",
    "MCPServerSettings",
    "build_server",
    "main",
]
