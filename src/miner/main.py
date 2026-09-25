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
    parser = argparse.ArgumentParser(
        description=(
            "Generate reusable VAS rules from 'issue references' or 'source example suites', "
            "or delete previously generated rules and their artifacts. Multiple inputs "
            "are processed sequentially, with one rule generated per input."
        ),
    )
    mining_inputs = parser.add_mutually_exclusive_group()
    mining_inputs.add_argument(
        "--issue",
        dest="issue_input",
        action="append",
        nargs="+",
        metavar="REFERENCE",
        help="CVE ID, GitHub issue URL, or report URL/reference.",
    )
    mining_inputs.add_argument(
        "--example-suite",
        dest="example_suite",
        action="append",
        nargs="+",
        type=Path,
        metavar="DIR",
        help="One or more directories containing related good/bad source examples.",
    )
    parser.add_argument("--use-cache", action="store_true", help="Use valid runtime-scoped cached outputs.")
    parser.add_argument(
        "--delete",
        dest="delete_vas_ids",
        nargs="+",
        metavar="VAS_ID",
        help="Delete one or more VAS registrations and their generated artifacts.",
    )
    args = parser.parse_args(argv)
    args.issue_input = [
        issue
        for item_group in (args.issue_input or [])
        for item in item_group
        for issue in (part.strip() for part in item.split(","))
        if issue
    ]
    if args.example_suite is not None:
        args.example_suite = [suite for suite_group in args.example_suite for suite in suite_group]
    if args.delete_vas_ids is not None:
        if args.example_suite is not None or args.issue_input:
            parser.error("--delete is mutually exclusive with mining inputs")
        args.delete_vas_ids = list(dict.fromkeys(args.delete_vas_ids))
        return args
    if args.example_suite is None and not args.issue_input:
        parser.error("provide --issue REFERENCE, --example-suite DIR, or --delete VAS_ID")
    return args


def make_runtime() -> AgentRuntime:
    runtime_log = RuntimeLog()
    if MINER_AGENT_RUNTIME == "pydanic-sdk":
        from .runtimes.pydantic.hooks import make_cli_hooks
        from .runtimes.pydantic.runtime import PydanticAIRuntime

        return PydanticAIRuntime(hooks=make_cli_hooks(runtime_log=runtime_log))
    from .runtimes.claude.runtime import ClaudeCodeRuntime

    return ClaudeCodeRuntime(
        ClaudeCodeConfig(
            executable=CLAUDE_CODE_COMMAND,
            model=CLAUDE_CODE_MODEL,
            effort=CLAUDE_CODE_EFFORT,
            default_timeout_seconds=CLAUDE_CODE_TIMEOUT_SECONDS,
            max_stdout_bytes=CLAUDE_CODE_MAX_OUTPUT_BYTES,
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
            base_dir=VAS_WORKSPACE_DIR,
            output_root=MINER_OUTPUT_DIR,
            rules_dir=VAS_RULES_DIR,
        )
        deleted = [vas_id for vas_id, removed in results.items() if removed]
        missing = [vas_id for vas_id, removed in results.items() if not removed]
        if deleted:
            print(f"Deleted VAS rules: {', '.join(deleted)}")
        if missing:
            print(f"VAS rules not found: {', '.join(missing)}")
        return None

    runtime = make_runtime()
    miner = VAMiner(
        runtime,
        options=WorkflowOptions(
            use_cache=args.use_cache,
            workspace_dir=VAS_WORKSPACE_DIR,
            output_dir=MINER_OUTPUT_DIR,
            rules_dir=VAS_RULES_DIR,
        ),
    )
    try:
        inputs = (
            [ExampleSuiteInput(path=path) for path in args.example_suite]
            if args.example_suite
            else [IssueInput(reference=reference) for reference in args.issue_input]
        )
        results = [await miner.mine(value) for value in inputs]
        return results[0] if len(results) == 1 else results
    finally:
        flush_tracing()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
