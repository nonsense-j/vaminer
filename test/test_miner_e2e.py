"""End-to-end deterministic test for the RCA → synthesis → rule agent path."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from git import Actor, Repo
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.miner.core.agents import (
    make_ast_grep_synthesizer,
    make_root_cause_analyzer,
    make_rule_generator,
)
from src.miner.core.context import MinerContext
from src.miner.utils.models import AnchorSynthesisRequest

CASE_SOURCE = "/* missing guard */\nvoid trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


def root_cause_payload() -> dict:
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
                "snippet": "void trigger(void) { danger(1); }",
            }
        ],
        "fixing_pattern": "Establish the guard before invoking danger.",
        "extracted_case_files": ["case1.c"],
    }


def synthesis_request_payload() -> dict:
    return {
        "root_cause": root_cause_payload(),
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


def synthesis_payload() -> dict:
    return {
        "anchors": [
            {
                "id": "danger-call",
                "behavior_weight": 5,
                "query_weight": 5,
                "type": "pattern",
                "query": "danger($ARG);",
                "behavior": BEHAVIOR,
                "inspect_hint": INSPECT_HINT,
            }
        ],
        "case_coverage": [{"path": "case1.c", "anchor_ids": ["danger-call"]}],
        "repo_evidence": [{"anchor_id": "danger-call", "file": "bug.c", "line": 2}],
        "adjustments": [],
    }


def synthesis_run_payload() -> dict:
    return {
        "anchor": synthesis_payload()["anchors"][0],
        "adjustments": [],
    }


def core_payload() -> dict:
    return {
        "category": "SECURITY",
        "language": "c",
        "root_cause_summary": root_cause_payload()["root_cause_summary"],
        "summary": synthesis_request_payload()["summary"],
        "scenarios": {
            "unsafe": ["A dangerous operation executes before its required guard is established."],
            "safe": ["The required guard is established before the dangerous operation executes."],
        },
        "anchors": synthesis_payload()["anchors"],
    }


def prepare_repository(repo_path: Path) -> None:
    repo_path.mkdir()
    (repo_path / "bug.c").write_text(CASE_SOURCE, encoding="utf-8")
    repo = Repo.init(repo_path)
    actor = Actor("VAS Test", "vas-test@example.com")
    repo.index.add(["bug.c"])
    buggy = repo.index.commit("buggy", author=actor, committer=actor)
    repo.create_head("buggy", buggy)

    (repo_path / "bug.c").write_text(
        "void trigger(void) { guard(1); danger(1); }\n",
        encoding="utf-8",
    )
    repo.index.add(["bug.c"])
    fixed = repo.index.commit("fixed", author=actor, committer=actor)
    repo.create_head("fixed", fixed)
    repo.heads.buggy.checkout()


async def test_miner_agents_execute_real_tools_and_complete_the_rule(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    prepare_repository(repo_path)

    rca_step = 0

    async def rca_model(messages, info):
        nonlocal rca_step
        rca_step += 1
        if rca_step == 1:
            return ModelResponse(parts=[ToolCallPart("read_fixed_diff", {})])
        if rca_step == 2:
            assert "guard(1)" in repr(messages)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "cases_write_file",
                        {"path": "case1.c", "content": CASE_SOURCE},
                    )
                ]
            )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, root_cause_payload())])

    rca_agent = make_root_cause_analyzer(
        repo_path,
        cases_dir,
        model=FunctionModel(rca_model),
    )
    rca_result = await rca_agent.run(
        "Analyze the fixture.",
        deps=MinerContext(
            workspace_root=tmp_path,
            repo_path=repo_path,
            cases_dir=cases_dir,
        ),
    )
    root_cause = rca_result.output

    scanned_directories: list[str] = []
    synthesis_step = 0

    async def synthesis_model(messages, info):
        nonlocal synthesis_step
        synthesis_step += 1
        if synthesis_step <= 2:
            target_dir = cases_dir if synthesis_step == 1 else repo_path
            scanned_directories.append(target_dir.as_posix())
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_ast_grep",
                        {
                            "target_dir": target_dir.as_posix(),
                            "query_type": "pattern",
                            "query": "danger($ARG);",
                            "output": "sample",
                        },
                    )
                ]
            )
        assert "'match_count': 1" in repr(messages)
        assert "anchor_intent" in repr(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, synthesis_run_payload())])

    parent_step = 0

    async def rule_model(messages, info):
        nonlocal parent_step
        parent_step += 1
        if parent_step == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "synthesize_ast_grep_anchors",
                        {"request": synthesis_request_payload()},
                    )
                ]
            )
        assert "danger-call" in repr(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, core_payload())])

    synthesizer = make_ast_grep_synthesizer(
        repo_path,
        cases_dir,
        model=FunctionModel(synthesis_model),
    )
    generator = make_rule_generator(
        cases_dir,
        synthesizer,
        model=FunctionModel(rule_model),
    )
    deps = MinerContext(
        workspace_root=tmp_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        anchor_synthesis_request=AnchorSynthesisRequest.model_validate(synthesis_request_payload()),
    )

    rule_result = await generator.run("Generate the fixture rule.", deps=deps)

    assert (cases_dir / "case1.c").read_text(encoding="utf-8") == CASE_SOURCE
    assert scanned_directories == [cases_dir.as_posix(), repo_path.as_posix()]
    assert rule_result.output.anchors[0].id == "danger-call"
    assert deps.anchor_synthesis is not None
    assert rule_result.usage.requests == 5
