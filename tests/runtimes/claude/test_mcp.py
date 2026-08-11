"""Tests for phase-scoped VAMiner MCP tool registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args, get_type_hints

from src.miner.runtimes.claude.mcp import (
    CASES_DIR_ENV,
    FIXED_DIFF_ENV,
    PROFILE_ENV,
    SERVER_NAME,
    SOURCE_ROOT_ENV,
    WORKSPACE_ROOT_ENV,
    MCPProfile,
    MCPServerSettings,
    build_server,
)


class FakeFastMCP:
    def __init__(self, name: str):
        self.name = name
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str):
        def register(function):
            self.tools[name] = function
            return function

        return register


def _base_env(workspace: Path, profile: str) -> dict[str, str]:
    return {
        PROFILE_ENV: profile,
        WORKSPACE_ROOT_ENV: workspace.as_posix(),
    }


def test_issue_profile_registers_only_issue_collection_tools(tmp_path: Path):
    server = build_server(
        env=_base_env(tmp_path, "issue_collection"),
        fast_mcp_factory=FakeFastMCP,
    )

    assert server.name == SERVER_NAME == "vaminer"
    assert set(server.tools) == {
        "fetch_cve",
        "fetch_github_issue",
        "parse_commit",
        "clone_repo",
        "search_commit_by_tag",
        "search_commit_by_time",
    }


def test_root_cause_profile_hides_unavailable_fixed_diff(tmp_path: Path):
    env = {
        **_base_env(tmp_path, "root_cause"),
        FIXED_DIFF_ENV: "false",
    }

    server = build_server(env=env, fast_mcp_factory=FakeFastMCP)

    assert set(server.tools) == set()


async def test_rule_generation_profile_registers_only_typed_plan_delegation(
    tmp_path: Path,
):
    async def synthesize(_request):
        return []

    server = build_server(
        settings=MCPServerSettings(
            profile=MCPProfile.RULE_GENERATION,
            workspace_root=tmp_path,
        ),
        fast_mcp_factory=FakeFastMCP,
        rule_synthesis_handler=synthesize,
    )

    assert set(server.tools) == {"synthesize_ast_grep_anchors"}


def test_ast_grep_synthesis_profile_exposes_only_typed_runner(tmp_path: Path):
    source_root = tmp_path / "src" / "owner" / "repo"
    cases_dir = tmp_path / "cases"
    source_root.mkdir(parents=True)
    cases_dir.mkdir()
    server = build_server(
        env={
            **_base_env(tmp_path, "ast_grep_synthesis"),
            SOURCE_ROOT_ENV: str(source_root),
            CASES_DIR_ENV: str(cases_dir),
        },
        fast_mcp_factory=FakeFastMCP,
    )

    assert set(server.tools) == {"run_ast_grep_query"}
    target_type = get_type_hints(server.tools["run_ast_grep_query"])["target"]
    assert get_args(target_type) == ("src", "cases")
