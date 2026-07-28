"""Tests for isolated per-anchor synthesis and deterministic batch aggregation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.miner.core import agents as agent_module
from src.miner.core.context import MinerContext
from src.miner.core.validation import (
    aggregate_anchor_synthesis_runs,
    validate_anchor_synthesis_run,
)
from src.miner.utils.models import (
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
    RootCauseAnalysis,
)

DANGER_BEHAVIOR = "Calls the dangerous operation with one argument."
DANGER_HINT = "Inspect whether the required guard applies before the matched call."
AUDIT_BEHAVIOR = "Records the value passed to the dangerous operation."
AUDIT_HINT = "Trace whether the recorded value corresponds to the dangerous operation."


def _prepare_sources(tmp_path: Path) -> tuple[Path, Path, RootCauseAnalysis]:
    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    repo_path.mkdir()
    cases_dir.mkdir()
    (repo_path / "bug.c").write_text(
        "void trigger(void) { danger(1); }\n" "void record(void) { audit(1); }\n",
        encoding="utf-8",
    )
    (cases_dir / "case1.c").write_text(
        "void first(void) { danger(1); audit(1); }\n",
        encoding="utf-8",
    )
    (cases_dir / "case2.c").write_text(
        "void second(void) { danger(2); }\n",
        encoding="utf-8",
    )
    root_cause = RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The dangerous operation and its associated audit site expose the causal chain.",
            "buggy_components": [
                {
                    "file": "bug.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": "void trigger(void) { danger(1); }",
                },
                {
                    "file": "bug.c",
                    "start_line": 2,
                    "end_line": 2,
                    "role": "Records the associated value.",
                    "snippet": "void record(void) { audit(1); }",
                },
            ],
            "fixing_pattern": "Establish the required guard before invoking danger.",
            "extracted_case_files": ["case1.c", "case2.c"],
        }
    )
    return repo_path, cases_dir, root_cause


def _request(root_cause: RootCauseAnalysis) -> AnchorSynthesisRequest:
    return AnchorSynthesisRequest.model_validate(
        {
            "root_cause": root_cause.model_dump(mode="json"),
            "summary": "Dangerous operations must run only after the required guard is established.",
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": DANGER_BEHAVIOR,
                    "inspect_hint": DANGER_HINT,
                    "required_cases": ["case1.c", "case2.c"],
                },
                {
                    "id": "audit-call",
                    "behavior_weight": 3,
                    "behavior": AUDIT_BEHAVIOR,
                    "inspect_hint": AUDIT_HINT,
                    "required_cases": ["case1.c"],
                },
            ],
        }
    )


def _run_result(anchor_id: str) -> AnchorSynthesisRunResult:
    if anchor_id == "danger-call":
        return AnchorSynthesisRunResult.model_validate(
            {
                "anchor": {
                    "id": anchor_id,
                    "behavior_weight": 5,
                    "query_weight": 5,
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": DANGER_BEHAVIOR,
                    "inspect_hint": DANGER_HINT,
                },
                "adjustments": ["Generalized the call argument with a metavariable."],
            }
        )
    return AnchorSynthesisRunResult.model_validate(
        {
            "anchor": {
                "id": anchor_id,
                "behavior_weight": 3,
                "query_weight": 3,
                "type": "pattern",
                "query": "audit($ARG);",
                "behavior": AUDIT_BEHAVIOR,
                "inspect_hint": AUDIT_HINT,
            },
            "adjustments": [],
        }
    )


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_per_anchor_validation_ignores_other_intents_case_contract(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)
    audit_request = AnchorSynthesisRunRequest(
        root_cause=root_cause,
        summary=request.summary,
        anchor_intent=request.anchor_intents[1],
    )

    errors = validate_anchor_synthesis_run(
        _run_result("audit-call"),
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        run_request=audit_request,
    )

    assert errors == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_batch_aggregation_restores_request_order_and_derives_metadata(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)

    result = aggregate_anchor_synthesis_runs(
        request,
        [_run_result("audit-call"), _run_result("danger-call")],
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
    )

    assert [anchor.id for anchor in result.anchors] == ["danger-call", "audit-call"]
    assert [item.model_dump() for item in result.case_coverage] == [
        {"path": "case1.c", "anchor_ids": ["danger-call", "audit-call"]},
        {"path": "case2.c", "anchor_ids": ["danger-call"]},
    ]
    assert [item.model_dump() for item in result.repo_evidence] == [
        {"anchor_id": "danger-call", "file": "bug.c", "line": 1},
        {"anchor_id": "audit-call", "file": "bug.c", "line": 2},
    ]
    assert result.adjustments == [
        "danger-call: Generalized the call argument with a metavariable.",
    ]


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_rule_generator_runs_each_anchor_with_an_isolated_context(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)
    model_requests = {"danger-call": 0, "audit-call": 0}

    async def synthesis_model(messages, info):
        rendered = repr(messages)
        anchor_id = "danger-call" if "danger-call" in rendered else "audit-call"
        other_id = "audit-call" if anchor_id == "danger-call" else "danger-call"
        assert other_id not in rendered
        model_requests[anchor_id] += 1
        run = _run_result(anchor_id)
        if "'match_count':" not in rendered:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_ast_grep",
                        {
                            "target_dir": cases_dir.as_posix(),
                            "query_type": "pattern",
                            "query": run.anchor.query,
                            "output": "count",
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    run.model_dump(mode="json", by_alias=True),
                )
            ]
        )

    parent_requests = 0

    async def rule_model(_messages, info):
        nonlocal parent_requests
        parent_requests += 1
        if parent_requests == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "synthesize_ast_grep_anchors",
                        {"request": request.model_dump(mode="json")},
                    )
                ]
            )
        assert deps.anchor_synthesis is not None
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "category": "SECURITY",
                        "language": "c",
                        "root_cause_summary": root_cause.root_cause_summary,
                        "summary": request.summary,
                        "scenarios": {
                            "unsafe": ["The dangerous operation executes without its required guard."],
                            "safe": ["The required guard is established before the dangerous operation."],
                        },
                        "anchors": [
                            anchor.model_dump(mode="json", by_alias=True) for anchor in deps.anchor_synthesis.anchors
                        ],
                    },
                )
            ]
        )

    synthesizer = agent_module.make_ast_grep_synthesizer(
        repo_path,
        cases_dir,
        model=FunctionModel(synthesis_model),
    )
    generator = agent_module.make_rule_generator(
        cases_dir,
        synthesizer,
        model=FunctionModel(rule_model),
    )
    deps = MinerContext(
        workspace_root=tmp_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
    )

    result = await generator.run("Generate the fixture rule.", deps=deps)

    assert model_requests == {"danger-call": 2, "audit-call": 2}
    assert [anchor.id for anchor in result.output.anchors] == ["danger-call", "audit-call"]
    assert result.usage.requests == 6
