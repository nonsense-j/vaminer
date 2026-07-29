"""Deterministic subprocess tests for the Claude Code runtime adapter."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.miner.core.tasks import make_rule_generation_task
from src.miner.runtime.claude_code import (
    ClaudeCodeBudgetError,
    ClaudeCodeConfig,
    ClaudeCodeConfigurationError,
    ClaudeCodeOutputLimitError,
    ClaudeCodeProtocolError,
    ClaudeCodeProviderError,
    ClaudeCodeRequestLimitError,
    ClaudeCodeRuntime,
    ClaudeCodeTimeoutError,
    ClaudeCodeValidationError,
)
from src.miner.runtime.contracts import (
    AgentPhase,
    AgentTask,
    FileAccess,
    RunLimits,
    RuntimeCapability,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.runtime.mcp_server import (
    BATCH_RESULT_ENV,
    CASES_DIR_ENV,
    FIXED_DIFF_ENV,
    PROFILE_ENV,
    REPO_PATH_ENV,
    SOURCE_ROOT_ENV,
)
from src.miner.utils.models import AnalysisSubject, RootCauseAnalysis, VASCoreInfo

SOURCE = "void trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


class ProbeOutput(BaseModel):
    value: int


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
            "fixing_pattern": "Establish the guard first.",
            "extracted_case_files": ["case1.c"],
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
                "unsafe": ["The operation executes before its required guard."],
                "safe": ["The required guard is established before the operation."],
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


def _synthesis(core: VASCoreInfo) -> dict:
    return {
        "anchors": [core.anchors[0].model_dump(mode="json", by_alias=True)],
        "case_coverage": [{"path": "case1.c", "anchor_ids": ["danger-call"]}],
        "repo_evidence": [{"anchor_id": "danger-call", "file": "bug.c", "line": 1}],
        "adjustments": [],
    }


def _write_fake_claude(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """\
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
            if scenario in {"rule_success", "rule_missing_finalizer"}:
                output = json.loads(os.environ["FAKE_CLAUDE_CORE"])
                if scenario == "rule_success":
                    server_env = mcp["mcpServers"]["vaminer"]["env"]
                    Path(server_env["VAMINER_MCP_BATCH_RESULT"]).write_text(
                        os.environ["FAKE_CLAUDE_SYNTHESIS"]
                    )
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

            if scenario in {"stream_success", "rule_success", "rule_missing_finalizer"}:
                print(json.dumps({
                    "type": "system",
                    "subtype": "init",
                    "plugin_errors": [],
                    "mcp_servers": [{"name": "vaminer", "status": "pending"}],
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
            """
        ),
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
            repository=(
                FileAccess.READ_ONLY
                if phase is AgentPhase.ROOT_CAUSE
                else FileAccess.READ_WRITE
            ),
            cases=FileAccess.READ_WRITE if phase is AgentPhase.ROOT_CAUSE else FileAccess.NONE,
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
    subject = AnalysisSubject(
        type="issue",
        source_root=source_root.resolve().as_posix(),
        cases_dir=cases_dir.resolve().as_posix(),
        grounding_policy="repo_evidence",
    )
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
        "native_delegation_tool": "Agent",
        "environment": {
            "FAKE_CLAUDE_RECORD": str(tmp_path / "calls.json"),
            "FAKE_CLAUDE_SCENARIO": scenario,
        },
    }
    values.update(overrides)
    return ClaudeCodeConfig(**values)


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_oauth_compatible_default_isolation_and_usage_audit(tmp_path: Path):
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
    assert result.usage.cost_usd == Decimal("0.0123")
    invocation = json.loads(result.artifacts.invocation_path.read_text(encoding="utf-8"))
    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    argv = call["argv"]
    assert "--bare" not in argv
    assert _option(argv, "--setting-sources") == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert invocation["isolation"]["bare"] is False
    assert invocation["isolation"]["setting_sources"] == []
    assert invocation["isolation"]["strict_mcp_config"] is True
    assert invocation["isolation"]["session_persistence"] is False
    assert invocation["isolation"]["auto_memory"] is False
    assert invocation["isolation"]["claude_ai_mcp_servers"] is False
    assert invocation["isolation"]["subprocess_environment_scrub"] is True
    assert invocation["isolation"]["credential_store"] == "preserved"
    assert invocation["isolation"]["subagent_depth"] == 1
    assert invocation["isolation"]["subagent_spawn_depth_env"] == 2
    assert result.metadata["model_usage"]["fake-model"]["inputTokens"] == 11
    assert call["isolation_env"] == {
        "CLAUDE_CONFIG_DIR": None,
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "2",
    }


async def test_bare_mode_is_explicit_and_safe_auth_helper_settings_are_filtered(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": "/usr/local/bin/auth-helper",
                "forceLoginMethod": "console",
                "hooks": {"PreToolUse": []},
                "enabledPlugins": {"untrusted": True},
            }
        ),
        encoding="utf-8",
    )
    await ClaudeCodeRuntime(
        _make_config(
            tmp_path,
            fake,
            scenario="json_success",
            bare=True,
            settings_file=settings,
        )
    ).run(_make_task(tmp_path))

    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    assert "--bare" in call["argv"]
    assert call["settings"]["apiKeyHelper"] == "/usr/local/bin/auth-helper"
    assert call["settings"]["forceLoginMethod"] == "console"
    assert "hooks" not in call["settings"]
    assert "enabledPlugins" not in call["settings"]


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_rule_generation_materializes_exact_plugin_and_requires_finalizer(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    task, core = _make_rule_task(tmp_path)
    environment = {
        "FAKE_CLAUDE_RECORD": str(tmp_path / "calls.json"),
        "FAKE_CLAUDE_SCENARIO": "rule_success",
        "FAKE_CLAUDE_CORE": core.model_dump_json(by_alias=True),
        "FAKE_CLAUDE_SYNTHESIS": json.dumps(_synthesis(core)),
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
    assert result.metadata["subagent_events"][0]["agent_type"] == (
        "vaminer:ast-grep-synthesizer"
    )
    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    argv = call["argv"]
    assert _option(argv, "--agent") == "vaminer:rule-generator"
    assert "--forward-subagent-text" in argv
    assert _option(argv, "--tools") == "Agent"
    allowed = set(_option(argv, "--allowedTools").split(","))
    assert "Agent(vaminer:ast-grep-synthesizer)" in allowed
    assert "Agent" not in allowed
    assert "Bash" in call["settings"]["permissions"]["deny"]
    assert {"Write", "Edit", "Task"} <= set(call["settings"]["permissions"]["deny"])
    assert {
        ".claude-plugin/plugin.json",
        "agents/rule-generator.md",
        "agents/ast-grep-synthesizer.md",
        "skills/ast-grep/SKILL.md",
    } <= set(call["plugin_files"])
    assert "tools: Agent," in call["plugin_agents"]["rule-generator.md"]
    child = call["plugin_agents"]["ast-grep-synthesizer.md"]
    assert "Bash" not in child
    assert "Agent" not in child.split("---", 2)[1]
    mcp_env = call["mcp"]["mcpServers"]["vaminer"]["env"]
    assert mcp_env[PROFILE_ENV] == "rule_generation"
    assert mcp_env[SOURCE_ROOT_ENV] == str(tmp_path / "source")
    assert mcp_env[CASES_DIR_ENV] == str(tmp_path / "cases")
    assert BATCH_RESULT_ENV in mcp_env
    invocation = json.loads(result.artifacts.invocation_path.read_text(encoding="utf-8"))
    assert invocation["tool_audit"]["main_mcp_tools"] == [
        "list_case_artifacts",
        "read_case_artifact",
        "finalize_anchor_synthesis_batch",
    ]

    missing_dir = tmp_path / "missing-finalizer"
    missing_dir.mkdir()
    missing_fake = _write_fake_claude(missing_dir / "claude")
    missing_task, missing_core = _make_rule_task(missing_dir)
    with pytest.raises(ClaudeCodeValidationError, match="did not finalize"):
        await ClaudeCodeRuntime(
            _make_config(
                missing_dir,
                missing_fake,
                scenario="rule_missing_finalizer",
                output_format="stream-json",
                max_repair_attempts=0,
                environment={
                    "FAKE_CLAUDE_RECORD": str(missing_dir / "calls.json"),
                    "FAKE_CLAUDE_SCENARIO": "rule_missing_finalizer",
                    "FAKE_CLAUDE_CORE": missing_core.model_dump_json(by_alias=True),
                    "FAKE_CLAUDE_SYNTHESIS": json.dumps(_synthesis(missing_core)),
                },
            )
        ).run(missing_task)


async def test_root_cause_uses_only_source_and_case_mcp_tools(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    await ClaudeCodeRuntime(
        _make_config(tmp_path, fake, scenario="json_success")
    ).run(_make_task(tmp_path, phase=AgentPhase.ROOT_CAUSE))

    call = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))[0]
    assert _option(call["argv"], "--tools") == ""
    allowed = set(_option(call["argv"], "--allowedTools").split(","))
    assert {
        "mcp__vaminer__read_source_file",
        "mcp__vaminer__search_source_files",
        "mcp__vaminer__write_case_artifact",
    } <= allowed
    assert all("Bash" not in tool for tool in allowed)
    mcp_env = call["mcp"]["mcpServers"]["vaminer"]["env"]
    assert mcp_env[PROFILE_ENV] == "root_cause"
    assert mcp_env[SOURCE_ROOT_ENV] == str(tmp_path / "source")
    assert mcp_env[FIXED_DIFF_ENV] == "false"
    assert REPO_PATH_ENV not in mcp_env


async def test_repair_attempts_are_bounded_and_usage_is_aggregated(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    result = await ClaudeCodeRuntime(
        _make_config(tmp_path, fake, scenario="repair")
    ).run(_make_task(tmp_path, limits=RunLimits(request_limit=8, output_retries=5)))

    assert result.output == ProbeOutput(value=9)
    assert result.attempts == 2
    assert result.usage is not None
    assert result.usage.turns == 2
    assert result.usage.input_tokens == 22
    assert result.usage.output_tokens == 10
    assert result.usage.cost_usd == Decimal("0.0246")
    records = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    assert len(records) == 2
    assert "previous structured response failed validation" in records[1]["prompt"]
    assert _option(records[0]["argv"], "--max-turns") == "8"
    assert _option(records[1]["argv"], "--max-turns") == "7"

    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    invalid_fake = _write_fake_claude(invalid_dir / "claude")
    with pytest.raises(ClaudeCodeValidationError) as exc_info:
        await ClaudeCodeRuntime(
            _make_config(invalid_dir, invalid_fake, scenario="always_invalid")
        ).run(_make_task(invalid_dir, limits=RunLimits(request_limit=8, output_retries=10)))
    assert exc_info.value.attempts == 3


async def test_fenced_json_is_still_subject_to_typed_validation(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    result = await ClaudeCodeRuntime(
        _make_config(tmp_path, fake, scenario="prose_fenced_json")
    ).run(_make_task(tmp_path))

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
        ("budget_with_output", ClaudeCodeBudgetError),
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
    config = _make_config(
        tmp_path,
        fake,
        scenario=scenario,
        max_budget_usd=1 if scenario.startswith("budget") else None,
    )
    with pytest.raises(error):
        await ClaudeCodeRuntime(config).run(_make_task(tmp_path))


def test_project_and_local_setting_sources_are_rejected():
    with pytest.raises(ValueError, match="project/local"):
        ClaudeCodeConfig(setting_sources=("user", "local"))


def test_legacy_task_schema_keeps_the_exact_agent_permission(tmp_path: Path):
    task, _ = _make_rule_task(tmp_path)
    policy = ClaudeCodeRuntime._compile_task_policy(
        task,
        native_delegation_tool="Task",
    )

    assert policy.built_in_tools == ("Task",)
    assert policy.allowed_tools[0] == "Agent(vaminer:ast-grep-synthesizer)"
    assert policy.main_allowed_tools[0] == "Agent(vaminer:ast-grep-synthesizer)"
    assert "Agent" not in policy.allowed_tools
    assert "Task" not in policy.allowed_tools
    assert "Task" not in policy.denied_tools


def test_explicit_mcp_failure_requires_absence_of_later_success():
    failed_init = {
        "type": "system",
        "subtype": "init",
        "plugin_errors": [],
        "mcp_servers": [{"name": "vaminer", "status": "failed"}],
    }
    successful_call = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "mcp__vaminer__fetch_cve",
                    "input": {},
                }
            ]
        },
    }
    successful_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "{}",
                }
            ]
        },
    }
    with pytest.raises(ClaudeCodeConfigurationError, match="failed to connect MCP"):
        ClaudeCodeRuntime._validate_initialization_events((failed_init,))
    ClaudeCodeRuntime._validate_initialization_events(
        (failed_init, successful_call, successful_result)
    )


async def test_provider_errors_are_classified_without_repair(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    with pytest.raises(ClaudeCodeProviderError) as exc_info:
        await ClaudeCodeRuntime(
            _make_config(tmp_path, fake, scenario="provider_error")
        ).run(_make_task(tmp_path))
    assert exc_info.value.category == "rate_limit"
    assert exc_info.value.status_code == 429
    assert len(json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))) == 1


async def test_parent_subagent_model_drift_is_rejected(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    with pytest.raises(ClaudeCodeConfigurationError, match="model identity drifted"):
        await ClaudeCodeRuntime(
            _make_config(tmp_path, fake, scenario="model_drift")
        ).run(_make_task(tmp_path))


async def test_stream_artifact_is_readable_while_claude_is_running(tmp_path: Path):
    fake = _write_fake_claude(tmp_path / "claude")
    pending = asyncio.create_task(
        ClaudeCodeRuntime(
            _make_config(
                tmp_path,
                fake,
                scenario="live_stream",
                output_format="stream-json",
            )
        ).run(_make_task(tmp_path))
    )
    event_path: Path | None = None
    for _ in range(100):
        candidates = list((tmp_path / "artifacts").glob("**/attempt-1.stdout.jsonl"))
        if candidates and candidates[0].read_text(encoding="utf-8"):
            event_path = candidates[0]
            break
        await asyncio.sleep(0.01)
    assert event_path is not None
    assert not pending.done()
    assert '"subtype": "init"' in event_path.read_text(encoding="utf-8")
    assert (await pending).output == ProbeOutput(value=7)


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
