from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.miner.preflight import tracing
from src.miner.preflight.claude import check_claude_langfuse_plugin, check_mcp_server
from src.miner.preflight.cli import parse_args
from src.miner.preflight.models import CheckResult, CheckStatus, PreflightReport
from src.miner.preflight.progress import start_heartbeat, stop_heartbeat
from src.miner.runtimes.claude.config import ClaudeCodeConfig


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


def test_claude_plugin_check_requires_installed_official_plugin(monkeypatch: pytest.MonkeyPatch):
    def completed(enabled: bool | None) -> subprocess.CompletedProcess[str]:
        plugins = (
            "[]"
            if enabled is None
            else (
                '[{"id":"langfuse-observability@langfuse-observability",'
                f'"version":"1.0.0","scope":"user","enabled":{str(enabled).lower()}}}]'
            )
        )
        return subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=plugins,
            stderr="",
        )

    monkeypatch.setattr("src.miner.preflight.claude._run_cli", lambda *_args, **_kwargs: completed(None))
    missing = check_claude_langfuse_plugin("claude", required=True, timeout_seconds=1)
    monkeypatch.setattr("src.miner.preflight.claude._run_cli", lambda *_args, **_kwargs: completed(False))
    disabled = check_claude_langfuse_plugin("claude", required=True, timeout_seconds=1)
    monkeypatch.setattr("src.miner.preflight.claude._run_cli", lambda *_args, **_kwargs: completed(True))
    enabled = check_claude_langfuse_plugin("claude", required=True, timeout_seconds=1)

    assert missing.status is CheckStatus.FAIL
    assert disabled.status is CheckStatus.PASS
    assert "activation per invocation" in (disabled.detail or "")
    assert enabled.status is CheckStatus.PASS


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
