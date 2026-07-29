"""Tests for phase-scoped VAMiner MCP tool registration and finalization."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from src.miner.runtime.mcp_server import (
    BATCH_RESULT_ENV,
    CASES_DIR_ENV,
    FIXED_DIFF_ENV,
    PROFILE_ENV,
    REPO_PATH_ENV,
    SERVER_NAME,
    SKILL_ROOTS_ENV,
    SOURCE_ROOT_ENV,
    TASK_STATE_ENV,
    WORKSPACE_ROOT_ENV,
    MCPServerSettings,
    build_server,
)
from src.miner.utils.models import (
    AnalysisSubject,
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
    RootCauseAnalysis,
)

SOURCE = "void trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


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


def _root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The call is reached before the required guard.",
            "buggy_components": [
                {
                    "file": "bug.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": SOURCE.rstrip(),
                }
            ],
            "fixing_pattern": "Establish the required guard before invoking danger.",
            "extracted_case_files": ["case1.c"],
        }
    )


def _subject(source: Path, cases: Path) -> AnalysisSubject:
    return AnalysisSubject(
        type="issue",
        source_root=source.resolve().as_posix(),
        cases_dir=cases.resolve().as_posix(),
        grounding_policy="repo_evidence",
    )


def _request(root_cause: RootCauseAnalysis) -> AnchorSynthesisRequest:
    return AnchorSynthesisRequest.model_validate(
        {
            "root_cause": root_cause.model_dump(mode="json"),
            "summary": "Dangerous operations must run only after the required guard is established.",
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": BEHAVIOR,
                    "inspect_hint": INSPECT_HINT,
                    "required_cases": ["case1.c"],
                }
            ],
        }
    )


def _run(request: AnchorSynthesisRequest) -> tuple[AnchorSynthesisRunRequest, AnchorSynthesisRunResult]:
    return (
        AnchorSynthesisRunRequest(
            root_cause=request.root_cause,
            summary=request.summary,
            anchor_intent=request.anchor_intents[0],
        ),
        AnchorSynthesisRunResult.model_validate(
            {
                "anchor": {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "query_weight": 5,
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": BEHAVIOR,
                    "inspect_hint": INSPECT_HINT,
                },
                "adjustments": [],
            }
        ),
    )


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


def test_root_cause_profile_uses_source_root_and_hides_unavailable_fixed_diff(tmp_path: Path):
    source = tmp_path / "snapshot"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text(SOURCE, encoding="utf-8")
    env = {
        **_base_env(tmp_path, "root_cause"),
        SOURCE_ROOT_ENV: source.as_posix(),
        CASES_DIR_ENV: cases.as_posix(),
        FIXED_DIFF_ENV: "false",
    }

    server = build_server(env=env, fast_mcp_factory=FakeFastMCP)

    assert set(server.tools) == {
        "read_source_file",
        "search_source_files",
        "list_case_artifacts",
        "read_case_artifact",
        "write_case_artifact",
    }
    assert "danger" in server.tools["read_source_file"]("bug.c")["content"]
    server.tools["write_case_artifact"]("case1.c", SOURCE)
    assert server.tools["list_case_artifacts"]() == ["case1.c"]
    with pytest.raises(ValueError, match="bare filename"):
        server.tools["write_case_artifact"]("../case2.c", SOURCE)


def test_root_cause_fixed_diff_requires_a_real_repo_path(tmp_path: Path):
    source = tmp_path / "source"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    with pytest.raises(ValueError, match=REPO_PATH_ENV):
        MCPServerSettings.from_env(
            {
                **_base_env(tmp_path, "root_cause"),
                SOURCE_ROOT_ENV: source.as_posix(),
                CASES_DIR_ENV: cases.as_posix(),
                FIXED_DIFF_ENV: "true",
            }
        )


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_rule_profile_typed_gate_rejects_mutation_and_finalizes_exact_batch(tmp_path: Path):
    source = tmp_path / "source"
    cases = tmp_path / "cases"
    skill = tmp_path / "ast-grep"
    source.mkdir()
    cases.mkdir()
    skill.mkdir()
    (source / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases / "case1.c").write_text(SOURCE, encoding="utf-8")
    (skill / "SKILL.md").write_text("# AST-Grep\n", encoding="utf-8")
    root_cause = _root_cause()
    subject = _subject(source, cases)
    state = tmp_path / "task-state.json"
    state.write_text(
        json.dumps(
            {
                "root_cause": root_cause.model_dump(mode="json"),
                "analysis_subject": subject.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    result_path = tmp_path / "batch-result.json"
    env = {
        **_base_env(tmp_path, "rule_generation"),
        SOURCE_ROOT_ENV: source.as_posix(),
        CASES_DIR_ENV: cases.as_posix(),
        TASK_STATE_ENV: state.as_posix(),
        BATCH_RESULT_ENV: result_path.as_posix(),
        SKILL_ROOTS_ENV: json.dumps({"ast-grep": skill.as_posix()}),
    }
    server = build_server(env=env, fast_mcp_factory=FakeFastMCP)

    assert set(server.tools) == {
        "list_case_artifacts",
        "read_case_artifact",
        "read_source_file",
        "search_source_files",
        "list_skill_resources",
        "read_skill_resource",
        "run_ast_grep",
        "submit_anchor_synthesis_run",
        "finalize_anchor_synthesis_batch",
    }
    request = _request(root_cause)
    run_request, run_result = _run(request)
    changed_root = root_cause.model_copy(update={"analysis": "Changed by an untrusted child."})
    changed_request = run_request.model_copy(update={"root_cause": changed_root})
    with pytest.raises(ValueError, match="exact root_cause JSON"):
        server.tools["submit_anchor_synthesis_run"](changed_request, run_result)
    submitted = server.tools["submit_anchor_synthesis_run"](run_request, run_result)
    assert submitted["status"] == "validated"
    mutated = request.model_copy(
        update={"summary": "Dangerous operations should be reviewed before execution."}
    )
    with pytest.raises(ValueError, match="exact request JSON"):
        server.tools["finalize_anchor_synthesis_batch"](mutated)

    # A failed immutable finalization leaves the gate open for the exact request.
    finalized = server.tools["finalize_anchor_synthesis_batch"](request)
    assert finalized["anchors"][0]["id"] == "danger-call"
    assert result_path.is_file()
    with pytest.raises(ValueError, match="finalized more than once"):
        server.tools["finalize_anchor_synthesis_batch"](request)


def test_profile_settings_reject_out_of_scope_paths_and_obsolete_profiles(tmp_path: Path):
    outside = tmp_path.parent / "outside-mcp-source"
    outside.mkdir(exist_ok=True)
    cases = tmp_path / "cases"
    cases.mkdir()
    with pytest.raises(ValueError, match="must stay inside"):
        MCPServerSettings.from_env(
            {
                **_base_env(tmp_path, "root_cause"),
                SOURCE_ROOT_ENV: outside.as_posix(),
                CASES_DIR_ENV: cases.as_posix(),
            }
        )
    with pytest.raises(ValueError, match="unsupported MCP profile"):
        MCPServerSettings.from_env(_base_env(tmp_path, "anchor_synthesis"))


async def test_mcp_server_completes_a_real_stdio_handshake(tmp_path: Path):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.miner.runtime.mcp_server"],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            **_base_env(tmp_path, "issue"),
        },
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()

    assert initialized.server_info.name == SERVER_NAME
    assert {tool.name for tool in tools.tools} == {
        "fetch_cve",
        "fetch_github_issue",
        "parse_commit",
        "clone_repo",
        "search_commit_by_tag",
        "search_commit_by_time",
    }
