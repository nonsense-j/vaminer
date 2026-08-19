"""Claude CLI, plugin, MCP, and live Agent preflight checks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import BaseModel

from ..runtimes.claude.config import LANGFUSE_CLAUDE_PLUGIN_ID, ClaudeCodeConfig
from ..runtimes.claude.mcp import (
    CASES_DIR_ENV,
    PROFILE_ENV,
    SERVER_NAME,
    SOURCE_ROOT_ENV,
    WORKSPACE_ROOT_ENV,
    MCPProfile,
)
from ..runtimes.claude.policy import PolicyCompiler, cleanup_session_transcript
from ..runtimes.claude.process import ProcessRunner, redact
from ..runtimes.claude.protocol import ClaudeStreamDecoder
from ..utils.log import RuntimeLog
from ..utils.telemetry import claude_trace_environment, propagated_trace_environment
from .models import CheckResult
from .progress import ProgressCallback, notify, start_heartbeat, stop_heartbeat

_MCP_TOOL = f"mcp__{SERVER_NAME}__list_src_files"
_EXPECTED_MCP_TOOLS = {
    "list_src_files",
    "search_src_files",
    "read_src_file",
    "list_case_artifacts",
    "read_case_artifact",
    "write_case_artifact",
}
_REQUIRED_CLAUDE_FLAGS = {
    "--allowedTools",
    "--disable-slash-commands",
    "--json-schema",
    "--mcp-config",
    "--setting-sources",
    "--strict-mcp-config",
}
_SENTINEL_FILE = "vaminer_preflight.c"


class _ClaudeProbeOutput(BaseModel):
    greeting: Literal["hello from vaminer"]
    mcp_sentinel_seen: Literal[True]


def _run_cli(
    argv: list[str],
    *,
    environment: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=environment,
        text=True,
        capture_output=True,
        timeout=max(1.0, timeout_seconds),
        check=False,
    )


def check_claude_cli(config: ClaudeCodeConfig, *, timeout_seconds: float) -> tuple[CheckResult, str | None]:
    started = time.monotonic()
    compiler = PolicyCompiler(config)
    environment = compiler.environment()
    try:
        executable = compiler.resolve_executable(environment)
        version = _run_cli([executable, "--version"], environment=environment, timeout_seconds=timeout_seconds)
        if version.returncode != 0:
            detail = redact(version.stderr or version.stdout).strip()
            return CheckResult.failed("claude.cli", "Claude CLI version check failed", detail=detail), None
        help_result = _run_cli([executable, "--help"], environment=environment, timeout_seconds=timeout_seconds)
        if help_result.returncode != 0:
            detail = redact(help_result.stderr or help_result.stdout).strip()
            return CheckResult.failed("claude.cli", "Claude CLI help check failed", detail=detail), None
        missing = sorted(flag for flag in _REQUIRED_CLAUDE_FLAGS if flag not in help_result.stdout)
        if missing:
            return CheckResult.failed(
                "claude.cli",
                "Claude CLI does not support flags required by VAMiner",
                detail=", ".join(missing),
            ), None
    except Exception as exc:  # noqa: BLE001
        return (
            CheckResult.failed(
                "claude.cli",
                "Claude CLI could not be executed",
                detail=f"{type(exc).__name__}: {exc}",
            ),
            None,
        )
    return (
        CheckResult.passed(
            "claude.cli",
            f"{version.stdout.strip() or 'Claude CLI'} supports the required invocation flags",
            detail=executable,
            duration_ms=round((time.monotonic() - started) * 1000),
        ),
        executable,
    )


def check_claude_langfuse_plugin(
    executable: str | None,
    *,
    required: bool,
    timeout_seconds: float,
) -> CheckResult:
    if not required:
        return CheckResult.skipped("claude.langfuse-plugin", "Langfuse tracing is not enabled for this run")
    if executable is None:
        return CheckResult.failed(
            "claude.langfuse-plugin",
            "Claude CLI is unavailable, so its plugin cannot be checked",
        )
    try:
        completed = _run_cli(
            [executable, "plugin", "list", "--json"],
            environment=os.environ.copy(),
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode != 0:
            return CheckResult.failed(
                "claude.langfuse-plugin",
                "Claude plugin inventory command failed",
                detail=redact(completed.stderr or completed.stdout).strip(),
            )
        plugins = json.loads(completed.stdout)
        plugin = next(
            (
                item
                for item in plugins
                if isinstance(item, dict) and item.get("id") == LANGFUSE_CLAUDE_PLUGIN_ID
            ),
            None,
        )
        if plugin is None:
            return CheckResult.failed(
                "claude.langfuse-plugin",
                "The official Langfuse Claude plugin is not installed",
                detail=f"missing {LANGFUSE_CLAUDE_PLUGIN_ID}",
            )
        return CheckResult.passed(
            "claude.langfuse-plugin",
            f"Langfuse Claude plugin {plugin.get('version') or 'unknown version'} is installed",
            detail=(
                f"scope={plugin.get('scope') or 'unknown'}; "
                "VAMiner manages activation per invocation"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "claude.langfuse-plugin",
            "Claude plugin inventory could not be validated",
            detail=f"{type(exc).__name__}: {exc}",
        )


def _mcp_environment(config: ClaudeCodeConfig, *, workspace: Path, source: Path, cases: Path) -> dict[str, str]:
    return {
        PROFILE_ENV: MCPProfile.ROOT_CAUSE.value,
        WORKSPACE_ROOT_ENV: str(workspace),
        SOURCE_ROOT_ENV: str(source),
        CASES_DIR_ENV: str(cases),
        "PYTHONPATH": str(config.project_root),
        **propagated_trace_environment(),
    }


def _prepare_probe_workspace(root: Path) -> tuple[Path, Path]:
    source = root / "src"
    cases = root / "cases"
    source.mkdir()
    cases.mkdir()
    (source / _SENTINEL_FILE).write_text("int vaminer_preflight(void) { return 0; }\n", encoding="utf-8")
    return source, cases


async def check_mcp_server(
    config: ClaudeCodeConfig,
    *,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
) -> CheckResult:
    started = time.monotonic()
    heartbeat = start_heartbeat(
        progress,
        message="VAMiner MCP subprocess is running",
        timeout_seconds=timeout_seconds,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="vaminer-preflight-mcp-") as raw_temp:
            workspace = Path(raw_temp).resolve()
            source, cases = _prepare_probe_workspace(workspace)
            python = str(config.mcp_python or Path(sys.executable).absolute())
            parameters = StdioServerParameters(
                command=python,
                args=["-m", "src.miner.runtimes.claude.mcp"],
                env=_mcp_environment(config, workspace=workspace, source=source, cases=cases),
                cwd=config.project_root,
            )
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error_log:
                async with asyncio.timeout(timeout_seconds):
                    async with stdio_client(parameters, errlog=error_log) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            notify(progress, "MCP initialize completed; requesting tool inventory ...")
                            tools = await session.list_tools()
                            available = {tool.name for tool in tools.tools}
                            missing = sorted(_EXPECTED_MCP_TOOLS - available)
                            if missing:
                                return CheckResult.failed(
                                    "claude.mcp",
                                    "VAMiner MCP server omitted required Root Cause tools",
                                    detail=", ".join(missing),
                                )
                            result = await session.call_tool("list_src_files", {"max_results": 10})
                            structured = result.structured_content or {}
                            if result.is_error or _SENTINEL_FILE not in structured.get("files", []):
                                return CheckResult.failed(
                                    "claude.mcp",
                                    "VAMiner MCP tool call returned an unexpected result",
                                    detail=f"is_error={result.is_error}; payload={structured!r}",
                                )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "claude.mcp",
            "VAMiner MCP handshake or tool call failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await stop_heartbeat(heartbeat)
    return CheckResult.passed(
        "claude.mcp",
        f"MCP handshake exposed {len(_EXPECTED_MCP_TOOLS)} Root Cause tools and list_src_files worked",
        duration_ms=round((time.monotonic() - started) * 1000),
    )


async def check_claude_live(
    config: ClaudeCodeConfig,
    *,
    executable: str,
    tracing_active: bool,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
) -> CheckResult:
    started = time.monotonic()
    session_id = str(uuid.uuid4())
    compiler = PolicyCompiler(config)
    environment = compiler.environment()
    try:
        with tempfile.TemporaryDirectory(prefix="vaminer-preflight-claude-") as raw_temp:
            workspace = Path(raw_temp).resolve()
            source, cases = _prepare_probe_workspace(workspace)
            settings_path = workspace / "settings.json"
            prompt_path = workspace / "system-prompt.md"
            mcp_path = workspace / "mcp.json"

            settings: dict[str, object] = {
                "permissions": {"defaultMode": "dontAsk", "allow": [_MCP_TOOL]},
                "enableAllProjectMcpServers": False,
                "enabledPlugins": {LANGFUSE_CLAUDE_PLUGIN_ID: tracing_active},
            }
            settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            prompt_path.write_text(
                "You are a VAMiner runtime preflight probe. Use only the allowed MCP tool and return the schema.",
                encoding="utf-8",
            )
            python = str(config.mcp_python or Path(sys.executable).absolute())
            mcp_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            SERVER_NAME: {
                                "type": "stdio",
                                "command": python,
                                "args": ["-m", "src.miner.runtimes.claude.mcp"],
                                "env": _mcp_environment(
                                    config,
                                    workspace=workspace,
                                    source=source,
                                    cases=cases,
                                ),
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            argv = [
                executable,
                "--print",
                "--setting-sources",
                "user",
                "--settings",
                str(settings_path),
                "--system-prompt-file",
                str(prompt_path),
                "--input-format",
                "text",
                "--output-format",
                "stream-json",
                "--verbose",
                "--session-id",
                session_id,
                "--json-schema",
                json.dumps(_ClaudeProbeOutput.model_json_schema()),
                "--mcp-config",
                str(mcp_path),
                "--strict-mcp-config",
                "--tools",
                "",
                "--allowedTools",
                _MCP_TOOL,
                "--permission-mode",
                "dontAsk",
                "--disable-slash-commands",
                "--max-turns",
                "3",
            ]
            if config.model is not None:
                argv.extend(("--model", config.model))
            if config.effort is not None:
                argv.extend(("--effort", config.effort))

            decoder = ClaudeStreamDecoder(
                output_type=_ClaudeProbeOutput,
                agent_name="Preflight Agent",
                runtime_log=RuntimeLog(emit_console=False),
                expected_mcp_server=SERVER_NAME,
                expected_mcp_tools=(_MCP_TOOL,),
            )
            runner = ProcessRunner(
                max_stdout_bytes=config.max_stdout_bytes,
                max_stderr_bytes=config.max_stderr_bytes,
                terminate_grace_seconds=config.terminate_grace_seconds,
            )
            first_event = False

            def feed_line(raw: str) -> None:
                nonlocal first_event
                decoder.feed_line(raw)
                if not first_event and decoder.parsed_count:
                    first_event = True
                    notify(progress, "Claude emitted its first stream event; waiting for Agent/tool completion ...")

            heartbeat = start_heartbeat(
                progress,
                message="Claude Agent subprocess is running",
                timeout_seconds=timeout_seconds,
            )
            try:
                process = await runner.run(
                    argv,
                    cwd=workspace,
                    environment=environment,
                    prompt=(
                        "Call mcp__vaminer__list_src_files. Confirm that its files include "
                        f"{_SENTINEL_FILE}, then return greeting='hello from vaminer' and mcp_sentinel_seen=true."
                    ),
                    timeout_seconds=timeout_seconds,
                    stdout_line_handler=feed_line,
                )
            finally:
                await stop_heartbeat(heartbeat)
            notify(progress, "Claude subprocess exited; validating the terminal structured output ...")
            decoded = decoder.finish(process)
            if decoded.validation_errors or decoded.output is None:
                return CheckResult.failed(
                    "claude.agent-live",
                    "Claude CLI did not return the required structured probe output",
                    detail="; ".join(decoded.validation_errors),
                )
            if not any(event.type == "tool.call" for event in decoded.events):
                return CheckResult.failed(
                    "claude.agent-live",
                    "Claude returned the probe output without calling the required MCP tool",
                )
            trace_carrier = claude_trace_environment()
            if tracing_active and "CC_LANGFUSE_TRACEPARENT" not in environment:
                return CheckResult.failed(
                    "claude.agent-live",
                    "Claude completed, but no Langfuse traceparent reached the child process",
                    detail=f"active carrier now present={bool(trace_carrier)}",
                )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "claude.agent-live",
            "Claude live Agent probe failed",
            detail=f"{type(exc).__name__}: {redact(str(exc))}",
        )
    finally:
        cleanup_session_transcript(session_id, environment)
    return CheckResult.passed(
        "claude.agent-live",
        "Claude authenticated, connected VAMiner MCP, called list_src_files, and returned structured output",
        duration_ms=round((time.monotonic() - started) * 1000),
    )


__all__ = [
    "check_claude_cli",
    "check_claude_langfuse_plugin",
    "check_claude_live",
    "check_mcp_server",
]
