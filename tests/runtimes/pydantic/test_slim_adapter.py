from pathlib import Path

import pytest
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from src.miner.mining.tasks import (
    make_ast_grep_synthesis_task,
    make_root_cause_task,
    make_rule_generation_task,
)
from src.miner.mining.examples import ExampleSuiteIntake, inspect_example_suite
from src.miner.models import (
    AnchorIntent,
    AnchorPlan,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCollectionInfo,
    RootCauseAnalysis,
)
from src.miner.runtimes.pydantic.runtime import PydanticAIRuntime
from src.miner.runtimes.pydantic import runtime as pydantic_runtime
from src.miner.runtimes.pydantic import telemetry as pydantic_telemetry
from src.miner.mining.synthesis import (
    AnchorPlanError,
    AnchorSynthesisLimitError,
    AnchorSynthesisSession,
)
from src.miner.tools.ast_grep import AstGrepQueryError, AstGrepRunnerError


def _root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        language=AstGrepLanguage.C,
        root_cause_summary="unchecked copy",
        analysis="an unbounded length reaches copy",
        buggy_components=[
            BuggyComponent(file="bug.c", start_line=1, end_line=1, role="copy", snippet="copy();")
        ],
        fixing_pattern="bound the length",
        extracted_case_files=["case1.c"],
    )


def test_pydantic_tracing_uses_native_instrumentation_once(monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(pydantic_telemetry, "_INSTRUMENTED", False)
    monkeypatch.setattr(pydantic_telemetry, "configure_tracing", lambda: object())
    monkeypatch.setattr(pydantic_telemetry.Agent, "instrument_all", lambda: calls.append("instrumented"))

    pydantic_telemetry.instrument_tracing()
    pydantic_telemetry.instrument_tracing()

    assert calls == ["instrumented"]


def _tool_names(runtime: PydanticAIRuntime, task) -> set[str]:
    agent = runtime.build_agent(
        task,
        model=TestModel(),
        validation_state=[],
        final_state=[],
    )
    return set(agent._function_toolset.tools)


def test_pydantic_phase_tools_match_closed_authority(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    root_cause = _root_cause()
    runtime = PydanticAIRuntime(model=TestModel())
    src_tools = {tool.__name__: tool for tool in runtime._src_tools(source)}
    assert "already rooted at the analyzed Src Root" in src_tools["list_src_files"].__doc__
    assert source.as_posix() in src_tools["list_src_files"].__doc__
    assert "file or directory" in src_tools["search_src_files"].__doc__
    assert "one-based" in src_tools["read_src_file"].__doc__

    root_task = make_root_cause_task(
        IssueCollectionInfo(
            issue_id="x",
            issue_summary="x",
            issue_details="x",
            repo_url="https://example.invalid/x",
            buggy_commit="a" * 40,
            repo_path=str(source),
        ),
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    assert _tool_names(runtime, root_task) == set(root_task.tools)

    inspection = inspect_example_suite(source)
    suite_task = make_root_cause_task(
        ExampleSuiteIntake(
            **inspection.model_dump(mode="json"),
            snapshot_path=source.as_posix(),
            snapshot_ref="src/input_snapshot",
        ),
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
    )
    assert _tool_names(runtime, suite_task) == {
        "list_src_files",
        "search_src_files",
        "read_src_file",
        "list_case_artifacts",
        "read_case_artifact",
        "write_case_artifact",
    }

    (source / "long.c").write_text("line\n" * 250, encoding="utf-8")
    complete = src_tools["read_src_file"]("long.c", full_file=True)
    assert complete["end_line"] == 250
    assert complete["truncated"] is False

    rule_task = make_rule_generation_task(
        root_cause,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    assert _tool_names(runtime, rule_task) == set(rule_task.tools)

    intent = AnchorIntent(
        id="copy-site",
        behavior_weight=4,
        behavior="copy a runtime length",
        inspect_hint="inspect its bound",
        required_cases=["case1.c"],
    )
    synthesis_task = make_ast_grep_synthesis_task(
        AnchorPlan(summary="bound runtime copies", intents=[intent]),
        intent,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause=root_cause,
    )
    assert _tool_names(runtime, synthesis_task) == set(synthesis_task.tools)
    assert not {"write_file", "write_case_artifact", "bash", "delegate"} & _tool_names(
        runtime, synthesis_task
    )


def test_pydantic_read_tools_surface_correctable_value_errors_as_model_retries(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")

    runtime = PydanticAIRuntime(model=TestModel())
    src_tools = {tool.__name__: tool for tool in runtime._src_tools(source)}
    case_tools = {tool.__name__: tool for tool in runtime._case_tools(cases, writable=False)}

    with pytest.raises(ModelRetry, match="start_line and max_lines must be positive"):
        src_tools["read_src_file"]("bug.c", start_line=0)
    with pytest.raises(ModelRetry, match="start_line must be positive"):
        case_tools["read_case_artifact"]("case1.c", start_line=0)


@pytest.mark.asyncio
async def test_pydantic_src_tools_return_expected_failures_to_the_model(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")

    runtime = PydanticAIRuntime(model=TestModel())
    src_tools = {tool.__name__: tool for tool in runtime._src_tools(source)}

    with pytest.raises(ModelRetry, match="search pattern must be between"):
        await src_tools["search_src_files"]("")
    with pytest.raises(ModelRetry, match="must stay inside"):
        await src_tools["list_src_files"]("../outside")
    with pytest.raises(ToolFailed, match="regex parse error"):
        await src_tools["search_src_files"]("[", mode="regex")


@pytest.mark.asyncio
async def test_pydantic_attempts_include_structured_output_schema_repairs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    intent = AnchorIntent(
        id="copy-site",
        behavior_weight=4,
        behavior="copy a runtime length",
        inspect_hint="inspect its bound",
        required_cases=["case1.c"],
    )
    task = make_ast_grep_synthesis_task(
        AnchorPlan(summary="bound runtime copies", intents=[intent]),
        intent,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause=_root_cause(),
    )
    calls = 0

    def respond(_messages, agent_info):
        nonlocal calls
        calls += 1
        output_tool = agent_info.output_tools[0]
        payload = (
            {}
            if calls == 1
            else {
                "target_anchor_id": "copy-site",
                "type": "pattern",
                "query": "",
                "query_weight": 2,
                "adjustments": [],
                "plan_suggestion": "",
            }
        )
        return ModelResponse(parts=[ToolCallPart(output_tool.name, payload)])

    result = await PydanticAIRuntime(model=FunctionModel(respond)).run(task)

    assert result.attempts == 2
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AnchorPlanError("bad plan"), ModelRetry),
        (AnchorSynthesisLimitError("plan limit"), ToolFailed),
        (RuntimeError("child runtime failed"), RuntimeError),
    ],
)
async def test_pydantic_plan_tool_translates_only_correctable_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: type[Exception],
):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    root_cause = _root_cause()
    task = make_rule_generation_task(
        root_cause,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )

    async def fail(_session, _plan):
        raise failure

    monkeypatch.setattr(AnchorSynthesisSession, "synthesize", fail)
    agent = PydanticAIRuntime(model=TestModel()).build_agent(
        task,
        model=TestModel(),
        validation_state=[],
        final_state=[],
    )
    tool = agent._function_toolset.tools["synthesize_anchor_plan"]
    plan = AnchorPlan(
        summary="copy",
        intents=[
            AnchorIntent(
                id="copy-site",
                behavior_weight=4,
                behavior="copy",
                inspect_hint="bound",
                required_cases=["case1.c"],
            )
        ],
    )

    with pytest.raises(expected, match=str(failure)):
        await tool.function(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AstGrepQueryError("invalid pattern"), ModelRetry),
        (AstGrepRunnerError("ast-grep timed out"), AstGrepRunnerError),
    ],
)
async def test_pydantic_ast_grep_tool_repairs_only_query_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: type[Exception],
):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    intent = AnchorIntent(
        id="copy-site",
        behavior_weight=4,
        behavior="copy",
        inspect_hint="bound",
        required_cases=["case1.c"],
    )
    task = make_ast_grep_synthesis_task(
        AnchorPlan(summary="copy", intents=[intent]),
        intent,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause=_root_cause(),
    )

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(pydantic_runtime, "run_ast_grep", fail)
    agent = PydanticAIRuntime(model=TestModel()).build_agent(
        task,
        model=TestModel(),
        validation_state=[],
        final_state=[],
    )
    tool = agent._function_toolset.tools["run_ast_grep_query"]

    with pytest.raises(expected, match=str(failure)):
        await tool.function("src", "c", "pattern", "copy($A)")


@pytest.mark.asyncio
async def test_pydantic_runtime_does_not_accept_empty_query_after_tool_execution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    intent = AnchorIntent(
        id="copy-site",
        behavior_weight=4,
        behavior="copy",
        inspect_hint="bound",
        required_cases=["case1.c"],
    )
    task = make_ast_grep_synthesis_task(
        AnchorPlan(summary="copy", intents=[intent]),
        intent,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause=_root_cause(),
    )

    def fail(*_args, **_kwargs):
        raise AstGrepRunnerError("ast-grep timed out")

    def respond(_messages, _agent_info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "run_ast_grep_query",
                    {
                        "target": "src",
                        "language": "c",
                        "query_type": "pattern",
                        "query": "copy($A)",
                    },
                )
            ]
        )

    monkeypatch.setattr(pydantic_runtime, "run_ast_grep", fail)
    runtime = PydanticAIRuntime(model=FunctionModel(respond))

    with pytest.raises(AstGrepRunnerError, match="timed out"):
        await runtime.run(task)
