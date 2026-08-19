"""Orchestrate VAMiner preflight checks without starting a mining workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..runtimes.claude.config import ClaudeCodeConfig
from ..utils.telemetry import trace_pipeline
from .claude import (
    check_claude_cli,
    check_claude_langfuse_plugin,
    check_claude_live,
    check_mcp_server,
)
from .common import check_ast_grep, check_git, check_paths, check_project_assets, check_python, check_rg
from .models import CheckResult, CheckStatus, PreflightReport
from .pydantic import check_pydantic_config, check_pydantic_live
from .progress import ProgressCallback, notify
from .tracing import check_langfuse, check_trace_ingestion, tracing_requested

RUNTIME_IDS = ("pydanic-sdk", "claude-cli")


@dataclass(frozen=True, slots=True)
class PreflightOptions:
    runtime_id: str
    workspace_dir: Path
    output_dir: Path
    rules_dir: Path
    live: bool = False
    timeout_seconds: float = 300.0
    trace_wait_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.runtime_id not in RUNTIME_IDS:
            raise ValueError(f"unsupported runtime: {self.runtime_id!r}")
        if self.timeout_seconds <= 0 or self.trace_wait_seconds <= 0:
            raise ValueError("preflight timeouts must be positive")


async def run_preflight(
    options: PreflightOptions,
    *,
    claude_config: ClaudeCodeConfig | None = None,
    progress: ProgressCallback | None = None,
) -> PreflightReport:
    checks: list[CheckResult] = []

    def result_message(check: CheckResult) -> str:
        detail = f"; detail={check.detail}" if check.detail else ""
        return f"{check.status.value.upper()} {check.name}: {check.summary}{detail}"

    def run_check(label: str, function: Callable[[], CheckResult]) -> None:
        notify(progress, f"checking {label} ...")
        check = function()
        checks.append(check)
        notify(progress, result_message(check))

    run_check("Python", check_python)
    run_check("project assets", check_project_assets)
    run_check(
        "writable paths",
        lambda: check_paths(
            workspace_dir=options.workspace_dir,
            output_dir=options.output_dir,
            rules_dir=options.rules_dir,
        ),
    )
    run_check("Git", check_git)
    run_check("ripgrep source navigation", check_rg)
    run_check("ast-grep functional query", lambda: check_ast_grep(timeout_seconds=options.timeout_seconds))

    notify(progress, "checking Langfuse authentication ...")
    langfuse_check, langfuse_client = check_langfuse()
    checks.append(langfuse_check)
    notify(progress, result_message(langfuse_check))

    executable: str | None = None
    active_claude_config = claude_config or ClaudeCodeConfig()
    if options.runtime_id == "pydanic-sdk":
        run_check("Pydantic model configuration", check_pydantic_config)
    else:
        notify(progress, "checking Claude CLI executable and supported flags ...")
        cli_check, executable = check_claude_cli(
            active_claude_config,
            timeout_seconds=options.timeout_seconds,
        )
        checks.append(cli_check)
        notify(progress, result_message(cli_check))
        notify(progress, "checking Claude Langfuse plugin ...")
        plugin_check = check_claude_langfuse_plugin(
            executable,
            required=tracing_requested(),
            timeout_seconds=options.timeout_seconds,
        )
        checks.append(plugin_check)
        notify(progress, result_message(plugin_check))
        notify(progress, "checking VAMiner MCP handshake and tool call ...")
        mcp_check = await check_mcp_server(
            active_claude_config,
            timeout_seconds=options.timeout_seconds,
            progress=progress,
        )
        checks.append(mcp_check)
        notify(progress, result_message(mcp_check))

    live_name = "pydantic.agent-live" if options.runtime_id == "pydanic-sdk" else "claude.agent-live"
    if not options.live:
        live_check = CheckResult.skipped(live_name, "Use --live to make a real model request")
        trace_check = CheckResult.skipped("langfuse.trace", "Use --live to emit and verify a probe trace")
        checks.extend((live_check, trace_check))
        notify(progress, result_message(live_check))
        notify(progress, result_message(trace_check))
        return PreflightReport(options.runtime_id, options.live, tuple(checks))

    failed_prerequisites = [check.name for check in checks if check.status is CheckStatus.FAIL]
    if failed_prerequisites:
        live_check = CheckResult.skipped(
            live_name,
            "Live model request was not made because prerequisites failed: " + ", ".join(failed_prerequisites),
        )
        trace_check = CheckResult.skipped("langfuse.trace", "No live probe trace was emitted")
        checks.extend((live_check, trace_check))
        notify(progress, result_message(live_check))
        notify(progress, result_message(trace_check))
        return PreflightReport(options.runtime_id, options.live, tuple(checks))

    trace_id: str | None = None
    with trace_pipeline(
        mining_input={"kind": "preflight", "runtime": options.runtime_id},
        runtime_id=options.runtime_id,
    ) as pipeline:
        notify(progress, f"starting {live_name} (timeout {options.timeout_seconds:g}s) ...")
        if pipeline.observation is not None:
            trace_id = pipeline.trace_id
            pipeline.update(
                name=f"VAMiner Preflight @{options.runtime_id}",
                metadata={"kind": "preflight", "runtime": options.runtime_id},
            )
        if options.runtime_id == "pydanic-sdk":
            live_check = await check_pydantic_live(
                timeout_seconds=options.timeout_seconds,
                progress=progress,
            )
        else:
            assert executable is not None
            live_check = await check_claude_live(
                active_claude_config,
                executable=executable,
                tracing_active=pipeline.observation is not None,
                timeout_seconds=options.timeout_seconds,
                progress=progress,
            )
        pipeline.update(output=live_check.as_dict())
        checks.append(live_check)
        notify(progress, result_message(live_check))

    if options.runtime_id == "claude-cli" and live_check.status is CheckStatus.FAIL:
        trace_check = CheckResult.skipped(
            "langfuse.trace",
            "Claude live Agent probe failed; trace ingestion was not checked",
        )
        checks.append(trace_check)
        notify(progress, result_message(trace_check))
        return PreflightReport(options.runtime_id, options.live, tuple(checks))

    notify(progress, "checking Langfuse trace ingestion ...")
    trace_check = await check_trace_ingestion(
        langfuse_client,
        trace_id=trace_id,
        minimum_observations=2,
        timeout_seconds=options.trace_wait_seconds,
        progress=progress,
    )
    checks.append(trace_check)
    notify(progress, result_message(trace_check))
    return PreflightReport(options.runtime_id, options.live, tuple(checks))


__all__ = ["PreflightOptions", "RUNTIME_IDS", "run_preflight"]
