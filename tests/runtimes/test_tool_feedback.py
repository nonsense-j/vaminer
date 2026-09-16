"""Exercise feedback through real SDK/MCP dispatch, including argument schemas."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from src.miner.agent import TurnBudgetExceeded
from src.miner.mining.tasks import (
    make_ast_grep_synthesis_task, make_issue_collection_task, make_root_cause_task, make_rule_generation_task,
)
from src.miner.models import AnchorIntent, AnchorPlan, AstGrepLanguage, BuggyComponent, GroundingPolicy, IssueCollectionInfo, RootCauseAnalysis
from src.miner.runtimes.claude.mcp import MCPProfile, MCPServerSettings, build_server
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.policy import InvocationFiles
from src.miner.runtimes.claude.process import ProcessResult
from src.miner.runtimes.claude.runtime import ClaudeCodeRuntime
from src.miner.runtimes.claude.errors import (
    ClaudeCodeChildSynthesisError, ClaudeCodeToolExecutionError,
)
from src.miner.runtimes.pydantic import runtime as sdk
from src.miner.runtimes.pydantic.capabilities import tool_feedback_capability
from src.miner.tools.errors import ToolExecutionError, ToolInputError


@pytest.fixture
def synthesis_task(tmp_path: Path):
    source, cases = tmp_path / "src", tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("void f() { copy(); }\n")
    (cases / "case1.c").write_text("void f() { copy(); }\n")
    rca = RootCauseAnalysis(
        language=AstGrepLanguage.C, root_cause_summary="unchecked copy", analysis="unchecked copy",
        buggy_components=[BuggyComponent(file="bug.c", start_line=1, end_line=1, role="copy", snippet="void f() { copy(); }")],
        fixing_pattern="bound the copy", extracted_case_files=["case1.c"],
    )
    intent = AnchorIntent(id="copy", behavior_weight=4, behavior="copy", inspect_hint="bound", required_cases=["case1.c"])
    return make_ast_grep_synthesis_task(
        AnchorPlan(summary="bound copy", intents=[intent]), intent, workspace_root=tmp_path,
        source_root=source, cases_dir=cases, root_cause=rca, grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )


def _output(info):
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "anchor_id": "copy", "type": "pattern", "query": "copy()", "query_weight": 3,
        "adjustments": [], "plan_suggestion": "",
    })])


@pytest.mark.parametrize(("name", "args", "diagnostic"), [
    ("read_src_file", {}, "path"),  # Missing schema field.
    ("read_src_file", {"path": "missing.c"}, "does not exist"),
    ("run_ast_grep_query", {"target": "src", "language": "c", "query_type": "rule", "query": "kind: not_a_kind"}, "Cannot parse rule"),
])
async def test_repeated_bad_arguments_remain_feedback(synthesis_task, name, args, diagnostic):
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if 1 < calls <= 5:
            result = messages[-1].parts[0]
            assert isinstance(result, ToolReturnPart)
            assert result.outcome == "failed"
            assert diagnostic in result.content
        if calls <= 4:
            return ModelResponse(parts=[ToolCallPart(name, args)])
        if calls == 5:
            return ModelResponse(parts=[ToolCallPart("read_src_file", {"path": "bug.c"})])
        assert "copy();" in messages[-1].parts[0].content
        return _output(info)

    runtime = sdk.PydanticAIRuntime(model=FunctionModel(respond))
    result = await runtime.run(synthesis_task)
    assert calls == 6
    assert result.attempts == 1  # Only one final output was submitted.


@pytest.mark.parametrize("name", ["read_src_file", "nonexistent_tool"])
async def test_persistent_bad_calls_end_at_request_limit(synthesis_task, name):
    task = replace(synthesis_task, limit_override=replace(synthesis_task.limits, request_limit=5))
    calls = 0

    def respond(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[ToolCallPart(name, {"path": "missing.c"})])

    with pytest.raises(TurnBudgetExceeded, match="budget of 5"):
        await sdk.PydanticAIRuntime(model=FunctionModel(respond)).run(task)
    assert calls == 5


@pytest.mark.parametrize("exhausts_turns", [False, True])
async def test_claude_native_output_failure_resumes_without_resetting_turns(synthesis_task, exhausts_turns):
    task = replace(synthesis_task, limit_override=replace(synthesis_task.limits, request_limit=5))
    calls = 0
    runtime = ClaudeCodeRuntime(ClaudeCodeConfig(executable=sys.executable))

    class Runner:
        async def run(self, argv, **kwargs):
            nonlocal calls
            calls += 1
            assert int(argv[argv.index("--max-turns") + 1]) == 6 - calls
            assert ("--resume" in argv) == (calls > 1)
            event = {"type": "result", "num_turns": 1}
            if calls < 5:
                event.update(subtype="error_max_structured_output_retries", errors=["anchor_id is missing"])
            elif exhausts_turns:
                event.update(subtype="error_max_turns", is_error=True)
            else:
                event.update(subtype="success", structured_output={
                    "anchor_id": "copy", "type": "pattern", "query": "copy()",
                    "query_weight": 3, "adjustments": [], "plan_suggestion": "",
                })
            return ProcessResult(json.dumps(event), "", int(event["subtype"] != "success"), 1)

    runtime._runner = Runner()
    if exhausts_turns:
        with pytest.raises(TurnBudgetExceeded, match="budget of 5"):
            await runtime.run(task)
    else:
        result = await runtime.run(task)
        assert result.attempts == 5
    assert calls == 5


@pytest.mark.parametrize("runtime_kind", ["sdk", "claude"])
@pytest.mark.parametrize(("phase", "schema_failure", "recovers"), [
    ("issue", True, False), ("root_cause", True, False), ("rule", True, False),
    ("synthesis", True, False), ("synthesis", True, True),
    ("synthesis", False, False), ("synthesis", False, True),
])
async def test_output_repairs_share_the_turn_budget(synthesis_task, runtime_kind, phase, schema_failure, recovers):
    authority = synthesis_task.authority
    scope = dict(workspace_root=synthesis_task.workspace_root, source_root=authority.source_root,
                 cases_dir=authority.cases_dir, grounding_policy=authority.grounding_policy)
    if phase == "issue":
        task = make_issue_collection_task("CVE-2026-1234", workspace_root=synthesis_task.workspace_root)
    elif phase == "root_cause":
        issue = IssueCollectionInfo(issue_id="test", issue_summary="copy", issue_details="copy",
                                    repo_url="https://github.com/o/r", repo_path=str(authority.source_root),
                                    buggy_commit="a" * 40)
        task = make_root_cause_task(issue, **scope)
    elif phase == "rule":
        task = make_rule_generation_task(authority.root_cause, **scope)
    else:
        task = synthesis_task
    task = replace(task, limit_override=replace(task.limits, request_limit=5))
    calls = 0

    def payload():
        nonlocal calls
        calls += 1
        valid = recovers and calls in {5, 6}
        if not valid and schema_failure:
            return {}
        return {
            "anchor_id": "copy" if valid else "wrong", "type": "pattern", "query": "copy()",
            "query_weight": 3, "adjustments": [], "plan_suggestion": "",
        }

    if runtime_kind == "sdk":
        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload())])
        runtime = sdk.PydanticAIRuntime(model=FunctionModel(respond))
        limit_error = TurnBudgetExceeded
    else:
        class Runner:
            async def run(self, argv, **kwargs):
                expected_turns = 5 - calls if calls < 5 else 1
                assert int(argv[argv.index("--max-turns") + 1]) == expected_turns
                return ProcessResult(stdout=json.dumps({
                    "type": "result", "subtype": "success", "structured_output": payload(), "num_turns": 1,
                }), stderr="", returncode=0, duration_ms=1)
        runtime = ClaudeCodeRuntime(ClaudeCodeConfig(executable=sys.executable))
        runtime._runner = Runner()
        limit_error = TurnBudgetExceeded

    if recovers:
        session = runtime.open_session(task)
        try:
            result = await session.send(task.prompt)
            assert result.output.anchor_id == "copy"
            assert result.attempts == 5
            with pytest.raises(limit_error):
                await session.send("Check the output again.")
            final = await session.send(
                "Return the terminal fallback.",
                request_limit_extension=1,
            )
            assert final.output.anchor_id == "copy"
            with pytest.raises(limit_error):
                await session.send(
                    "Try another terminal fallback.",
                    request_limit_extension=1,
                )
        finally:
            await session.close()
    else:
        with pytest.raises(limit_error):
            await runtime.run(task)
    assert calls == (6 if recovers else 5)


@pytest.mark.parametrize("error", [
    ValueError("tool implementation bug"), TypeError("tool implementation bug"),
    PermissionError("tool implementation bug"),
])
async def test_sdk_does_not_hide_tool_bugs(synthesis_task, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(sdk, "run_query", fail)
    def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart("run_ast_grep_query", {
            "target": "src", "language": "c", "query_type": "pattern", "query": "copy()",
        })])
    with pytest.raises(type(error), match=str(error)):
        await sdk.PydanticAIRuntime(model=FunctionModel(respond)).run(synthesis_task)


async def test_model_protocol_failure_is_not_three_output_attempts(synthesis_task):
    def respond(messages, info):
        raise UnexpectedModelBehavior("broken model protocol")
    with pytest.raises(sdk.PydanticAIRuntimeError) as raised:
        await sdk.PydanticAIRuntime(model=FunctionModel(respond)).run(synthesis_task)
    assert "3 attempts" not in str(raised.value)


async def test_deferred_tools_share_the_feedback_policy():
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Capability

    def deferred_lookup(path: str):
        raise ToolInputError("choose another path")

    calls = 0
    def respond(messages, info):
        nonlocal calls
        calls += 1
        names = {tool.name for tool in info.function_tools}
        if "deferred_lookup" not in names:
            return ModelResponse(parts=[ToolCallPart("load_capability", {"capability_id": "lookup"})])
        if calls < 6:
            return ModelResponse(parts=[ToolCallPart("deferred_lookup", {"path": "bad"})])
        from pydantic_ai.messages import TextPart
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(model=FunctionModel(respond), capabilities=[
        tool_feedback_capability(), Capability(id="lookup", description="lookup", tools=[deferred_lookup], defer_loading=True),
    ])
    result = await agent.run("lookup")
    assert result.output == "done"


async def test_mcp_schema_and_tool_errors_are_results_without_fatal_receipt(
    synthesis_task,
    tmp_path,
    monkeypatch,
):
    from mcp.server import MCPServer
    from mcp.types import CallToolRequestParams
    from src.miner.runtimes.claude import mcp

    failure = tmp_path / "tool-failure.json"
    server = build_server(settings=MCPServerSettings(
        profile=MCPProfile.AST_GREP_SYNTHESIS, workspace_root=tmp_path,
        source_root=synthesis_task.authority.source_root, cases_dir=synthesis_task.authority.cases_dir,
        skill_root=synthesis_task.authority.skill_root, tool_failure_path=failure,
    ), fast_mcp_factory=MCPServer)
    # Use the actual MCP tool dispatcher so argument-schema validation is covered.
    for args in ({}, {"path": None}, {"path": "missing.c"}, {"path": "bug.c", "start_line": 0}):
        result = await server._handle_call_tool(None, CallToolRequestParams(name="read_src_file", arguments=args))
        assert result.is_error
        assert result.content
        assert not failure.exists()
    result = await server._handle_call_tool(
        None, CallToolRequestParams(name="read_src_file", arguments={"path": "bug.c"}),
    )
    assert not result.is_error

    def fail_query(*_args, **_kwargs):
        raise ToolExecutionError("native ast-grep diagnostic")

    monkeypatch.setattr(mcp, "run_query", fail_query)
    result = await server._handle_call_tool(None, CallToolRequestParams(
        name="run_ast_grep_query",
        arguments={
            "target": "src",
            "language": "c",
            "query_type": "pattern",
            "query": "copy($A)",
        },
    ))
    assert result.is_error
    assert "native ast-grep diagnostic" in str(result.content)
    assert not failure.exists()


@pytest.mark.parametrize("profile", list(MCPProfile))
async def test_all_mcp_profiles_record_unexpected_failures(synthesis_task, tmp_path, monkeypatch, profile):
    from mcp.server import MCPServer
    from mcp.types import CallToolRequestParams
    from src.miner.runtimes.claude import mcp

    def fail(*args, **kwargs):
        raise ValueError("implementation bug")

    monkeypatch.setattr(mcp, "list_cases_impl", fail)
    monkeypatch.setattr(mcp, "fetch_cve_plain", fail)
    failure = tmp_path / "tool-failure.json"
    server = build_server(settings=MCPServerSettings(
        profile=profile, workspace_root=tmp_path, source_root=synthesis_task.authority.source_root,
        cases_dir=synthesis_task.authority.cases_dir, skill_root=synthesis_task.authority.skill_root,
        tool_failure_path=failure,
    ), fast_mcp_factory=MCPServer, rule_synthesis_handler=fail)
    name, args = ("fetch_cve", {"cve_id": "CVE-2026-1234"}) if profile is MCPProfile.ISSUE else ("list_case_artifacts", {})
    result = await server._handle_call_tool(None, CallToolRequestParams(name=name, arguments=args))
    assert result.is_error
    with pytest.raises(ClaudeCodeToolExecutionError, match="implementation bug"):
        ClaudeCodeRuntime._raise_tool_failure(failure)


@pytest.mark.parametrize("child_failure", [False, True])
async def test_claude_terminates_process_after_fatal_receipt(tmp_path, monkeypatch, child_failure):
    failure = tmp_path / "failure.json"
    files = InvocationFiles(
        system_prompt=tmp_path, settings=tmp_path, mcp=tmp_path, session_id="test", trace_state=tmp_path,
        synthesis_failure=failure if child_failure else None,
        tool_failure=None if child_failure else failure,
    )
    processes = []
    create_process = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    script = (
        "import pathlib, sys, time\n"
        "sys.stdin.read()\n"
        f"pathlib.Path(sys.argv[1]).write_text({json.dumps({'type': 'RuntimeError', 'message': 'fatal tool bug'})!r})\n"
        "time.sleep(30)\n"
    )
    error = ClaudeCodeChildSynthesisError if child_failure else ClaudeCodeToolExecutionError
    with pytest.raises(error, match="fatal tool bug"):
        await asyncio.wait_for(ClaudeCodeRuntime()._run_process(
            [sys.executable, "-c", script, str(failure)], files=files, cwd=tmp_path,
            environment={}, prompt="run", timeout_seconds=10, stdout_line_handler=lambda line: None,
        ), timeout=5)
    assert len(processes) == 1
    assert processes[0].returncode is not None
