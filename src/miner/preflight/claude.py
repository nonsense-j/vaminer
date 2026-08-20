"""Claude CLI, bundled tracing, MCP, and live Agent preflight checks."""

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
from typing import Literal, TextIO

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
from ..runtimes.claude.tracing import (
    BUNDLED_HOOK,
    BUNDLED_HOOK_LICENSE,
    BUNDLED_HOOK_VERSION,
    emit_session_trace,
    probe_bundled_hook,
)
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


def _exception_detail(exc: BaseException) -> str:
    """Keep the useful leaf messages when AnyIO wraps an MCP error in groups."""

    if isinstance(exc, BaseExceptionGroup):
        details = [_exception_detail(child) for child in exc.exceptions]
        return "; ".join(dict.fromkeys(detail for detail in details if detail))
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _mcp_failure_detail(error_log: TextIO, *details: str | None) -> str:
    error_log.flush()
    error_log.seek(0)
    stderr = error_log.read().strip()
    parts = [detail for detail in details if detail]
    if stderr:
        parts.append(f"stderr: {stderr}")
    return "; ".join(parts) or "MCP failure did not include diagnostic details"


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
    runtime_name = config.display_name
    compiler = PolicyCompiler(config)
    environment = compiler.environment()
    try:
        executable = compiler.resolve_executable(environment)
        version = _run_cli([executable, "--version"], environment=environment, timeout_seconds=timeout_seconds)
        if version.returncode != 0:
            detail = redact(version.stderr or version.stdout).strip()
            return CheckResult.failed("claude.cli", f"{runtime_name} CLI version check failed", detail=detail), None
        help_result = _run_cli([executable, "--help"], environment=environment, timeout_seconds=timeout_seconds)
        if help_result.returncode != 0:
            detail = redact(help_result.stderr or help_result.stdout).strip()
            return CheckResult.failed("claude.cli", f"{runtime_name} CLI help check failed", detail=detail), None
        missing = sorted(flag for flag in _REQUIRED_CLAUDE_FLAGS if flag not in help_result.stdout)
        if missing:
            return CheckResult.failed(
                "claude.cli",
                f"{runtime_name} CLI does not support flags required by VAMiner",
                detail=", ".join(missing),
            ), None
    except Exception as exc:  # noqa: BLE001
        return (
            CheckResult.failed(
                "claude.cli",
                f"{runtime_name} CLI could not be executed",
                detail=f"{type(exc).__name__}: {exc}",
            ),
            None,
        )
    return (
        CheckResult.passed(
            "claude.cli",
            f"{version.stdout.strip() or runtime_name + ' CLI'} supports the required invocation flags",
            detail=executable,
            duration_ms=round((time.monotonic() - started) * 1000),
        ),
        executable,
    )


def check_claude_langfuse_hook(
    langfuse_client: object | None,
    executable: str | None,
    *,
    display_name: str = "Claude",
    required: bool,
) -> CheckResult:
    if not required:
        return CheckResult.skipped("claude.langfuse-hook", "Langfuse tracing is not enabled for this run")
    if langfuse_client is None:
        return CheckResult.failed(
            "claude.langfuse-hook",
            "The bundled hook cannot be exercised without an authenticated Langfuse client",
        )
    if executable is None:
        return CheckResult.failed(
            "claude.langfuse-hook",
            f"{display_name} CLI is unavailable, so its tracing identity cannot be checked",
        )
    started = time.monotonic()
    try:
        if not BUNDLED_HOOK.is_file() or not BUNDLED_HOOK_LICENSE.is_file():
            missing = [str(path) for path in (BUNDLED_HOOK, BUNDLED_HOOK_LICENSE) if not path.is_file()]
            return CheckResult.failed(
                "claude.langfuse-hook",
                "Bundled Langfuse hook assets are missing",
                detail=", ".join(missing),
            )
        compile(BUNDLED_HOOK.read_text(encoding="utf-8"), str(BUNDLED_HOOK), "exec")
        with tempfile.TemporaryDirectory(prefix="vaminer-preflight-langfuse-hook-") as raw_temp:
            temporary_root = Path(raw_temp)
            transcript = temporary_root / "empty-session.jsonl"
            transcript.write_text("", encoding="utf-8")
            emitted = probe_bundled_hook(
                langfuse_client,
                transcript_path=transcript,
                state_dir=temporary_root / "state",
                executable=executable,
                display_name=display_name,
                traceparent="00-0123456789abcdef0123456789abcdef-fedcba9876543210-01",
            )
        if emitted != 0:
            return CheckResult.failed(
                "claude.langfuse-hook",
                "Bundled Langfuse hook probe emitted observations for an empty transcript",
                detail=f"emitted={emitted}",
            )
        return CheckResult.passed(
            "claude.langfuse-hook",
            f"Bundled Langfuse hook {BUNDLED_HOOK_VERSION} loaded and parsed an empty transcript",
            detail=f"cli={Path(executable).name}; name={display_name}; api=in-process",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "claude.langfuse-hook",
            "Bundled Langfuse hook probe failed",
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
                try:
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
                                        detail=_mcp_failure_detail(error_log, ", ".join(missing)),
                                    )
                                result = await session.call_tool("list_src_files", {"max_results": 10})
                                structured = result.structured_content or {}
                                if result.is_error or _SENTINEL_FILE not in structured.get("files", []):
                                    return CheckResult.failed(
                                        "claude.mcp",
                                        "VAMiner MCP tool call returned an unexpected result",
                                        detail=_mcp_failure_detail(
                                            error_log,
                                            f"is_error={result.is_error}; payload={structured!r}",
                                        ),
                                    )
                except Exception as exc:  # noqa: BLE001
                    return CheckResult.failed(
                        "claude.mcp",
                        "VAMiner MCP handshake or tool call failed",
                        detail=_mcp_failure_detail(error_log, _exception_detail(exc)),
                    )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "claude.mcp",
            "VAMiner MCP handshake or tool call failed",
            detail=_exception_detail(exc),
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
    runtime_name = config.display_name
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
                "enabledPlugins": {LANGFUSE_CLAUDE_PLUGIN_ID: False},
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
                cli_name=runtime_name,
                runtime_log=RuntimeLog(emit_console=False),
                expected_mcp_server=SERVER_NAME,
                expected_mcp_tools=(_MCP_TOOL,),
            )
            runner = ProcessRunner(
                max_stdout_bytes=config.max_stdout_bytes,
                max_stderr_bytes=config.max_stderr_bytes,
                terminate_grace_seconds=config.terminate_grace_seconds,
                cli_name=runtime_name,
            )
            first_event = False
            hook_result = None

            def feed_line(raw: str) -> None:
                nonlocal first_event
                decoder.feed_line(raw)
                if not first_event and decoder.parsed_count:
                    first_event = True
                    notify(
                        progress,
                        f"{runtime_name} emitted its first stream event; waiting for Agent/tool completion ...",
                    )

            heartbeat = start_heartbeat(
                progress,
                message=f"{runtime_name} Agent subprocess is running",
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
                if tracing_active:
                    hook_result = await emit_session_trace(
                        session_id,
                        environment=environment,
                        state_dir=workspace / "langfuse-state",
                        executable=executable,
                        display_name=runtime_name,
                    )
            notify(progress, f"{runtime_name} subprocess exited; validating the terminal structured output ...")
            decoded = decoder.finish(process)
            if decoded.validation_errors or decoded.output is None:
                return CheckResult.failed(
                    "claude.agent-live",
                    f"{runtime_name} CLI did not return the required structured probe output",
                    detail="; ".join(decoded.validation_errors),
                )
            if not any(event.type == "tool.call" for event in decoded.events):
                return CheckResult.failed(
                    "claude.agent-live",
                    f"{runtime_name} returned the probe output without calling the required MCP tool",
                )
            trace_carrier = claude_trace_environment()
            if tracing_active and "CC_LANGFUSE_TRACEPARENT" not in environment:
                return CheckResult.failed(
                    "claude.agent-live",
                    f"{runtime_name} completed, but no Langfuse traceparent reached the child process",
                    detail=f"active carrier now present={bool(trace_carrier)}",
                )
            if tracing_active and (
                hook_result is None
                or hook_result.transcript_count == 0
                or hook_result.invoked_count == 0
                or hook_result.emitted_turn_count == 0
                or not hook_result.ok
            ):
                detail = "bundled hook was not invoked"
                if hook_result is not None:
                    detail = (
                        f"transcripts={hook_result.transcript_count}; "
                        f"invocations={hook_result.invoked_count}; "
                        f"emitted_turns={hook_result.emitted_turn_count}; "
                        f"errors={'; '.join(hook_result.errors) or 'none'}"
                    )
                return CheckResult.failed(
                    "claude.agent-live",
                    f"{runtime_name} completed, but the bundled Langfuse hook did not process its transcript",
                    detail=detail,
                )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "claude.agent-live",
            f"{runtime_name} live Agent probe failed",
            detail=f"{type(exc).__name__}: {redact(str(exc))}",
        )
    finally:
        cleanup_session_transcript(
            session_id,
            environment,
            executable=executable,
        )
    return CheckResult.passed(
        "claude.agent-live",
        f"{runtime_name} authenticated, connected VAMiner MCP, called list_src_files, and returned structured output",
        duration_ms=round((time.monotonic() - started) * 1000),
    )


__all__ = [
    "check_claude_cli",
    "check_claude_langfuse_hook",
    "check_claude_live",
    "check_mcp_server",
]
