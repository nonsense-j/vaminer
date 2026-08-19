"""Command-line interface for VAMiner environment diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..runtimes.claude.config import COMMAND, MODEL, TIMEOUT_SECONDS, ClaudeCodeConfig
from ..utils.config import MINER_AGENT_RUNTIME, MINER_OUTPUT_DIR, VAS_RULES_DIR, VAS_WORKSPACE_DIR
from .models import CheckStatus, PreflightReport
from .progress import ProgressCallback
from .runner import RUNTIME_IDS, PreflightOptions, run_preflight

_STATUS_STYLE = {
    CheckStatus.PASS: "green",
    CheckStatus.WARN: "yellow",
    CheckStatus.FAIL: "bold red",
    CheckStatus.SKIP: "dim",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate VAMiner's local tools, selected Agent runtime, MCP, and optional tracing.",
    )
    parser.add_argument("--runtime", choices=RUNTIME_IDS, default=MINER_AGENT_RUNTIME)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make one real model request and verify its Langfuse observations when tracing is configured.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=min(float(TIMEOUT_SECONDS), 300.0))
    parser.add_argument("--trace-wait-seconds", type=float, default=120.0)
    parser.add_argument("--workspace-dir", type=Path, default=VAS_WORKSPACE_DIR)
    parser.add_argument("--output-dir", type=Path, default=MINER_OUTPUT_DIR)
    parser.add_argument("--rules-dir", type=Path, default=VAS_RULES_DIR)
    parser.add_argument("--claude-command", default=COMMAND)
    parser.add_argument("--claude-model", default=MODEL)
    parser.add_argument("--claude-effort", choices=("low", "medium", "high", "xhigh", "max"), default=None)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    parser.add_argument("--quiet", action="store_true", help="Suppress streamed progress on stderr.")
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0 or args.trace_wait_seconds <= 0:
        parser.error("timeouts must be positive")
    return args


def render_report(report: PreflightReport, *, console: Console | None = None) -> None:
    target = console or Console()
    table = Table(title=f"VAMiner preflight: {report.runtime_id} (live={str(report.live).lower()})")
    table.add_column("Status", width=6)
    table.add_column("Check", no_wrap=True)
    table.add_column("Result")
    table.add_column("Time", justify="right", no_wrap=True)
    for check in report.checks:
        result = check.summary if not check.detail else f"{check.summary}\n[dim]{check.detail}[/dim]"
        elapsed = f"{check.duration_ms} ms" if check.duration_ms is not None else ""
        table.add_row(
            f"[{_STATUS_STYLE[check.status]}]{check.status.value.upper()}[/]",
            check.name,
            result,
            elapsed,
        )
    target.print(table)
    if report.ok:
        target.print("[bold green]READY[/bold green] Selected runtime passed all required preflight checks.")
    else:
        target.print("[bold red]NOT READY[/bold red] Fix failed checks before starting Miner.")


async def main(args: argparse.Namespace) -> PreflightReport:
    progress: ProgressCallback | None = None
    if not args.quiet:
        progress_console = Console(stderr=True)
        progress_started = time.monotonic()

        def render_progress(message: str) -> None:
            elapsed = time.monotonic() - progress_started
            rendered = message
            if message.startswith("PASS "):
                marker = "[green]PASS[/green]"
                rendered = message[5:]
            elif message.startswith("FAIL "):
                marker = "[bold red]FAIL[/bold red]"
                rendered = message[5:]
            elif message.startswith("WARN "):
                marker = "[yellow]WARN[/yellow]"
                rendered = message[5:]
            elif message.startswith("SKIP "):
                marker = "[dim]SKIP[/dim]"
                rendered = message[5:]
            else:
                marker = "[cyan]>[/cyan]"
            progress_console.print(f"[dim]{elapsed:6.1f}s[/dim] {marker} {escape(rendered)}")

        progress = render_progress

    report = await run_preflight(
        PreflightOptions(
            runtime_id=args.runtime,
            workspace_dir=args.workspace_dir,
            output_dir=args.output_dir,
            rules_dir=args.rules_dir,
            live=args.live,
            timeout_seconds=args.timeout_seconds,
            trace_wait_seconds=args.trace_wait_seconds,
        ),
        claude_config=ClaudeCodeConfig(
            executable=args.claude_command,
            model=args.claude_model,
            effort=args.claude_effort,
            default_timeout_seconds=args.timeout_seconds,
        ),
        progress=progress,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        render_report(report)
    return report


def entrypoint(argv: list[str] | None = None) -> int:
    report = asyncio.run(main(parse_args(argv)))
    return 0 if report.ok else 1


__all__ = ["entrypoint", "main", "parse_args", "render_report"]
