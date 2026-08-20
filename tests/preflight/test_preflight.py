from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.miner.preflight import claude as claude_preflight
from src.miner.preflight import runner, tracing
from src.miner.preflight.claude import check_claude_langfuse_hook, check_claude_live, check_mcp_server
from src.miner.preflight.cli import parse_args
from src.miner.preflight import common
from src.miner.preflight.models import CheckResult, CheckStatus, PreflightReport
from src.miner.preflight.progress import start_heartbeat, stop_heartbeat
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.process import ProcessResult, ProcessRunner


def test_report_fails_only_when_a_required_check_fails():
    ready = PreflightReport(
        runtime_id="claude-cli",
        live=False,
        checks=(
            CheckResult.passed("one", "ok"),
            CheckResult.warning("two", "warning"),
            CheckResult.skipped("three", "optional"),
        ),
    )
    blocked = PreflightReport(
        runtime_id="claude-cli",
        live=False,
        checks=(*ready.checks, CheckResult.failed("four", "broken")),
    )

    assert ready.ok is True
    assert blocked.ok is False
    assert blocked.as_dict()["checks"][-1]["status"] == "fail"


def test_rg_preflight_requires_ripgrep_on_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(common.shutil, "which", lambda name: None if name == "rg" else "/usr/bin/" + name)
    missing = common.check_rg()
    assert missing.status is CheckStatus.FAIL
    assert "rg" in (missing.detail or missing.summary)

    monkeypatch.setattr(common.shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
    available = common.check_rg()
    assert available.status is CheckStatus.PASS
    assert "/usr/bin/rg" in available.summary


def test_langfuse_check_distinguishes_disabled_incomplete_and_authenticated(monkeypatch: pytest.MonkeyPatch):
    disabled, disabled_client = tracing.check_langfuse({"LANGFUSE_TRACING_ENABLED": "false"})
    incomplete, incomplete_client = tracing.check_langfuse({"LANGFUSE_PUBLIC_KEY": "pk"})
    fake_client = object()
    monkeypatch.setattr(tracing, "configure_tracing", lambda: fake_client)
    authenticated, authenticated_client = tracing.check_langfuse(
        {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}
    )

    assert (disabled.status, disabled_client) == (CheckStatus.SKIP, None)
    assert (incomplete.status, incomplete_client) == (CheckStatus.FAIL, None)
    assert (authenticated.status, authenticated_client) == (CheckStatus.PASS, fake_client)


@pytest.mark.asyncio
async def test_trace_ingestion_requires_runtime_observations():
    trace = SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(
            observations=[
                SimpleNamespace(name="VAMiner Preflight", type="CHAIN"),
                SimpleNamespace(name="Conversational Turn", type="SPAN"),
            ]
        )
    )
    client = SimpleNamespace(flush=lambda: None, api=SimpleNamespace(trace=trace))

    result = await tracing.check_trace_ingestion(
        client,
        trace_id="0" * 32,
        minimum_observations=2,
        timeout_seconds=1,
    )

    assert result.status is CheckStatus.PASS
    assert "2 probe observations" in result.summary


def test_claude_hook_check_exercises_bundled_in_process_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    skipped = check_claude_langfuse_hook(None, "claude", required=False)
    available = check_claude_langfuse_hook(object(), "/usr/local/bin/codeagent", required=True)
    monkeypatch.setattr(claude_preflight, "BUNDLED_HOOK", tmp_path / "missing-hook.py")
    missing = check_claude_langfuse_hook(object(), "claude", required=True)

    assert skipped.status is CheckStatus.SKIP
    assert available.status is CheckStatus.PASS
    assert "api=in-process" in (available.detail or "")
    assert "codeagent" in (available.detail or "")
    assert missing.status is CheckStatus.FAIL


@pytest.mark.asyncio
async def test_codeagent_live_probe_skips_safe_check(monkeypatch: pytest.MonkeyPatch):
    captured_argv: list[str] = []
    events = [
        {
            "type": "system",
            "subtype": "init",
            "tools": ["mcp__vaminer__list_src_files"],
            "mcp_servers": [{"name": "vaminer", "status": "connected"}],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__vaminer__list_src_files",
                        "id": "probe-tool",
                        "input": {},
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "structured_output": {
                "greeting": "hello from vaminer",
                "mcp_sentinel_seen": True,
            },
        },
    ]

    async def fake_run(self, argv, **kwargs):
        captured_argv.extend(argv)
        lines = [json.dumps(event) for event in events]
        for line in lines:
            kwargs["stdout_line_handler"](line)
        return ProcessResult(stdout="\n".join(lines), stderr="", returncode=0, duration_ms=1)

    monkeypatch.setattr(ProcessRunner, "run", fake_run)
    monkeypatch.setattr("src.miner.preflight.claude.cleanup_session_transcript", lambda *_args: ())

    result = await check_claude_live(
        ClaudeCodeConfig(executable="codeagent"),
        executable="/usr/local/bin/codeagent",
        tracing_active=False,
        timeout_seconds=1,
    )

    assert result.status is CheckStatus.PASS
    assert captured_argv[1] == "--skip-safe-check"


@pytest.mark.asyncio
async def test_failed_claude_live_probe_does_not_wait_for_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    passed = lambda name: CheckResult.passed(name, "ok")
    monkeypatch.setattr(runner, "check_python", lambda: passed("python"))
    monkeypatch.setattr(runner, "check_project_assets", lambda: passed("assets"))
    monkeypatch.setattr(runner, "check_paths", lambda **_kwargs: passed("paths"))
    monkeypatch.setattr(runner, "check_git", lambda: passed("git"))
    monkeypatch.setattr(runner, "check_rg", lambda: passed("rg"))
    monkeypatch.setattr(runner, "check_ast_grep", lambda **_kwargs: passed("ast-grep"))
    monkeypatch.setattr(runner, "check_langfuse", lambda: (passed("langfuse.auth"), object()))
    monkeypatch.setattr(runner, "tracing_requested", lambda: True)
    monkeypatch.setattr(runner, "check_claude_cli", lambda *_args, **_kwargs: (passed("claude.cli"), "claude"))
    monkeypatch.setattr(
        runner,
        "check_claude_langfuse_hook",
        lambda *_args, **_kwargs: passed("claude.langfuse-hook"),
    )

    async def check_mcp(*_args, **_kwargs):
        return passed("claude.mcp")

    async def check_live(*_args, **_kwargs):
        return CheckResult.failed("claude.agent-live", "probe failed")

    async def fail_if_trace_checked(*_args, **_kwargs):
        pytest.fail("trace ingestion must not be checked after a failed Claude live probe")

    @contextmanager
    def traced_pipeline(**_kwargs):
        yield SimpleNamespace(trace_id="0" * 32, observation=object(), update=lambda **_values: None)

    monkeypatch.setattr(runner, "check_mcp_server", check_mcp)
    monkeypatch.setattr(runner, "check_claude_live", check_live)
    monkeypatch.setattr(runner, "check_trace_ingestion", fail_if_trace_checked)
    monkeypatch.setattr(runner, "trace_pipeline", traced_pipeline)

    report = await runner.run_preflight(
        runner.PreflightOptions(
            runtime_id="claude-cli",
            workspace_dir=tmp_path,
            output_dir=tmp_path,
            rules_dir=tmp_path,
            live=True,
            timeout_seconds=1,
            trace_wait_seconds=1,
        ),
        claude_config=ClaudeCodeConfig(executable="claude"),
    )

    assert report.checks[-2].status is CheckStatus.FAIL
    assert report.checks[-1].status is CheckStatus.SKIP
    assert report.checks[-1].name == "langfuse.trace"


@pytest.mark.asyncio
async def test_real_mcp_preflight_handshake_and_tool_call():
    project_root = Path(__file__).resolve().parents[2]
    progress = []

    result = await check_mcp_server(
        ClaudeCodeConfig(executable="/bin/true", project_root=project_root),
        timeout_seconds=15,
        progress=progress.append,
    )

    assert result.status is CheckStatus.PASS, result.detail
    assert any("MCP initialize completed" in message for message in progress)


@pytest.mark.asyncio
async def test_mcp_preflight_reports_nested_exception_and_server_stderr(tmp_path: Path):
    project_root = Path(__file__).resolve().parents[2]
    failing_server = tmp_path / "failing-mcp"
    failing_server.write_text("#!/bin/sh\necho 'MCP bootstrap failed' >&2\nexit 17\n", encoding="utf-8")
    failing_server.chmod(0o755)

    result = await check_mcp_server(
        ClaudeCodeConfig(executable="/bin/true", project_root=project_root, mcp_python=failing_server),
        timeout_seconds=5,
    )

    assert result.status is CheckStatus.FAIL
    assert "MCPError: Connection closed" in (result.detail or "")
    assert "stderr: MCP bootstrap failed" in (result.detail or "")


def test_preflight_cli_is_input_free_and_live_is_explicit():
    args = parse_args(["--runtime", "claude-cli"])
    live = parse_args(["--runtime", "pydanic-sdk", "--live", "--json", "--quiet"])

    assert args.live is False
    assert args.trace_wait_seconds == 120.0
    assert live.live is True
    assert live.json is True
    assert live.quiet is True


@pytest.mark.asyncio
async def test_progress_heartbeat_reports_long_waits():
    messages = []
    heartbeat = start_heartbeat(
        messages.append,
        message="Agent request is running",
        timeout_seconds=1,
        interval_seconds=0.01,
    )

    await asyncio.sleep(0.025)
    await stop_heartbeat(heartbeat)

    assert any("still waiting" in message for message in messages)
