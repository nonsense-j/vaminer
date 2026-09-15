"""Behavior tests for the Pydantic AI runtime."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from src.miner.mining.examples import ExampleSuiteIntake, inspect_example_suite
from src.miner.mining.synthesis import (
    AnchorPlanError,
    AnchorSynthesisSession,
)
from src.miner.mining.tasks import (
    make_ast_grep_synthesis_task,
    make_root_cause_task,
    make_rule_generation_task,
)
from src.miner.models import (
    AnchorIntent,
    AnchorPlan,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCollectionInfo,
    RootCauseAnalysis,
)
from src.miner.runtimes.pydantic import runtime as pydantic_runtime
from src.miner.runtimes.pydantic import telemetry as pydantic_telemetry
from src.miner.runtimes.pydantic.runtime import PydanticAIRuntime
from src.miner.tools.errors import ToolInputError
from src.miner.tools.ast_grep import AstGrepQueryError, AstGrepRunnerError


@pytest.mark.asyncio
async def test_generator_corrects_plans_and_replans_after_admission_failure_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from src.miner.anchors.scanner import AnchorMatch, AnchorRunResult, AnchorScanResult
    from src.miner.mining import synthesis
    from src.miner.mining.validation import anchors as validation

    source, cases = tmp_path / "src", tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    task = make_rule_generation_task(
        _root_cause(), workspace_root=tmp_path, source_root=source,
        cases_dir=cases, grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )

    def scan(anchors, root, _language):
        return AnchorScanResult(root=root, anchor_results=[
            AnchorRunResult(anchor=anchor, matches=[AnchorMatch(
                anchor_id=anchor["id"], query_weight=anchor["query_weight"],
                behavior=anchor["behavior"], inspect_hint=anchor["inspect_hint"],
                file="case1.c" if root == cases else "bug.c", start_line=1, end_line=1,
            )])
            for anchor in anchors
        ])

    monkeypatch.setattr(synthesis, "scan_anchors", scan)
    monkeypatch.setattr(validation, "scan_anchors", scan)
    parent_calls = 0
    child_calls = 0

    def respond(messages, info):
        nonlocal parent_calls, child_calls
        output_tool = info.output_tools[0].name
        if output_tool == "return_anchor_synthesis_delta":
            child_calls += 1
            return ModelResponse(parts=[ToolCallPart(output_tool, {
                "anchor_id": "copy-site", "type": "pattern", "query": "copy();",
                "query_weight": child_calls, "adjustments": [], "plan_suggestion": "",
            })])

        parent_calls += 1
        if parent_calls == 1:
            return ModelResponse(parts=[ToolCallPart("synthesize_anchor_plan", {})])
        if parent_calls in (2, 3, 5):
            history = "\n".join(
                str(part.content) for message in messages for part in message.parts
                if hasattr(part, "content")
            )
            if parent_calls == 2:
                assert "Tool input error" in history
            if parent_calls == 3:
                assert "unknown Case Artifacts: case99.c" in history
            if parent_calls == 5:
                feedback = str(messages[-1].parts[0].content)
                assert "Replan and call synthesize_anchor_plan" in feedback
                assert "case files are not admitted: case1.c" in feedback
                assert "copy();" not in feedback
                assert "case1.c:1" not in feedback
                assert task.prompt in history
                assert "unknown Case Artifacts: case99.c" in history
                assert "copy();" in history
            return ModelResponse(parts=[ToolCallPart("synthesize_anchor_plan", {"plan": {
                "summary": "Copies must preserve bounds.",
                "intents": [{
                    "id": "copy-site", "behavior_weight": 4, "behavior": "Copy a value.",
                    "inspect_hint": "Inspect the bound.",
                    "required_cases": ["case99.c" if parent_calls == 2 else "case1.c"],
                }],
            }})])
        assert parent_calls in (4, 6)
        return ModelResponse(parts=[ToolCallPart(output_tool, {
            "category": "SECURITY", "scenarios": {"unsafe": ["Unbounded copy."], "safe": []},
        })])

    result = await PydanticAIRuntime(model=FunctionModel(respond)).run(task)

    assert parent_calls == 6
    assert child_calls == 2
    assert result.output.anchors[0].query_weight == 2
    assert task.validate_output(result.output) == ()


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
    assert complete.startswith("==> long.c | lines 1-250 of 250 <==\n")
    assert "\\n" not in complete

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


def test_pydantic_read_tools_classify_input_errors(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")

    runtime = PydanticAIRuntime(model=TestModel())
    src_tools = {tool.__name__: tool for tool in runtime._src_tools(source)}
    case_tools = {tool.__name__: tool for tool in runtime._case_tools(cases, writable=False)}

    with pytest.raises(ToolInputError, match="start_line and max_lines must be positive"):
        src_tools["read_src_file"]("bug.c", start_line=0)
    with pytest.raises(ToolInputError, match="start_line must be positive"):
        case_tools["read_case_artifact"]("case1.c", start_line=0)


def test_pydantic_case_writer_rejects_bad_names_as_retryable_feedback(tmp_path: Path):
    cases = tmp_path / "cases"
    cases.mkdir()

    runtime = PydanticAIRuntime(model=TestModel())
    writer = {tool.__name__: tool for tool in runtime._case_tools(cases, writable=True)}[
        "write_case_artifact"
    ]

    assert "caseN.<ext>" in writer.__doc__
    with pytest.raises(ToolInputError, match="caseN"):
        writer("not-a-case.c", "content\n")
    assert not (cases / "not-a-case.c").exists()


@pytest.mark.asyncio
async def test_pydantic_src_tools_return_expected_failures_to_the_model(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")

    runtime = PydanticAIRuntime(model=TestModel())
    src_tools = {tool.__name__: tool for tool in runtime._src_tools(source)}

    with pytest.raises(ToolInputError, match="search pattern must be between"):
        await src_tools["search_src_files"]("")
    with pytest.raises(ToolInputError, match="must stay inside"):
        await src_tools["list_src_files"]("../outside")
    with pytest.raises(ToolInputError, match="regex parse error"):
        await src_tools["search_src_files"]("[", mode="regex")


@pytest.mark.asyncio
async def test_pydantic_navigation_tools_return_fixed_plain_text(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "bug.c").write_text("before\nneedle();\nafter\n", encoding="utf-8")
    tools = {tool.__name__: tool for tool in PydanticAIRuntime._src_tools(source)}

    assert await tools["list_src_files"]() == "bug.c"
    assert await tools["search_src_files"]("needle") == (
        "bug.c-1-before\n"
        "bug.c:2:needle();\n"
        "bug.c-3-after"
    )
    assert tools["read_src_file"]("bug.c", start_line=2, end_line=2) == (
        "==> bug.c | lines 2-2 of 3 | more available <==\nneedle();"
    )


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
                "anchor_id": "copy-site",
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
        (AnchorPlanError("bad plan"), ToolInputError),
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
        (AstGrepQueryError("invalid pattern"), ToolInputError),
        (AstGrepRunnerError("ast-grep returned invalid JSON: decoder failed"), AstGrepRunnerError),
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

    captured: dict[str, object] = {}

    def fail(target_dir, **kwargs):
        captured["target_dir"] = target_dir
        captured.update(kwargs)
        raise failure

    monkeypatch.setattr(pydantic_runtime, "run_ast_grep", fail)
    agent = PydanticAIRuntime(model=TestModel()).build_agent(
        task,
        model=TestModel(),
        final_state=[],
    )
    tool = agent._function_toolset.tools["run_ast_grep_query"]

    with pytest.raises(expected) as raised:
        await tool.function(
            "src",
            "c",
            "pattern",
            "copy($A)",
            output="full",
            sample_size=7,
            debug_query="sexp",
        )

    assert str(raised.value) == str(failure)
    assert captured["target_dir"] == source
    assert captured["output"] == "full"
    assert captured["sample_size"] == 7
    assert captured["debug_query"] == "sexp"


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
