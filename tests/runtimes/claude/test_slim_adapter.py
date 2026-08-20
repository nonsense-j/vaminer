import asyncio
import json
import os
import sys
import uuid
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from src.miner.mining.tasks import (
    make_ast_grep_synthesis_task,
    make_root_cause_task,
    make_rule_generation_task,
)
from src.miner.mining.examples import ExampleSuiteIntake, inspect_example_suite
from src.miner.models import (
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCollectionInfo,
    RootCauseAnalysis,
)
from src.miner.runtimes.claude.config import LANGFUSE_CLAUDE_PLUGIN_ID, ClaudeCodeConfig
from src.miner.runtimes.claude.errors import (
    ClaudeCodeChildSynthesisError,
    ClaudeCodeConfigurationError,
    ClaudeCodeProtocolError,
    ClaudeCodeToolExecutionError,
)
from src.miner.runtimes.claude import mcp as mcp_module
from src.miner.runtimes.claude.mcp import (
    SYNTHESIS_CONTEXT_ENV,
    SYNTHESIS_LOG_ENV,
    TOOL_FAILURE_ENV,
    MCPProfile,
    MCPServerSettings,
    build_server,
)
from src.miner.runtimes.claude import policy as policy_module
from src.miner.runtimes.claude.policy import PolicyCompiler, cleanup_session_transcript
from src.miner.runtimes.claude.process import ProcessResult, ProcessRunner
from src.miner.runtimes.claude.protocol import ClaudeStreamDecoder, decode_claude_stream
from src.miner.runtimes.claude.runtime import ClaudeCodeRuntime, _relay_synthesis_log
from src.miner.tools.ast_grep import AstGrepRunnerError
from src.miner.utils.log import RuntimeLog


class FakeServer:
    def __init__(self, _name: str) -> None:
        self.tools = {}

    def tool(self, *, name: str):
        def register(function):
            self.tools[name] = function
            return function

        return register


def _rca() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        language=AstGrepLanguage.C,
        root_cause_summary="copy",
        analysis="copy",
        buggy_components=[BuggyComponent(file="bug.c", start_line=1, end_line=1, role="copy", snippet="copy();")],
        fixing_pattern="bound",
        extracted_case_files=["case1.c"],
    )


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    return workspace, source, cases


def test_policy_inherits_environment_and_exposes_only_typed_filesystem_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace, source, cases = _workspace(tmp_path)
    collection = IssueCollectionInfo(
        issue_id="x",
        issue_summary="x",
        issue_details="x",
        repo_url="https://example.invalid/x",
        buggy_commit="a" * 40,
        repo_path=str(source),
    )
    task = make_root_cause_task(
        collection,
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    monkeypatch.setenv("VAMINER_TEST_SENTINEL", "visible")
    compiler = PolicyCompiler(ClaudeCodeConfig(executable="/bin/true"))
    environment = compiler.environment()
    policy = compiler.compile(task)
    assert environment["VAMINER_TEST_SENTINEL"] == "visible"
    assert environment["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert not {"Read", "Grep", "Glob", "Write", "Edit", "Bash"} & set(policy.allowed_tools)
    assert "mcp__vaminer__write_case_artifact" in policy.allowed_tools

    temporary = tmp_path / "invocation"
    temporary.mkdir()
    files = compiler.materialize(temporary, task=task, policy=policy, executable="/bin/true", model_id="session")
    materialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (files.system_prompt, files.settings, files.mcp)
    )
    system_prompt = files.system_prompt.read_text(encoding="utf-8")
    assert "`mcp__vaminer__<tool_name>`" in system_prompt
    assert "VAMINER_TEST_SENTINEL" not in materialized
    assert "visible" not in materialized
    argv = compiler.argv(executable="/bin/true", task=task, policy=policy, files=files, model_id="session")
    assert "--strict-mcp-config" in argv
    assert "--skip-safe-check" not in argv
    assert "--no-session-persistence" not in argv
    assert argv[argv.index("--session-id") + 1] == files.session_id
    assert str(uuid.UUID(files.session_id)) == files.session_id
    resumed_argv = compiler.argv(
        executable="/bin/true",
        task=task,
        policy=policy,
        files=files,
        model_id="session",
        resume=True,
    )
    assert "--session-id" not in resumed_argv
    assert resumed_argv[resumed_argv.index("--resume") + 1] == files.session_id
    assert "--disallowedTools" not in argv
    assert "stream-json" in argv
    assert files.receipt is None
    assert files.trace_state == temporary / "langfuse-state"
    assert json.loads(files.settings.read_text(encoding="utf-8"))["enabledPlugins"] == {
        LANGFUSE_CLAUDE_PLUGIN_ID: False
    }
    mcp_config = json.loads(files.mcp.read_text(encoding="utf-8"))
    assert mcp_config["mcpServers"]["vaminer"]["command"] == str(Path(sys.executable).absolute())

    codeagent_compiler = PolicyCompiler(ClaudeCodeConfig(executable="codeagent"))
    codeagent_argv = codeagent_compiler.argv(
        executable="/usr/local/bin/codeagent",
        task=task,
        policy=policy,
        files=files,
        model_id="session",
    )
    assert codeagent_argv[1] == "--print"
    assert "--skip-safe-check" not in codeagent_argv

    default_codeagent = ClaudeCodeConfig(
        executable="codeagent",
        display_name="CodeAgent",
        effort="high",
    )
    assert default_codeagent.config_dir_name == ".cac"
    assert default_codeagent.display_name == "CodeAgent"

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
    suite_policy = compiler.compile(suite_task)
    assert {"list_src_files", "search_src_files", "read_src_file"} <= set(suite_policy.mcp_tools)

    traceparent = "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01"
    monkeypatch.setattr(
        policy_module,
        "claude_trace_environment",
        lambda: {"CC_LANGFUSE_TRACEPARENT": traceparent},
    )
    assert compiler.environment()["CC_LANGFUSE_TRACEPARENT"] == traceparent
    traced_temporary = tmp_path / "traced-invocation"
    traced_temporary.mkdir()
    traced_files = compiler.materialize(
        traced_temporary,
        task=task,
        policy=policy,
        executable="/bin/true",
        model_id="session",
    )
    assert json.loads(traced_files.settings.read_text(encoding="utf-8"))["enabledPlugins"] == {
        LANGFUSE_CLAUDE_PLUGIN_ID: False
    }


def test_rule_policy_materializes_synthesizer_log_channel(tmp_path: Path):
    workspace, source, cases = _workspace(tmp_path)
    task = make_rule_generation_task(
        _rca(),
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    compiler = PolicyCompiler(
        ClaudeCodeConfig(
            executable="codeagent",
            effort="xhigh",
            display_name="CodeAgent",
        )
    )
    temporary = tmp_path / "rule-invocation"
    temporary.mkdir()
    files = compiler.materialize(
        temporary,
        task=task,
        policy=compiler.compile(task),
        executable="/bin/true",
        model_id="session",
    )

    assert files.synthesis_log is not None and files.synthesis_log.is_file()
    mcp_environment = json.loads(files.mcp.read_text(encoding="utf-8"))["mcpServers"]["vaminer"]["env"]
    assert mcp_environment[SYNTHESIS_LOG_ENV] == str(files.synthesis_log)
    synthesis_context = json.loads(Path(mcp_environment[SYNTHESIS_CONTEXT_ENV]).read_text(encoding="utf-8"))
    assert synthesis_context["effort"] == "xhigh"
    assert synthesis_context["display_name"] == "CodeAgent"


@pytest.mark.asyncio
async def test_synthesizer_log_relay_forwards_complete_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    synthesis_log = tmp_path / "synthesis.log"
    synthesis_log.write_text("first panel line\nsecond panel line\n", encoding="utf-8")
    received = []
    runtime_log = RuntimeLog(emit_console=False)
    monkeypatch.setattr(runtime_log, "relay", received.append)
    finished = asyncio.Event()
    finished.set()

    await _relay_synthesis_log(synthesis_log, finished, runtime_log=runtime_log)

    assert received == ["first panel line", "second panel line"]


def test_mcp_profiles_register_exact_typed_tools(tmp_path: Path):
    workspace, source, cases = _workspace(tmp_path)
    root = build_server(
        settings=MCPServerSettings(
            profile=MCPProfile.ROOT_CAUSE,
            workspace_root=workspace,
            source_root=source,
            cases_dir=cases,
        ),
        fast_mcp_factory=FakeServer,
    )
    assert set(root.tools) == {
        "list_src_files",
        "search_src_files",
        "read_src_file",
        "list_case_artifacts",
        "read_case_artifact",
        "write_case_artifact",
    }
    assert "already rooted at the analyzed Src Root" in root.tools["list_src_files"].__doc__
    assert source.as_posix() in root.tools["list_src_files"].__doc__
    assert "file or directory" in root.tools["search_src_files"].__doc__
    assert "one-based" in root.tools["read_src_file"].__doc__
    (source / "long.c").write_text("line\n" * 250, encoding="utf-8")
    complete = root.tools["read_src_file"]("long.c", full_file=True)
    assert complete["end_line"] == 250
    assert complete["truncated"] is False
    synthesis = build_server(
        settings=MCPServerSettings(
            profile=MCPProfile.AST_GREP_SYNTHESIS,
            workspace_root=workspace,
            source_root=source,
            cases_dir=cases,
            skill_root=Path("src/miner/skills/ast-grep").resolve(),
        ),
        fast_mcp_factory=FakeServer,
    )
    assert "write_case_artifact" not in synthesis.tools
    assert {"list_skill_resources", "read_skill_resource", "run_ast_grep_query"} <= set(synthesis.tools)


def test_protocol_normalizes_type_and_content():
    stdout = "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "plan"}]}}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "structured_output": {
                        "target_anchor_id": "copy-site",
                        "type": "pattern",
                        "query": "copy($A)",
                        "query_weight": 2,
                        "adjustments": [],
                        "plan_suggestion": "",
                    },
                    "num_turns": 2,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
        ]
    )
    decoded = decode_claude_stream(
        ProcessResult(stdout=stdout, stderr="", returncode=0, duration_ms=9),
        output_type=AnchorSynthesisDelta,
    )
    assert [event.type for event in decoded.events] == ["thinking", "output"]
    assert decoded.usage.turns == 2


def test_protocol_keeps_malformed_terminal_json_for_model_repair():
    decoded = decode_claude_stream(
        ProcessResult(
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": '{"target_anchor_id": "copy-site"',
                    "num_turns": 1,
                }
            ),
            stderr="",
            returncode=0,
            duration_ms=2,
        ),
        output_type=AnchorSynthesisDelta,
    )

    assert decoded.output is None
    assert decoded.validation_errors == ("Claude terminal result is not valid JSON",)
    assert decoded.usage.turns == 1


def test_protocol_fails_fast_when_required_mcp_server_is_unavailable():
    init = {
        "type": "system",
        "subtype": "init",
        "model": "test-model",
        "tools": ["StructuredOutput"],
        "mcp_servers": [{"name": "vaminer", "status": "failed"}],
    }

    with pytest.raises(ClaudeCodeConfigurationError, match="vaminer.*failed"):
        decode_claude_stream(
            ProcessResult(stdout=json.dumps(init), stderr="", returncode=0, duration_ms=1),
            output_type=AnchorSynthesisDelta,
            expected_mcp_server="vaminer",
            expected_mcp_tools=("mcp__vaminer__run_ast_grep_query",),
        )


def test_protocol_fails_fast_when_required_mcp_tool_is_not_exposed():
    init = {
        "type": "system",
        "subtype": "init",
        "model": "test-model",
        "tools": ["StructuredOutput", "mcp__vaminer__read_src_file"],
        "mcp_servers": [{"name": "vaminer", "status": "connected"}],
    }

    with pytest.raises(ClaudeCodeConfigurationError, match="run_ast_grep_query"):
        decode_claude_stream(
            ProcessResult(stdout=json.dumps(init), stderr="", returncode=0, duration_ms=1),
            output_type=AnchorSynthesisDelta,
            expected_mcp_server="vaminer",
            expected_mcp_tools=(
                "mcp__vaminer__read_src_file",
                "mcp__vaminer__run_ast_grep_query",
            ),
        )


def test_protocol_does_not_promote_nonterminal_output_tool_arguments():
    stdout = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "return_anchor_synthesis_delta",
                        "id": "tool-1",
                        "input": {
                            "target_anchor_id": "copy-site",
                            "type": "pattern",
                            "query": "",
                            "query_weight": 1,
                            "adjustments": [],
                            "plan_suggestion": "",
                        },
                    }
                ]
            },
        }
    )

    with pytest.raises(ClaudeCodeProtocolError, match="without a terminal result"):
        decode_claude_stream(
            ProcessResult(stdout=stdout, stderr="", returncode=0, duration_ms=1),
            output_type=AnchorSynthesisDelta,
        )


def test_runtime_propagates_child_synthesis_failure_receipt(tmp_path: Path):
    failure = tmp_path / "synthesis-failure.json"
    failure.write_text(
        json.dumps({"type": "AnchorExecutionError", "message": "ast-grep timed out"}),
        encoding="utf-8",
    )

    with pytest.raises(ClaudeCodeChildSynthesisError, match="ast-grep timed out"):
        ClaudeCodeRuntime._raise_synthesis_failure(failure)


@pytest.mark.asyncio
async def test_mcp_records_fatal_ast_grep_tool_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace, source, cases = _workspace(tmp_path)
    failure_path = tmp_path / "tool-failure.json"
    server = build_server(
        settings=MCPServerSettings(
            profile=MCPProfile.AST_GREP_SYNTHESIS,
            workspace_root=workspace,
            source_root=source,
            cases_dir=cases,
            skill_root=Path("src/miner/skills/ast-grep").resolve(),
            tool_failure_path=failure_path,
        ),
        fast_mcp_factory=FakeServer,
    )

    def fail(*_args, **_kwargs):
        raise AstGrepRunnerError("ast-grep timed out")

    monkeypatch.setattr(mcp_module, "run_ast_grep", fail)
    with pytest.raises(AstGrepRunnerError, match="timed out"):
        await server.tools["run_ast_grep_query"]("src", "c", "pattern", "copy($A)")

    assert json.loads(failure_path.read_text(encoding="utf-8")) == {
        "type": "AstGrepRunnerError",
        "message": "ast-grep timed out",
    }


def test_protocol_bounds_stdout_without_creating_trace_events():
    stdout = StringIO()
    decoder = ClaudeStreamDecoder(
        output_type=AnchorSynthesisDelta,
        agent_name="AST-Grep Synthesizer",
        runtime_log=RuntimeLog(
            console=Console(file=stdout, width=80, color_system=None, force_terminal=False),
            width=80,
        ),
    )
    thinking = "x" * 2_000

    decoder.feed_line(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": thinking}]}}
        )
    )

    rendered = stdout.getvalue()
    assert "🧠 Thinking" in rendered
    assert "... [truncated]" in rendered


def test_cleanup_session_transcript_removes_only_owned_session(tmp_path: Path):
    config_root = tmp_path / "claude-config"
    project = config_root / "projects" / "-workspace"
    project.mkdir(parents=True)
    session_id = str(uuid.uuid4())
    other_session_id = str(uuid.uuid4())
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    session_dir = project / session_id / "tool-results"
    session_dir.mkdir(parents=True)
    (session_dir / "result.txt").write_text("result", encoding="utf-8")
    other = project / f"{other_session_id}.jsonl"
    other.write_text("{}\n", encoding="utf-8")

    removed = cleanup_session_transcript(
        session_id,
        {"CLAUDE_CONFIG_DIR": str(config_root)},
    )

    assert transcript.resolve() in removed
    assert not transcript.exists()
    assert not (project / session_id).exists()
    assert other.exists()


def test_cleanup_session_transcript_uses_command_default_below_home(tmp_path: Path):
    session_id = str(uuid.uuid4())
    transcript = tmp_path / ".cac" / "projects" / "-workspace" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")

    removed = cleanup_session_transcript(
        session_id,
        {"HOME": str(tmp_path)},
        executable="codeagent",
    )

    assert transcript.resolve() in removed
    assert not transcript.exists()


@pytest.mark.asyncio
async def test_process_runner_relays_stdout_lines_before_exit(tmp_path: Path):
    marker = tmp_path / "continue"
    program = (
        "import os, time\n"
        "print('first', flush=True)\n"
        f"marker = {str(marker)!r}\n"
        "while not os.path.exists(marker): time.sleep(0.01)\n"
        "print('second', flush=True)\n"
    )
    received: list[str] = []
    first_line = asyncio.Event()

    def handle_line(line: str) -> None:
        received.append(line)
        first_line.set()

    runner = ProcessRunner(
        max_stdout_bytes=10_000,
        max_stderr_bytes=10_000,
        terminate_grace_seconds=1,
    )
    running = asyncio.create_task(
        runner.run(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            environment=os.environ.copy(),
            prompt="",
            timeout_seconds=5,
            stdout_line_handler=handle_line,
        )
    )

    await asyncio.wait_for(first_line.wait(), timeout=1)
    assert received == ["first"]
    assert not running.done()
    marker.touch()
    result = await running
    assert received == ["first", "second"]
    assert result.stdout == "first\nsecond\n"


@pytest.mark.asyncio
async def test_process_runner_stops_on_failed_mcp_init(tmp_path: Path):
    init = {
        "type": "system",
        "subtype": "init",
        "tools": ["StructuredOutput"],
        "mcp_servers": [{"name": "vaminer", "status": "failed"}],
    }
    program = f"import time\nprint({json.dumps(json.dumps(init))}, flush=True)\ntime.sleep(30)\n"
    decoder = ClaudeStreamDecoder(
        output_type=AnchorSynthesisDelta,
        expected_mcp_server="vaminer",
        expected_mcp_tools=("mcp__vaminer__run_ast_grep_query",),
    )
    runner = ProcessRunner(
        max_stdout_bytes=10_000,
        max_stderr_bytes=10_000,
        terminate_grace_seconds=1,
    )

    with pytest.raises(ClaudeCodeConfigurationError, match="vaminer.*failed"):
        await runner.run(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            environment=os.environ.copy(),
            prompt="",
            timeout_seconds=5,
            stdout_line_handler=decoder.feed_line,
        )


@pytest.mark.asyncio
async def test_runtime_uses_ephemeral_invocation_and_returns_typed_delta(tmp_path: Path):
    workspace, source, cases = _workspace(tmp_path)
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
        root_cause=_rca(),
    )
    runtime = ClaudeCodeRuntime(ClaudeCodeConfig(executable="/bin/true", model="test-model"))

    class Runner:
        async def run(self, *_args, **_kwargs):
            payload = {
                "target_anchor_id": "copy-site",
                "type": "pattern",
                "query": "",
                "query_weight": 2,
                "adjustments": ["disabled"],
                "plan_suggestion": "",
            }
            return ProcessResult(
                stdout=json.dumps({"type": "result", "subtype": "success", "structured_output": payload}),
                stderr="",
                returncode=0,
                duration_ms=1,
            )

    runtime._runner = Runner()
    result = await runtime.run(task)
    assert result.output.target_anchor_id == "copy-site"
    assert result.identity.runtime_id == "claude-cli"
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_runtime_repairs_output_with_remaining_turn_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace, source, cases = _workspace(tmp_path)
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
        root_cause=_rca(),
    )
    runtime = ClaudeCodeRuntime(
        ClaudeCodeConfig(
            executable="/bin/true",
            model="test-model",
            display_name="Internal Agent",
        )
    )

    class Runner:
        def __init__(self) -> None:
            self.calls = 0
            self.max_turns: list[int] = []
            self.prompts: list[str] = []
            self.session_ids: list[str] = []
            self.session_flags: list[str] = []
            self.mcp_paths: list[str] = []

        async def run(self, argv, **kwargs):
            self.calls += 1
            self.max_turns.append(int(argv[argv.index("--max-turns") + 1]))
            self.prompts.append(kwargs["prompt"])
            session_flag = "--resume" if "--resume" in argv else "--session-id"
            self.session_flags.append(session_flag)
            self.session_ids.append(argv[argv.index(session_flag) + 1])
            self.mcp_paths.append(argv[argv.index("--mcp-config") + 1])
            payload = {
                "target_anchor_id": "wrong-site" if self.calls == 1 else "copy-site",
                "type": "pattern",
                "query": "",
                "query_weight": 2,
                "adjustments": [],
                "plan_suggestion": "",
            }
            return ProcessResult(
                stdout=json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "structured_output": payload,
                        "num_turns": 1,
                    }
                ),
                stderr="",
                returncode=0,
                duration_ms=1,
            )

    runner = Runner()
    runtime._runner = runner
    traced_sessions: list[tuple[str, dict[str, object]]] = []

    async def emit_trace(session_id: str, **kwargs):
        traced_sessions.append((session_id, kwargs))

    monkeypatch.setattr("src.miner.runtimes.claude.runtime.emit_session_trace", emit_trace)
    result = await runtime.run(task)
    assert result.attempts == 2
    assert runner.max_turns == [task.limits.request_limit, task.limits.request_limit - 1]
    assert runner.session_flags == ["--session-id", "--resume"]
    assert runner.session_ids[0] == runner.session_ids[1]
    assert [session_id for session_id, _kwargs in traced_sessions] == runner.session_ids
    assert all(kwargs["display_name"] == "Internal Agent" for _session_id, kwargs in traced_sessions)
    assert runner.mcp_paths[0] == runner.mcp_paths[1]
    assert task.prompt == runner.prompts[0]
    assert task.prompt not in runner.prompts[1]
    assert "do not repeat research" in runner.prompts[1]
    assert "Previous candidate" not in runner.prompts[1]


@pytest.mark.asyncio
async def test_runtime_does_not_retry_broken_stream_protocol(tmp_path: Path):
    workspace, source, cases = _workspace(tmp_path)
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
        root_cause=_rca(),
    )
    runtime = ClaudeCodeRuntime(ClaudeCodeConfig(executable="/bin/true", model="test-model"))

    class Runner:
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return ProcessResult(
                stdout="not-json",
                stderr="",
                returncode=0,
                duration_ms=1,
            )

    runner = Runner()
    runtime._runner = runner
    with pytest.raises(ClaudeCodeProtocolError, match="is not JSON"):
        await runtime.run(task)
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_runtime_propagates_fatal_tool_failure_before_accepting_empty_query(tmp_path: Path):
    workspace, source, cases = _workspace(tmp_path)
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
        root_cause=_rca(),
    )
    runtime = ClaudeCodeRuntime(ClaudeCodeConfig(executable="/bin/true", model="test-model"))

    class Runner:
        async def run(self, argv, **_kwargs):
            mcp_path = Path(argv[argv.index("--mcp-config") + 1])
            mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
            failure_path = Path(mcp_config["mcpServers"]["vaminer"]["env"][TOOL_FAILURE_ENV])
            failure_path.write_text(
                json.dumps({"type": "AstGrepRunnerError", "message": "binary missing"}),
                encoding="utf-8",
            )
            payload = {
                "target_anchor_id": "copy-site",
                "type": "pattern",
                "query": "",
                "query_weight": 1,
                "adjustments": ["degraded"],
                "plan_suggestion": "",
            }
            return ProcessResult(
                stdout=json.dumps(
                    {"type": "result", "subtype": "success", "structured_output": payload}
                ),
                stderr="",
                returncode=0,
                duration_ms=1,
            )

    runtime._runner = Runner()
    with pytest.raises(ClaudeCodeToolExecutionError, match="binary missing"):
        await runtime.run(task)
