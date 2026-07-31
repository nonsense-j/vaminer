"""Command-line entry point for the runtime-neutral issue-to-VAS workflow."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from pathlib import Path

from .agent import AgentPhase, RuntimeRouter
from .utils.config import (
    MINER_AGENT_RUNTIME,
    MINER_LOG_DIR,
    MINER_OUTPUT_DIR,
    VAS_RULES_DIR,
    VAS_WORKSPACE_DIR,
)
from .models.vas import VASFull
from .utils.log import logger, run_log_file
from .mining.examples import inspect_example_suite, materialize_example_suite
from .mining.workflow import ExampleSuiteWorkflow, IssueWorkflow, WorkflowOptions
from .runtimes.claude.config import (
    COMMAND as CLAUDE_CODE_COMMAND,
    ClaudeCodeConfig,
    MAX_OUTPUT_BYTES as CLAUDE_CODE_MAX_OUTPUT_BYTES,
    MODEL as CLAUDE_CODE_MODEL,
    TIMEOUT_SECONDS as CLAUDE_CODE_TIMEOUT_SECONDS,
)
from .utils.workspace import Workspace
from .utils.telemetry import flush_tracing, trace_pipeline

RUNTIME_IDS = ("pydantic-ai", "claude-code")
_ISSUE_WORKFLOW_PHASES = (
    AgentPhase.ISSUE_COLLECTION,
    AgentPhase.ROOT_CAUSE,
    AgentPhase.RULE_GENERATION,
)
_EXAMPLE_SUITE_WORKFLOW_PHASES = (
    AgentPhase.ROOT_CAUSE,
    AgentPhase.RULE_GENERATION,
)
_PHASE_ALIASES = {
    "issue": AgentPhase.ISSUE_COLLECTION,
    "issue_collection": AgentPhase.ISSUE_COLLECTION,
    "root_cause": AgentPhase.ROOT_CAUSE,
    "rca": AgentPhase.ROOT_CAUSE,
    "rule": AgentPhase.RULE_GENERATION,
    "rule_generation": AgentPhase.RULE_GENERATION,
}


def _phase_runtime(value: str) -> tuple[AgentPhase, str]:
    """Parse one ``phase=runtime`` override for argparse."""

    phase_text, separator, runtime_id = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("phase runtime must use PHASE=RUNTIME")
    try:
        phase = _PHASE_ALIASES[phase_text.strip().lower().replace("-", "_")]
    except KeyError as exc:
        allowed = ", ".join(sorted(_PHASE_ALIASES))
        raise argparse.ArgumentTypeError(f"unknown phase {phase_text!r}; expected one of: {allowed}") from exc
    runtime_id = runtime_id.strip().lower()
    if runtime_id not in RUNTIME_IDS:
        raise argparse.ArgumentTypeError(
            f"unknown runtime {runtime_id!r}; expected one of: {', '.join(RUNTIME_IDS)}"
        )
    return phase, runtime_id


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
    parser.add_argument("--use-cache", action="store_true", help="Use valid runtime/model-scoped cached outputs.")
    parser.add_argument(
        "--runtime",
        choices=RUNTIME_IDS,
        default=MINER_AGENT_RUNTIME,
        help="Default Agent Runtime for every phase.",
    )
    parser.add_argument(
        "--phase-runtime",
        action="append",
        default=[],
        type=_phase_runtime,
        metavar="PHASE=RUNTIME",
        help="Override one phase Runtime; repeat for multiple phases.",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=VAS_WORKSPACE_DIR,
        help="Root for source registry, checkouts, and generated cases.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=MINER_OUTPUT_DIR,
        help="Root for caches, logs, reviews, and runtime artifacts.",
    )
    parser.add_argument(
        "--rules-dir",
        type=Path,
        default=VAS_RULES_DIR,
        help="Destination for generated VAS rule JSON files.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help=f"Optional workflow log root override; defaults to {MINER_LOG_DIR}.",
    )

    claude = parser.add_argument_group("Claude Code Runtime")
    claude.add_argument("--claude-command", default=CLAUDE_CODE_COMMAND)
    claude.add_argument(
        "--claude-model",
        default=CLAUDE_CODE_MODEL,
        help="Claude model name. Authentication always comes from the Claude CLI user session.",
    )
    claude.add_argument(
        "--claude-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default=None,
    )
    claude.add_argument(
        "--claude-timeout-seconds",
        type=float,
        default=CLAUDE_CODE_TIMEOUT_SECONDS,
    )
    claude.add_argument(
        "--claude-max-output-bytes",
        type=int,
        default=CLAUDE_CODE_MAX_OUTPUT_BYTES,
    )
    args = parser.parse_args(argv)
    args.issue_input = [
        issue
        for item in args.issue_input
        for issue in (part.strip() for part in item.split(","))
        if issue
    ]
    if args.example_suite is not None and args.issue_input:
        parser.error("--example-suite is mutually exclusive with positional issue inputs")
    if args.example_suite is None and not args.issue_input:
        parser.error("provide at least one issue input or --example-suite PATH")
    return args


def make_runtime_router(args: argparse.Namespace) -> RuntimeRouter:
    """Build only the adapters selected by the default and phase routes."""

    phase_runtimes = dict(args.phase_runtime)
    selected = {args.runtime, *phase_runtimes.values()}
    runtimes = {}
    if "pydantic-ai" in selected:
        from .runtimes.pydantic.hooks import make_cli_hooks
        from .runtimes.pydantic.runtime import PydanticAIRuntime
        from .runtimes.pydantic.telemetry import instrument_tracing

        instrument_tracing()
        runtimes["pydantic-ai"] = PydanticAIRuntime(
            additional_capabilities=(make_cli_hooks(),)
        )
    if "claude-code" in selected:
        from .runtimes.claude.runtime import ClaudeCodeRuntime

        runtimes["claude-code"] = ClaudeCodeRuntime(
            ClaudeCodeConfig(
                executable=args.claude_command,
                model=args.claude_model,
                effort=args.claude_effort,
                default_timeout_seconds=args.claude_timeout_seconds,
                max_stdout_bytes=args.claude_max_output_bytes,
            )
        )
    return RuntimeRouter(
        runtimes=runtimes,
        default_runtime=args.runtime,
        phase_runtimes=phase_runtimes,
    )


def _workflow_runtime_ids(
    router: RuntimeRouter,
    phases: Iterable[AgentPhase],
) -> tuple[str, ...]:
    """Return the distinct runtimes used by a workflow in phase order."""

    return tuple(dict.fromkeys(router.runtime_id_for(phase) for phase in phases))


async def run_issue_workflow(
    issue_input: str,
    args: argparse.Namespace,
    *,
    router: RuntimeRouter,
) -> VASFull:
    """Run one complete issue pipeline under a single active trace root."""

    workspace_dir = args.workspace_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    rules_dir = args.rules_dir.expanduser().resolve()
    vas_id = Workspace.get_vas_id(issue_input, base_dir=workspace_dir)
    workflow = IssueWorkflow(
        router,
        options=WorkflowOptions(use_cache=args.use_cache),
    )
    runtime_ids = _workflow_runtime_ids(router, _ISSUE_WORKFLOW_PHASES)
    with trace_pipeline(
        issue_input=issue_input,
        vas_id=vas_id,
        runtime_ids=runtime_ids,
    ) as pipeline_trace:
        workspace = Workspace.from_id(
            vas_id,
            base_dir=workspace_dir,
            input_id=issue_input,
            output_root=output_dir,
            trace_id=pipeline_trace.trace_id,
            rules_dir=rules_dir,
        )
        log_root = (
            args.log_dir.expanduser().resolve()
            if args.log_dir is not None
            else output_dir / "logs" / "miner"
        )
        with run_log_file(
            log_root,
            vas_id,
            input_id=workspace.input_id,
            trace_id=pipeline_trace.trace_id,
            runtime="+".join(runtime_ids),
        ) as log_path:
            logger.info("Starting miner workflow for issue input: %s", issue_input)
            logger.info("Trace ID: %s", pipeline_trace.trace_id)
            logger.info("Storing run log in %s", log_path)
            logger.info("Default runtime: %s", args.runtime)
            try:
                result = await workflow.run(
                    issue_input,
                    vas_id=vas_id,
                    workspace=workspace,
                )
                pipeline_trace.update(output=result.vas.model_dump(mode="json"))
                return result.vas
            except Exception:
                logger.exception("Miner workflow failed for issue input: %s", issue_input)
                raise


async def run_example_suite_workflow(
    example_suite: Path,
    args: argparse.Namespace,
    *,
    router: RuntimeRouter,
) -> VASFull:
    """Run one complete example-suite pipeline under a single trace root."""

    workspace_dir = args.workspace_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    rules_dir = args.rules_dir.expanduser().resolve()
    inspection = inspect_example_suite(example_suite)
    vas_id = Workspace.prepare_example_suite_vas_id(
        inspection.registry_key,
        content_digest=inspection.content_digest,
        base_dir=workspace_dir,
    )
    workflow = ExampleSuiteWorkflow(
        router,
        options=WorkflowOptions(use_cache=args.use_cache),
    )
    runtime_ids = _workflow_runtime_ids(router, _EXAMPLE_SUITE_WORKFLOW_PHASES)
    with trace_pipeline(
        issue_input=inspection.registry_key,
        vas_id=vas_id,
        runtime_ids=runtime_ids,
    ) as pipeline_trace:
        workspace = Workspace.from_id(
            vas_id,
            base_dir=workspace_dir,
            input_id=inspection.registry_key,
            output_root=output_dir,
            trace_id=pipeline_trace.trace_id,
            rules_dir=rules_dir,
        )
        log_root = (
            args.log_dir.expanduser().resolve()
            if args.log_dir is not None
            else output_dir / "logs" / "miner"
        )
        with run_log_file(
            log_root,
            vas_id,
            input_id=workspace.input_id,
            trace_id=pipeline_trace.trace_id,
            runtime="+".join(runtime_ids),
        ) as log_path:
            logger.info("Starting miner workflow for example suite: %s", example_suite)
            logger.info("Trace ID: %s", pipeline_trace.trace_id)
            logger.info("Storing run log in %s", log_path)
            logger.info("Default runtime: %s", args.runtime)
            try:
                intake = materialize_example_suite(inspection, workspace=workspace)
                Workspace.register_example_suite(
                    inspection.registry_key,
                    vas_id=vas_id,
                    content_digest=inspection.content_digest,
                    base_dir=workspace_dir,
                )
                result = await workflow.run(
                    intake,
                    vas_id=vas_id,
                    workspace=workspace,
                )
                pipeline_trace.update(output=result.vas.model_dump(mode="json"))
                return result.vas
            except Exception:
                logger.exception("Miner workflow failed for example suite: %s", example_suite)
                raise


async def main(args: argparse.Namespace) -> VASFull | list[VASFull]:
    try:
        router = make_runtime_router(args)
        if args.example_suite is not None:
            return await run_example_suite_workflow(args.example_suite, args, router=router)
        results = [await run_issue_workflow(issue, args, router=router) for issue in args.issue_input]
        return results[0] if len(results) == 1 else results
    finally:
        flush_tracing()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
