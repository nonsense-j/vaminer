"""Tests for phase-scoped VAMiner MCP tool registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.miner.runtimes.claude.mcp import (
    FIXED_DIFF_ENV,
    PROFILE_ENV,
    REPO_PATH_ENV,
    SERVER_NAME,
    WORKSPACE_ROOT_ENV,
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


def test_root_cause_fixed_diff_requires_a_real_repo_path(tmp_path: Path):
    with pytest.raises(ValueError, match=REPO_PATH_ENV):
        MCPServerSettings.from_env(
            {
                **_base_env(tmp_path, "root_cause"),
                FIXED_DIFF_ENV: "true",
            }
        )


def test_non_mcp_phase_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported MCP profile"):
        MCPServerSettings.from_env(_base_env(tmp_path, "rule_generation"))
