"""End-to-end deterministic test for the Pydantic adapter and unified rule phase."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from git import Actor, Repo
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.miner.core.tasks import make_root_cause_task, make_rule_generation_task
from src.miner.runtime.pydantic_ai import PydanticAIRuntime
from src.miner.utils.models import AnalysisSubject, IssueCollectionInfo

CASE_SOURCE = "/* missing guard */\nvoid trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


def _root_cause_payload() -> dict:
    return {
        "language": "c",
        "root_cause_summary": "A dangerous operation executes without its required guard.",
        "analysis": "The call reaches danger before any guard establishes the required invariant.",
        "buggy_components": [
            {
                "file": "bug.c",
                "start_line": 1,
                "end_line": 2,
                "role": "Executes the dangerous operation without the required guard.",
                "snippet": CASE_SOURCE.rstrip(),
            }
        ],
        "fixing_pattern": "Establish the guard before invoking danger.",
        "extracted_case_files": ["case1.c"],
    }


def _request_payload() -> dict:
    return {
        "root_cause": _root_cause_payload(),
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


def _core_payload() -> dict:
    return {
        "category": "SECURITY",
        "language": "c",
        "root_cause_summary": _root_cause_payload()["root_cause_summary"],
        "summary": _request_payload()["summary"],
        "scenarios": {
            "unsafe": ["A dangerous operation executes before its required guard is established."],
            "safe": ["The required guard is established before the dangerous operation executes."],
        },
        "anchors": [_run_payload()["anchor"]],
    }


def _run_payload() -> dict:
    return {
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


def _prepare_repository(repo_path: Path) -> tuple[str, str]:
    repo_path.mkdir()
    (repo_path / "bug.c").write_text(CASE_SOURCE, encoding="utf-8")
    repo = Repo.init(repo_path)
    actor = Actor("VAS Test", "vas-test@example.com")
    repo.index.add(["bug.c"])
    buggy = repo.index.commit("buggy", author=actor, committer=actor)
    (repo_path / "bug.c").write_text(
        "void trigger(void) { guard(1); danger(1); }\n",
        encoding="utf-8",
    )
    repo.index.add(["bug.c"])
    fixed = repo.index.commit("fixed", author=actor, committer=actor)
    repo.create_head("buggy", buggy)
    repo.create_head("fixed", fixed)
    repo.heads.buggy.checkout()
    return buggy.hexsha, fixed.hexsha


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_pydantic_runtime_executes_rca_tools_and_unified_parent_child_rule_generation(
    tmp_path: Path,
):
    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    buggy_sha, fixed_sha = _prepare_repository(repo_path)
    collection = IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="Fixture issue",
        issue_details="A dangerous call lacks its guard.",
        repo_url="https://github.com/example/project",
        repo_path=repo_path.as_posix(),
        buggy_commit=buggy_sha,
        fixed_commit=fixed_sha,
    )
    rca_step = 0

    async def rca_model(messages, info):
        nonlocal rca_step
        rca_step += 1
        if rca_step == 1:
            assert "read_fixed_diff" in {tool.name for tool in info.function_tools}
            return ModelResponse(parts=[ToolCallPart("read_fixed_diff", {})])
        if rca_step == 2:
            assert "guard(1)" in repr(messages)
            return ModelResponse(
                parts=[ToolCallPart("cases_write_file", {"path": "case1.c", "content": CASE_SOURCE})]
            )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _root_cause_payload())])

    root_task = make_root_cause_task(
        collection,
        workspace_root=tmp_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
    )
    root_result = await PydanticAIRuntime(model=FunctionModel(rca_model)).run(root_task)
    root_cause = root_result.output
    parent_step = 0

    async def unified_rule_model(messages, info):
        nonlocal parent_step
        tools = {tool.name for tool in info.function_tools}
        if "synthesize_ast_grep_anchors" not in tools:
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, _run_payload())]
            )
        parent_step += 1
        if parent_step == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "synthesize_ast_grep_anchors",
                        {"request": _request_payload()},
                    )
                ]
            )
        assert "repo_evidence" in repr(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _core_payload())])

    subject = AnalysisSubject(
        type="issue",
        source_root=repo_path.resolve().as_posix(),
        cases_dir=cases_dir.resolve().as_posix(),
        grounding_policy="repo_evidence",
    )
    rule_task = make_rule_generation_task(
        root_cause,
        workspace_root=tmp_path,
        source_root=repo_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
        analysis_subject=subject,
    )
    rule_result = await PydanticAIRuntime(model=FunctionModel(unified_rule_model)).run(rule_task)

    assert (cases_dir / "case1.c").read_text(encoding="utf-8") == CASE_SOURCE
    assert rule_result.output.anchors[0].id == "danger-call"
    assert root_result.usage is not None and root_result.usage.requests == 3
    assert rule_result.usage is not None and rule_result.usage.requests == 3
    assert rule_result.metadata["subagent_events"][0]["intent_id"] == "danger-call"
