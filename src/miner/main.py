"""Command-line entry point for the single-runtime VAMiner workflow."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .agent import AgentRuntime
from .mining.inputs import ExampleSuiteInput, IssueInput
from .mining.workflow import VAMiner, WorkflowOptions
from .models.vas import VASFull
from .runtimes.claude.config import (
    COMMAND as CLAUDE_CODE_COMMAND,
)
from .runtimes.claude.config import (
    EFFORT as CLAUDE_CODE_EFFORT,
)
from .runtimes.claude.config import (
    MAX_OUTPUT_BYTES as CLAUDE_CODE_MAX_OUTPUT_BYTES,
)
from .runtimes.claude.config import (
    MODEL as CLAUDE_CODE_MODEL,
)
from .runtimes.claude.config import (
    TIMEOUT_SECONDS as CLAUDE_CODE_TIMEOUT_SECONDS,
)
from .runtimes.claude.config import (
    ClaudeCodeConfig,
)
from .utils.config import (
    MINER_AGENT_RUNTIME,
    MINER_OUTPUT_DIR,
    VAS_RULES_DIR,
    VAS_WORKSPACE_DIR,
)
from .utils.log import RuntimeLog
from .utils.telemetry import flush_tracing
from .utils.workspace import Workspace

RUNTIME_IDS = ("pydanic-sdk", "claude-cli")


def confirm_delete(vas_ids: list[str]) -> bool:
    """Ask for one explicit confirmation before deleting VAS artifacts."""

    print("The following VAS rules and all associated artifacts will be deleted:")
    for vas_id in vas_ids:
        print(f"  - {vas_id}")
    try:
        answer = input("Confirm deletion of all listed rules? [y/N]: ")
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "issue_input",
        nargs="*",
        help="Issue input(s), e.g. a CVE ID, GitHub issue URL, or report reference.",
    )
    parser.add_argument(
        "--example-suite",
        type=Path,
        default=None,
        help="Directory of related good/bad examples. Mutually exclusive with issue inputs.",
    )
    parser.add_argument(
        "--delete",
        dest="delete_vas_ids",
        nargs="+",
        metavar="VAS_ID",
        help="Delete one or more VAS registrations and their generated artifacts.",
    )
    parser.add_argument("--use-cache", action="store_true", help="Use valid runtime-scoped cached outputs.")
    parser.add_argument(
        "--runtime",
        choices=RUNTIME_IDS,
        default=MINER_AGENT_RUNTIME,
        help="Single Agent Runtime used for the complete mining run.",
    )
    parser.add_argument("--workspace-dir", type=Path, default=VAS_WORKSPACE_DIR)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=MINER_OUTPUT_DIR,
        help="Root for caches, logs, and reviews.",
    )
    parser.add_argument("--rules-dir", type=Path, default=VAS_RULES_DIR)

    claude = parser.add_argument_group("Claude CLI Runtime")
    claude.add_argument("--claude-command", default=CLAUDE_CODE_COMMAND)
    claude.add_argument(
        "--claude-model",
        default=CLAUDE_CODE_MODEL,
        help="Claude model name; authentication comes from the Claude CLI user session.",
    )
    claude.add_argument(
        "--claude-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default=CLAUDE_CODE_EFFORT,
    )
    claude.add_argument("--claude-timeout-seconds", type=float, default=CLAUDE_CODE_TIMEOUT_SECONDS)
    claude.add_argument("--claude-max-output-bytes", type=int, default=CLAUDE_CODE_MAX_OUTPUT_BYTES)
    args = parser.parse_args(argv)
    args.issue_input = [
        issue
        for item in args.issue_input
        for issue in (part.strip() for part in item.split(","))
        if issue
    ]
    if args.example_suite is not None and args.issue_input:
        parser.error("--example-suite is mutually exclusive with positional issue inputs")
    if args.delete_vas_ids is not None:
        if args.example_suite is not None or args.issue_input:
            parser.error("--delete is mutually exclusive with mining inputs")
        args.delete_vas_ids = list(dict.fromkeys(args.delete_vas_ids))
        return args
    if args.example_suite is None and not args.issue_input:
        parser.error("provide at least one issue input or --example-suite PATH")
    return args


def make_runtime(args: argparse.Namespace) -> AgentRuntime:
    runtime_log = RuntimeLog()
    if args.runtime == "pydanic-sdk":
        from .runtimes.pydantic.hooks import make_cli_hooks
        from .runtimes.pydantic.runtime import PydanticAIRuntime

        return PydanticAIRuntime(hooks=make_cli_hooks(runtime_log=runtime_log))
    from .runtimes.claude.runtime import ClaudeCodeRuntime

    return ClaudeCodeRuntime(
        ClaudeCodeConfig(
            executable=args.claude_command,
            model=args.claude_model,
            effort=args.claude_effort,
            default_timeout_seconds=args.claude_timeout_seconds,
            max_stdout_bytes=args.claude_max_output_bytes,
        ),
        runtime_log=runtime_log,
    )


async def main(args: argparse.Namespace) -> VASFull | list[VASFull] | None:
    if args.delete_vas_ids is not None:
        if not confirm_delete(args.delete_vas_ids):
            print(f"Deletion cancelled: {', '.join(args.delete_vas_ids)}")
            return None
        results = Workspace.delete_vas_many(
            args.delete_vas_ids,
            base_dir=args.workspace_dir,
            output_root=args.output_dir,
            rules_dir=args.rules_dir,
        )
        deleted = [vas_id for vas_id, removed in results.items() if removed]
        missing = [vas_id for vas_id, removed in results.items() if not removed]
        if deleted:
            print(f"Deleted VAS rules: {', '.join(deleted)}")
        if missing:
            print(f"VAS rules not found: {', '.join(missing)}")
        return None

    runtime = make_runtime(args)
    miner = VAMiner(
        runtime,
        options=WorkflowOptions(
            use_cache=args.use_cache,
            workspace_dir=args.workspace_dir,
            output_dir=args.output_dir,
            rules_dir=args.rules_dir,
        ),
    )
    try:
        if args.example_suite is not None:
            return await miner.mine(ExampleSuiteInput(path=args.example_suite))
        results = [await miner.mine(IssueInput(reference=reference)) for reference in args.issue_input]
        return results[0] if len(results) == 1 else results
    finally:
        flush_tracing()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
