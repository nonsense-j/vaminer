"""Deterministic subprocess tests for the Claude Code runtime adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import stat
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.errors import (
    ClaudeCodeConfigurationError,
    ClaudeCodeOutputLimitError,
    ClaudeCodeProtocolError,
    ClaudeCodeProviderError,
    ClaudeCodeRequestLimitError,
    ClaudeCodeTimeoutError,
    ClaudeCodeValidationError,
)
from src.miner.runtimes.claude.runtime import ClaudeCodeRuntime
from src.miner.utils.log import logger
from src.miner.agent.contracts import (
    AgentPhase,
    AgentTask,
    FileAccess,
    RunLimits,
    RuntimeCapability,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.runtimes.claude.mcp import (
    PROFILE_ENV,
)
from src.miner.models import VASCoreInfo
from tests.support.factories import (
    SOURCE,
    analysis_subject,
    root_cause as _root_cause,
    vas_core as _core,
)


class ProbeOutput(BaseModel):
    value: int


def _write_fake_claude(path: Path) -> Path:
    path.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env python3
            from __future__ import annotations

            import json
            import os
            from pathlib import Path
            import subprocess
            import sys
            import time


            def option(name: str) -> str | None:
                try:
                    return sys.argv[sys.argv.index(name) + 1]
                except (ValueError, IndexError):
                    return None


            prompt = sys.stdin.read()
            record_path = Path(os.environ["FAKE_CLAUDE_RECORD"])
            records = json.loads(record_path.read_text()) if record_path.exists() else []
            system_prompt_path = option("--system-prompt-file")
            plugin_path = option("--plugin-dir")
            mcp_path = option("--mcp-config")
            settings_path = option("--settings")
            plugin_agents = {}
            if plugin_path:
                for item in (Path(plugin_path) / "agents").glob("*.md"):
                    plugin_agents[item.name] = item.read_text()
            mcp = json.loads(Path(mcp_path).read_text()) if mcp_path else None
            record = {
                "argv": sys.argv[1:],
                "prompt": prompt,
                "system_prompt": Path(system_prompt_path).read_text() if system_prompt_path else None,
                "plugin_files": sorted(
                    item.relative_to(plugin_path).as_posix()
                    for item in Path(plugin_path).rglob("*")
                    if item.is_file()
                ) if plugin_path else [],
                "plugin_agents": plugin_agents,
                "mcp": mcp,
                "settings": json.loads(Path(settings_path).read_text()) if settings_path else None,
                "isolation_env": {
                    key: os.environ.get(key)
                    for key in (
                        "CLAUDE_CONFIG_DIR",
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                        "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
                        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
                        "ENABLE_CLAUDEAI_MCP_SERVERS",
                        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
                        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
                    )
                },
            }
            records.append(record)
            record_path.write_text(json.dumps(records))

            scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "json_success")
            attempt = len(records)
            output = {"value": 7}
            if scenario == "rule_success":
                output = json.loads(os.environ["FAKE_CLAUDE_CORE"])
            terminal = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": f"session-{attempt}",
                "num_turns": 1,
                "duration_ms": 12,
                "total_cost_usd": 0.0123,
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                },
                "modelUsage": {
                    "fake-model": {
                        "inputTokens": 11,
                        "outputTokens": 5,
                        "cacheCreationInputTokens": 2,
                        "cacheReadInputTokens": 3,
                        "costUSD": 0.0123,
                    }
                },
                "permission_denials": [],
                "terminal_reason": "end_turn",
                "result": json.dumps(output),
                "structured_output": output,
            }
            if scenario == "model_drift":
                terminal["modelUsage"] = {
                    "fake-model": {"inputTokens": 6},
                    "other-model": {"inputTokens": 5},
                }

            if scenario in {"stream_success", "rule_success"}:
                print(json.dumps({
                    "type": "system",
                    "subtype": "init",
                    "plugin_errors": [],
                    "mcp_servers": [],
                }))
                print(json.dumps({
                    "type": "assistant",
                    "message": {
                        "id": f"request-{attempt}",
                        "content": (
                            [{
                                "type": "tool_use",
                                "id": "agent-1",
                                "name": "Agent",
                                "input": {"subagent_type": "vaminer:ast-grep-synthesizer"},
                            }]
                            if scenario.startswith("rule_")
                            else []
                        ),
                    },
                }))
                print(json.dumps(terminal))
            elif scenario == "live_stream":
                print(json.dumps({
                    "type": "system",
                    "subtype": "init",
                    "plugin_errors": [],
                    "mcp_servers": [],
                }), flush=True)
                time.sleep(0.5)
                print(json.dumps(terminal), flush=True)
            elif scenario == "request_limit":
                for request_no in range(1, 4):
                    print(json.dumps({
                        "type": "assistant",
                        "message": {"id": f"request-{request_no}", "content": []},
                    }), flush=True)
                    time.sleep(0.05)
                time.sleep(30)
            elif scenario == "repair":
                if attempt == 1:
                    terminal["structured_output"] = {"value": "not-an-integer"}
                    terminal["result"] = json.dumps(terminal["structured_output"])
                else:
                    terminal["structured_output"] = {"value": 9}
                    terminal["result"] = json.dumps(terminal["structured_output"])
                print(json.dumps(terminal))
            elif scenario == "always_invalid":
                terminal["structured_output"] = {"value": "invalid"}
                print(json.dumps(terminal))
            elif scenario == "prose_fenced_json":
                terminal["structured_output"] = None
                terminal["result"] = 'Completed.\\n```json\\n{"value": 7}\\n```'
                print(json.dumps(terminal))
            elif scenario == "provider_error":
                terminal.update({
                    "is_error": True,
                    "terminal_reason": "api_error",
                    "api_error_status": 429,
                    "result": "Rate limit exceeded by upstream provider",
                    "structured_output": None,
                })
                print(json.dumps(terminal))
                raise SystemExit(1)
            elif scenario in {"budget_exhausted", "budget_with_output"}:
                terminal.update({
                    "subtype": "error_max_budget_usd",
                    "is_error": True,
                    "terminal_reason": "budget_exhausted",
                    "total_cost_usd": 1.01,
                    "result": json.dumps(output) if scenario == "budget_with_output" else None,
                    "structured_output": output if scenario == "budget_with_output" else None,
                })
                print(json.dumps(terminal))
            elif scenario == "max_turns_with_output":
                terminal.update({
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "terminal_reason": "max_turns",
                })
                print(json.dumps(terminal))
            elif scenario == "structured_retry_with_output":
                terminal.update({
                    "subtype": "error_max_structured_output_retries",
                    "is_error": True,
                    "terminal_reason": "structured_output_retry_exhausted",
                })
                print(json.dumps(terminal))
            elif scenario == "timeout":
                marker = os.environ["FAKE_CLAUDE_CHILD_MARKER"]
                child_code = (
                    "import pathlib,time; time.sleep(0.6); "
                    f"pathlib.Path({marker!r}).write_text('orphaned')"
                )
                subprocess.Popen([sys.executable, "-c", child_code])
                time.sleep(30)
            elif scenario == "large_stdout":
                sys.stdout.write("x" * 100_000)
                sys.stdout.flush()
                time.sleep(30)
            else:
                print(json.dumps(terminal))
            """),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _make_task(
    tmp_path: Path,
    *,
    phase: AgentPhase = AgentPhase.ISSUE_COLLECTION,
    fixed_diff: bool = False,
    limits: RunLimits | None = None,
) -> AgentTask[ProbeOutput]:
    source_root = tmp_path / "source"
    cases_dir = tmp_path / "cases"
    source_root.mkdir(exist_ok=True)
    cases_dir.mkdir(exist_ok=True)
    context = TaskContext(
        workspace_root=tmp_path,
        output_root=tmp_path / "output",
        input_id="CVE-2099-0001",
        trace_id="0123456789abcdef0123456789abcdef",
        source_root=source_root if phase is AgentPhase.ROOT_CAUSE else None,
        repo_path=source_root if phase is AgentPhase.ROOT_CAUSE and fixed_diff else None,
        cases_dir=cases_dir if phase is AgentPhase.ROOT_CAUSE else None,
    )
    capabilities = {RuntimeCapability.STRUCTURED_OUTPUT}
    if fixed_diff:
        capabilities.add(RuntimeCapability.FIXED_DIFF)
    return AgentTask(
        task_id="probe-task",
        phase=phase,
        agent_name="Probe",
        description="Probe the Claude adapter.",
        instructions="Treat source files as evidence and return a typed probe.",
        prompt="Produce the probe object.",
        output_type=ProbeOutput,
        context=context,
        workspace=WorkspacePolicy(
            cwd=tmp_path,
            native_workspace_access=(FileAccess.READ_WRITE if phase is AgentPhase.ROOT_CAUSE else FileAccess.NONE),
            allow_network=phase is AgentPhase.ISSUE_COLLECTION,
        ),
        required_capabilities=frozenset(capabilities),
        limits=limits or RunLimits(request_limit=8, output_retries=2),
    )


def _make_rule_task(tmp_path: Path) -> tuple[AgentTask[VASCoreInfo], VASCoreInfo]:
    source_root = tmp_path / "source"
    cases_dir = tmp_path / "cases"
    source_root.mkdir(exist_ok=True)
    cases_dir.mkdir(exist_ok=True)
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
    root_cause = _root_cause()
    subject = analysis_subject(source_root, cases_dir)
    return (
        make_rule_generation_task(
            root_cause,
            workspace_root=tmp_path,
            source_root=source_root,
            repo_path=source_root,
            cases_dir=cases_dir,
            analysis_subject=subject,
        ),
        _core(root_cause),
    )


def _make_config(
    tmp_path: Path,
    fake_claude: Path,
    *,
    scenario: str,
    output_format: str = "json",
    **overrides,
) -> ClaudeCodeConfig:
    values = {
        "executable": fake_claude,
        "model": "fake-model",
        "output_format": output_format,
        "artifact_root": tmp_path / "artifacts",
        "environment": {
            "FAKE_CLAUDE_RECORD": str(tmp_path / "calls.json"),
            "FAKE_CLAUDE_SCENARIO": scenario,
        },
    }
    values.update(overrides)
    return ClaudeCodeConfig(**values)


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_user_auth_model_selection_and_usage_audit(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    task = _make_task(tmp_path)
    result = await ClaudeCodeRuntime(
        _make_config(
            tmp_path,
            fake,
            scenario="stream_success",
            output_format="stream-json",
        )
    ).run(task)

    assert result.output == ProbeOutput(value=7)
    assert result.usage is not None
    assert result.usage.requests == 1
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 5
    assert result.usage.cache_creation_input_tokens == 2
    assert result.usage.cache_read_input_tokens == 3
    assert "costUSD" not in result.usage.model_usage["fake-model"]
    assert result.artifacts.prompt_path is None
    assert result.artifacts.stdout_path is None
    assert result.artifacts.stderr_path is None
    assert result.artifacts.events_path is None
    assert not list((tmp_path / "artifacts").glob("**/attempt-*.stdout.*"))
    assert not list((tmp_path / "artifacts").glob("**/attempt-*.prompt.md"))
    assert not list((tmp_path / "artifacts").glob("**/attempt-*.stderr.txt"))
    invocation = json.loads(result.artifacts.invocation_path.read_text(encoding="utf-8"))
    assert result.artifacts.invocation_path == (
        tmp_path
        / "artifacts"
        / "claude-code"
        / "CVE-2099-0001"
        / "0123456789abcdef0123456789abcdef"
        / "issue-collection"
        / "invocation.json"
    )
    artifact_files = list(result.artifacts.invocation_path.parent.iterdir())
    assert artifact_files == [result.artifacts.invocation_path]
    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    argv = call["argv"]
    assert "--bare" not in argv
    assert _option(argv, "--setting-sources") == "user"
    assert _option(argv, "--model") == "fake-model"
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert "--max-budget-usd" not in argv
    assert invocation["isolation"]["setting_sources"] == ["user"]
    assert invocation["input_id"] == "CVE-2099-0001"
    assert invocation["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert invocation["isolation"]["strict_mcp_config"] is True
    assert invocation["isolation"]["session_persistence"] is False
    assert invocation["isolation"]["auto_memory"] is False
    assert invocation["isolation"]["claude_ai_mcp_servers"] is False
    assert invocation["isolation"]["subagent_depth"] == 1
    assert result.metadata["model_usage"]["fake-model"]["inputTokens"] == 11
    assert call["isolation_env"] == {
        "CLAUDE_CONFIG_DIR": None,
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": None,
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "2",
    }


def test_claude_runtime_requires_an_explicit_model(tmp_path: Path):
    runtime = ClaudeCodeRuntime(ClaudeCodeConfig(model=None))

    with pytest.raises(ClaudeCodeConfigurationError, match="Claude model is required"):
        runtime.model_id_for(_make_task(tmp_path))


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_rule_generation_registers_restricted_synthesizer(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    task, core = _make_rule_task(tmp_path)
    environment = {
        "FAKE_CLAUDE_RECORD": str(tmp_path / "calls.json"),
        "FAKE_CLAUDE_SCENARIO": "rule_success",
        "FAKE_CLAUDE_CORE": core.model_dump_json(by_alias=True),
    }
    result = await ClaudeCodeRuntime(
        _make_config(
            tmp_path,
            fake,
            scenario="rule_success",
            output_format="stream-json",
            environment=environment,
        )
    ).run(task)

    assert result.output == core
    assert result.metadata["subagent_events"][0]["agent_type"] == ("vaminer:ast-grep-synthesizer")
    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    argv = call["argv"]
    assert _option(argv, "--agent") == "vaminer:rule-generator"
    assert "--forward-subagent-text" in argv
    assert _option(argv, "--tools") == "Read,Grep,Glob,Agent,Bash"
    allowed = set(_option(argv, "--allowedTools").split(","))
    assert "Agent(vaminer:ast-grep-synthesizer)" in allowed
    assert "Agent" not in allowed
    assert any(tool.startswith("Bash(") and "ast-grep/scripts/runner.py" in tool for tool in allowed)
    assert "Bash" not in call["settings"]["permissions"]["deny"]
    assert {"Write", "Edit"} <= set(call["settings"]["permissions"]["deny"])
    assert "Task" not in call["settings"]["permissions"]["deny"]
    assert not any(item.startswith("Read(") for item in call["settings"]["permissions"]["deny"])
    guard = call["settings"]["hooks"]["PreToolUse"][0]
    assert guard["matcher"] == "Read|Grep|Glob|Write|Bash"
    guard_args = guard["hooks"][0]["args"]
    assert guard_args[guard_args.index("--root") + 1] == str(tmp_path)
    assert "--write-root" not in guard_args
    assert {
        ".claude-plugin/plugin.json",
        "agents/rule-generator.md",
        "agents/ast-grep-synthesizer.md",
        "skills/ast-grep/SKILL.md",
    } <= set(call["plugin_files"])
    parent = call["plugin_agents"]["rule-generator.md"]
    assert "tools: Read, Grep, Glob, Agent" in parent
    child = call["plugin_agents"]["ast-grep-synthesizer.md"]
    assert "tools: Read, Grep, Glob, Bash" in child
    assert "Agent" not in child.split("---", 2)[1]
    assert call["mcp"]["mcpServers"] == {}
    schema = json.loads(_option(argv, "--json-schema"))
    rendered_schema = json.dumps(schema)
    assert "$defs" not in rendered_schema
    assert "$ref" not in rendered_schema
    invocation = json.loads(result.artifacts.invocation_path.read_text(encoding="utf-8"))
    assert invocation["tools"]["registered"] == ["Read", "Grep", "Glob", "Agent", "Bash"]


async def test_root_cause_uses_native_workspace_tools(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    await ClaudeCodeRuntime(_make_config(tmp_path, fake, scenario="json_success")).run(
        _make_task(tmp_path, phase=AgentPhase.ROOT_CAUSE)
    )

    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    assert _option(call["argv"], "--tools") == "Read,Grep,Glob,Write"
    allowed = set(_option(call["argv"], "--allowedTools").split(","))
    assert allowed == {"Read", "Grep", "Glob", "Write"}
    assert all("Bash" not in tool for tool in allowed)
    assert "Write" not in call["settings"]["permissions"]["deny"]
    assert "Edit" in call["settings"]["permissions"]["deny"]
    assert "Bash" in call["settings"]["permissions"]["deny"]
    guard = call["settings"]["hooks"]["PreToolUse"][0]
    assert guard["matcher"] == "Read|Grep|Glob|Write|Bash"
    guard_args = guard["hooks"][0]["args"]
    assert guard_args[guard_args.index("--root") + 1] == str(tmp_path)
    write_root_index = guard_args.index("--write-root")
    assert guard_args[write_root_index + 1] == str(tmp_path / "cases")
    assert call["mcp"]["mcpServers"] == {}


async def test_root_cause_adds_only_conditional_fixed_diff_mcp(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    await ClaudeCodeRuntime(_make_config(tmp_path, fake, scenario="json_success")).run(
        _make_task(tmp_path, phase=AgentPhase.ROOT_CAUSE, fixed_diff=True)
    )

    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    allowed = set(_option(call["argv"], "--allowedTools").split(","))
    assert allowed == {"Read", "Grep", "Glob", "Write", "mcp__vaminer__read_patch_diff"}
    mcp_env = call["mcp"]["mcpServers"]["vaminer"]["env"]
    assert mcp_env[PROFILE_ENV] == "root_cause"
    assert mcp_env["VAMINER_MCP_FIXED_DIFF_ENABLED"] == "true"
    assert mcp_env["VAMINER_MCP_REPO_PATH"] == str(tmp_path / "source")


async def test_repair_attempts_are_bounded_and_usage_is_aggregated(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    fake = _write_fake_claude(tmp_path / "claude")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = await ClaudeCodeRuntime(_make_config(tmp_path, fake, scenario="repair")).run(
                _make_task(tmp_path, limits=RunLimits(request_limit=8, output_retries=5))
            )
    finally:
        logger.removeHandler(caplog.handler)

    assert result.output == ProbeOutput(value=9)
    assert result.attempts == 2
    assert result.usage is not None
    assert result.usage.turns == 2
    assert result.usage.input_tokens == 22
    assert result.usage.output_tokens == 10
    assert "costUSD" not in result.usage.model_usage["fake-model"]
    records = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    assert len(records) == 2
    assert _option(records[0]["argv"], "--max-turns") == "8"
    assert _option(records[1]["argv"], "--max-turns") == "7"
    validation_paths = list((tmp_path / "artifacts").glob("**/attempt-1.validation-errors.txt"))
    assert len(validation_paths) == 1
    assert validation_paths[0].stat().st_size > 0
    assert not list((tmp_path / "artifacts").glob("**/attempt-2.validation-errors.txt"))
    assert result.metadata["attempt_artifacts"][0]["validation_errors"] == str(validation_paths[0])
    assert result.metadata["attempt_artifacts"][1]["validation_errors"] is None
    assert "Claude Code output validation failed: task=probe-task attempt=1/3" in caplog.text
    assert f"validation_errors={validation_paths[0]}" in caplog.text

    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    invalid_fake = _write_fake_claude(invalid_dir / "claude")
    with pytest.raises(ClaudeCodeValidationError) as exc_info:
        await ClaudeCodeRuntime(_make_config(invalid_dir, invalid_fake, scenario="always_invalid")).run(
            _make_task(invalid_dir, limits=RunLimits(request_limit=8, output_retries=10))
        )
    assert exc_info.value.attempts == 3


async def test_fenced_json_is_still_subject_to_typed_validation(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    result = await ClaudeCodeRuntime(_make_config(tmp_path, fake, scenario="prose_fenced_json")).run(
        _make_task(tmp_path)
    )

    assert result.output == ProbeOutput(value=7)


async def test_request_limit_is_enforced_across_streamed_responses(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    runtime = ClaudeCodeRuntime(
        _make_config(
            tmp_path,
            fake,
            scenario="request_limit",
            output_format="stream-json",
            terminate_grace_seconds=0.05,
        )
    )
    with pytest.raises(ClaudeCodeRequestLimitError) as exc_info:
        await runtime.run(
            _make_task(
                tmp_path,
                limits=RunLimits(request_limit=1, timeout_seconds=2, output_retries=2),
            )
        )
    assert exc_info.value.observed == 2


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        ("budget_with_output", ClaudeCodeProviderError),
        ("max_turns_with_output", ClaudeCodeRequestLimitError),
        ("structured_retry_with_output", ClaudeCodeProtocolError),
    ],
)
async def test_terminal_exhaustion_is_authoritative_even_with_structured_output(
    tmp_path: Path,
    scenario: str,
    error: type[Exception],
):
    fake = _write_fake_claude(tmp_path / "claude")
    config = _make_config(tmp_path, fake, scenario=scenario)
    with pytest.raises(error) as exc_info:
        await ClaudeCodeRuntime(config).run(_make_task(tmp_path))
    if scenario.startswith("budget"):
        assert exc_info.value.category == "budget"


async def test_provider_errors_are_classified_without_repair(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    fake = _write_fake_claude(tmp_path / "claude")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            with pytest.raises(ClaudeCodeProviderError) as exc_info:
                await ClaudeCodeRuntime(
                    _make_config(tmp_path, fake, scenario="provider_error")
                ).run(_make_task(tmp_path))
    finally:
        logger.removeHandler(caplog.handler)
    assert exc_info.value.category == "rate_limit"
    assert exc_info.value.status_code == 429
    assert len(json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))) == 1
    assert (
        "Claude terminal output: task=probe-task attempt=1 turns=1 reason=api_error "
        "structured=False output=Rate limit exceeded by upstream provider"
    ) in caplog.text
    assert not list((tmp_path / "artifacts").glob("**/attempt-*.stdout.*"))
    assert not list((tmp_path / "artifacts").glob("**/attempt-*.stderr.txt"))


async def test_parent_subagent_model_drift_is_rejected(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    with pytest.raises(ClaudeCodeConfigurationError, match="model identity drifted"):
        await ClaudeCodeRuntime(_make_config(tmp_path, fake, scenario="model_drift")).run(_make_task(tmp_path))


async def test_stream_output_is_logged_without_raw_artifact(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    fake = _write_fake_claude(tmp_path / "claude")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            result = await ClaudeCodeRuntime(
                _make_config(
                    tmp_path,
                    fake,
                    scenario="live_stream",
                    output_format="stream-json",
                )
            ).run(_make_task(tmp_path))
    finally:
        logger.removeHandler(caplog.handler)

    assert result.output == ProbeOutput(value=7)
    assert not list((tmp_path / "artifacts").glob("**/attempt-*.stdout.*"))
    assert (
        "Claude terminal output: task=probe-task attempt=1 turns=1 reason=end_turn "
        'structured=True output={"value":7}'
    ) in caplog.text


async def test_timeout_terminates_the_complete_process_group(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    marker = tmp_path / "orphan-marker"
    config = _make_config(
        tmp_path,
        fake,
        scenario="timeout",
        terminate_grace_seconds=0.05,
        environment={
            "FAKE_CLAUDE_RECORD": str(tmp_path / "calls.json"),
            "FAKE_CLAUDE_SCENARIO": "timeout",
            "FAKE_CLAUDE_CHILD_MARKER": str(marker),
        },
    )
    with pytest.raises(ClaudeCodeTimeoutError):
        await ClaudeCodeRuntime(config).run(
            _make_task(
                tmp_path,
                limits=RunLimits(request_limit=8, timeout_seconds=0.15, output_retries=0),
            )
        )
    await asyncio.sleep(0.8)
    assert not marker.exists()


async def test_stdout_limit_terminates_noisy_process(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    with pytest.raises(ClaudeCodeOutputLimitError) as exc_info:
        await ClaudeCodeRuntime(
            _make_config(
                tmp_path,
                fake,
                scenario="large_stdout",
                max_stdout_bytes=1024,
                terminate_grace_seconds=0.05,
            )
        ).run(
            _make_task(
                tmp_path,
                limits=RunLimits(request_limit=8, timeout_seconds=2, output_retries=0),
            )
        )
    assert exc_info.value.stream_name == "stdout"
    assert exc_info.value.limit_bytes == 1024
