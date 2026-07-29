"""Integration tests for the runtime-neutral Pydantic AI adapter."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.miner.core.tasks import make_root_cause_task, make_rule_generation_task
from src.miner.runtime import (
    AgentPhase,
    AgentTask,
    RunLimits,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.runtime.pydantic_ai import (
    PydanticAIOutputValidationError,
    PydanticAIRuntime,
)
from src.miner.utils.models import (
    AnalysisSubject,
    AnchorSynthesisRequest,
    IssueCollectionInfo,
    RootCauseAnalysis,
    VASCoreInfo,
)

SOURCE = "void trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


class ProbeOutput(BaseModel):
    value: str


def _root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The dangerous operation is reached before the guard establishes the invariant.",
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


def _subject(source_root: Path, cases_dir: Path) -> AnalysisSubject:
    return AnalysisSubject(
        type="issue",
        source_root=source_root.resolve().as_posix(),
        cases_dir=cases_dir.resolve().as_posix(),
        grounding_policy="repo_evidence",
    )


def _synthesis_request(root_cause: RootCauseAnalysis) -> AnchorSynthesisRequest:
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


def _core(root_cause: RootCauseAnalysis) -> VASCoreInfo:
    return VASCoreInfo.model_validate(
        {
            "category": "SECURITY",
            "language": "c",
            "root_cause_summary": root_cause.root_cause_summary,
            "summary": "Dangerous operations must run only after the required guard is established.",
            "scenarios": {
                "unsafe": ["The dangerous operation executes before its required guard."],
                "safe": ["The required guard is established before the dangerous operation."],
            },
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
        }
    )


def _probe_task(tmp_path: Path, *, retries: int, validator) -> AgentTask[ProbeOutput]:
    return AgentTask(
        task_id="repair-probe",
        phase=AgentPhase.ISSUE_COLLECTION,
        agent_name="Repair Probe",
        description="Exercises complete Pydantic AI task repair.",
        instructions="Return the requested structured probe output.",
        prompt="Return a probe output.",
        output_type=ProbeOutput,
        context=TaskContext(workspace_root=tmp_path),
        workspace=WorkspacePolicy(cwd=tmp_path),
        required_capabilities=frozenset(),
        limits=RunLimits(request_limit=20, output_retries=retries),
        output_validator=validator,
    )


async def test_pydantic_runtime_uses_native_output_retry_after_deterministic_rejection(
    tmp_path: Path,
):
    requests = 0

    async def model_function(messages, info):
        nonlocal requests
        requests += 1
        if requests == 2:
            rendered = repr(messages)
            assert "Deterministic output validation failed" in rendered
            assert "value must be 'good'" in rendered
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"value": "bad" if requests == 1 else "good"},
                )
            ]
        )

    task = _probe_task(
        tmp_path,
        retries=2,
        validator=lambda value, _context: [] if value.value == "good" else ["value must be 'good'"],
    )
    result = await PydanticAIRuntime(model=FunctionModel(model_function)).run(task)

    assert requests == 2
    assert result.output == ProbeOutput(value="good")
    assert result.usage is not None
    assert result.usage.requests == 2


async def test_pydantic_runtime_stops_after_configured_native_output_retries(tmp_path: Path):
    requests = 0

    async def model_function(_messages, info):
        nonlocal requests
        requests += 1
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"value": "bad"})])

    task = _probe_task(
        tmp_path,
        retries=2,
        validator=lambda _value, _context: ["still invalid"],
    )

    with pytest.raises(PydanticAIOutputValidationError) as exc_info:
        await PydanticAIRuntime(model=FunctionModel(model_function)).run(task)

    assert requests == 3
    assert exc_info.value.attempts == 3
    assert exc_info.value.errors == ("still invalid",)


async def test_root_cause_uses_authoritative_source_tools_and_conditional_fixed_diff(
    tmp_path: Path,
):
    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    repo_path.mkdir()
    cases_dir.mkdir()
    (repo_path / "bug.c").write_text(SOURCE, encoding="utf-8")
    collection = IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="Fixture issue",
        issue_details="A dangerous operation lacks its guard.",
        repo_url="https://github.com/example/project",
        repo_path=repo_path.as_posix(),
        buggy_commit="deadbeef",
        fixed_commit=None,
    )
    root_cause = _root_cause()
    requests = 0

    async def model_function(_messages, info):
        nonlocal requests
        requests += 1
        tool_names = {tool.name for tool in info.function_tools}
        assert {"source_read_file", "source_search_files", "cases_write_file"} <= tool_names
        assert "read_fixed_diff" not in tool_names
        if requests == 1:
            return ModelResponse(
                parts=[ToolCallPart("cases_write_file", {"path": "case1.c", "content": SOURCE})]
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, root_cause.model_dump(mode="json"))]
        )

    task = make_root_cause_task(
        collection,
        workspace_root=tmp_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
    )
    result = await PydanticAIRuntime(model=FunctionModel(model_function)).run(task)

    assert result.output == root_cause
    assert (cases_dir / "case1.c").read_text(encoding="utf-8") == SOURCE


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_rule_generator_delegates_typed_intent_and_consumes_exact_finalized_batch(
    tmp_path: Path,
):
    source_root = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
    root_cause = _root_cause()
    request = _synthesis_request(root_cause)
    core = _core(root_cause)
    parent_requests = 0
    child_requests = 0

    async def model_function(messages, info):
        nonlocal parent_requests, child_requests
        tool_names = {tool.name for tool in info.function_tools}
        if "synthesize_ast_grep_anchors" in tool_names:
            parent_requests += 1
            assert "source_read_file" not in tool_names
            if parent_requests == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "synthesize_ast_grep_anchors",
                            {"request": request.model_dump(mode="json")},
                        )
                    ]
                )
            assert "case_coverage" in repr(messages)
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, core.model_dump(mode="json"))]
            )

        child_requests += 1
        assert {"source_read_file", "cases_read_file", "run_ast_grep"} <= tool_names
        assert "synthesize_ast_grep_anchors" not in tool_names
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "anchor": core.anchors[0].model_dump(mode="json", by_alias=True),
                        "adjustments": [],
                    },
                )
            ]
        )

    task = make_rule_generation_task(
        root_cause,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=_subject(source_root, cases_dir),
    )
    result = await PydanticAIRuntime(model=FunctionModel(model_function)).run(task)

    assert result.output == core
    assert parent_requests == 2
    assert child_requests == 1
    assert result.usage is not None
    assert result.usage.requests == 3
    assert result.metadata["subagent_events"] == [
        {
            "intent_id": "danger-call",
            "runtime_id": "pydantic-ai",
            "model_id": result.model_id,
            "status": "validated",
        }
    ]
