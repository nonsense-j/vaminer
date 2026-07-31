"""Integration tests for the runtime-neutral Pydantic AI adapter."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.miner.mining.tasks import make_root_cause_task, make_rule_generation_task
from src.miner.agent import (
    AgentPhase,
    AgentTask,
    RunLimits,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.runtimes.pydantic.runtime import (
    PydanticAIOutputValidationError,
    PydanticAIRuntime,
)
from src.miner.runtimes.pydantic.hooks import make_cli_hooks
from src.miner.utils.log import run_log_file
from src.miner.models import (
    AnchorSynthesisRequest,
    IssueCollectionInfo,
    RootCauseAnalysis,
    VASCoreInfo,
)
from tests.support.factories import (
    BEHAVIOR,
    INSPECT_HINT,
    SOURCE,
    analysis_subject as _subject,
    root_cause as _root_cause,
    vas_core as _core,
)


class ProbeOutput(BaseModel):
    value: str



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
                },
                {
                    "id": "guard-check",
                    "behavior_weight": 4,
                    "behavior": "Evaluates the guard condition before the dangerous operation.",
                    "inspect_hint": "Inspect whether the guard establishes the required invariant.",
                    "required_cases": ["case1.c"],
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


async def test_root_cause_uses_workspace_tools_and_conditional_fixed_diff(
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
        assert tool_names == {
            "read_file",
            "search_files",
            "find_files",
            "write_file",
            "read_tool_result",
        }
        if requests == 1:
            return ModelResponse(
                parts=[ToolCallPart("write_file", {"path": "cases/case1.c", "content": SOURCE})]
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
async def test_rule_generator_delegates_typed_intent_and_receives_structural_results(
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
    core_payload = _core(root_cause).model_dump(mode="json", by_alias=True)
    core_payload["anchors"].append(
        {
            "id": "guard-check",
            "behavior_weight": 4,
            "query_weight": 3,
            "type": "pattern",
            "query": "danger($ARG);",
            "behavior": "Evaluates the guard condition before the dangerous operation.",
            "inspect_hint": "Inspect whether the guard establishes the required invariant.",
        }
    )
    core = VASCoreInfo.model_validate(core_payload)
    parent_requests = 0
    child_requests = 0

    async def model_function(messages, info):
        nonlocal parent_requests, child_requests
        tool_names = {tool.name for tool in info.function_tools}
        if "synthesize_ast_grep_anchors" in tool_names:
            parent_requests += 1
            assert tool_names == {
                "read_file",
                "search_files",
                "find_files",
                "synthesize_ast_grep_anchors",
                "read_tool_result",
            }
            if parent_requests == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "synthesize_ast_grep_anchors",
                            {"request": request.model_dump(mode="json")},
                        )
                    ]
                )
            rendered = repr(messages)
            assert "adjustments" in rendered
            assert "plan_suggestion" in rendered
            assert "Consider merging danger-call and guard-check" in rendered
            assert "case_coverage" not in rendered
            assert "missing($ARG)" in rendered
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, core.model_dump(mode="json"))]
            )

        child_requests += 1
        assert tool_names == {
            "source_read_file",
            "source_search_files",
            "source_find_files",
            "cases_read_file",
            "cases_search_files",
            "cases_find_files",
            "skill_read_file",
            "run_ast_grep_query",
            "read_tool_result",
        }
        assert "run_command" not in tool_names
        runner_tool = next(
            tool
            for tool in info.function_tools
            if tool.name == "run_ast_grep_query"
        )
        assert set(runner_tool.parameters_json_schema["properties"]) == {
            "target",
            "language",
            "query_type",
            "query",
            "output",
            "sample_size",
        }
        assert runner_tool.parameters_json_schema["properties"]["target"]["enum"] == [
            "source",
            "cases",
        ]
        rendered = repr(messages)
        assert '"anchor_plan"' in rendered
        assert '"target_anchor_id"' in rendered
        assert '"danger-call"' in rendered
        assert '"guard-check"' in rendered
        target_id = (
            "guard-check"
            if '"target_anchor_id": "guard-check"' in rendered
            else "danger-call"
        )
        target_anchor = next(anchor for anchor in core.anchors if anchor.id == target_id)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "anchor": target_anchor.model_copy(
                            update={"query": "missing($ARG);"}
                        ).model_dump(mode="json", by_alias=True),
                        "adjustments": ["No deterministic child gate should reject this result."],
                        "plan_suggestion": (
                            "Consider merging danger-call and guard-check because "
                            "the standalone high-value query is too broad."
                            if target_id == "danger-call"
                            else ""
                        ),
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
    with run_log_file(
        tmp_path / "logs",
        "VAS-TEST",
        input_id="CVE-TEST",
        trace_id="trace-1",
        runtime="pydantic-ai",
    ) as log_path:
        result = await PydanticAIRuntime(
            model=FunctionModel(model_function),
            additional_capabilities=(make_cli_hooks(emit_console=False),),
        ).run(task)

    assert result.output == core
    assert parent_requests == 2
    assert child_requests == 2
    assert result.usage is not None
    assert result.usage.requests == 4
    assert result.metadata["subagent_events"] == [
        {
            "intent_id": "danger-call",
            "runtime_id": "pydantic-ai",
            "model_id": result.model_id,
            "status": "completed",
        },
        {
            "intent_id": "guard-check",
            "runtime_id": "pydantic-ai",
            "model_id": result.model_id,
            "status": "completed",
        }
    ]
    rendered_log = log_path.read_text(encoding="utf-8")
    assert "AST-Grep Synthesizer Started" in rendered_log
    assert "AST-Grep Synthesizer Finished" in rendered_log
    assert "danger-call" in rendered_log
