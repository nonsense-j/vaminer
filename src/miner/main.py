"""Command-line entry point for the runtime-neutral issue-to-VAS workflow."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .configs import (
    CLAUDE_CODE_COMMAND,
    CLAUDE_CODE_MAX_BUDGET_USD,
    CLAUDE_CODE_MAX_OUTPUT_BYTES,
    CLAUDE_CODE_MODEL,
    CLAUDE_CODE_SETTING_SOURCES,
    CLAUDE_CODE_SETTINGS,
    CLAUDE_CODE_TIMEOUT_SECONDS,
    MINER_AGENT_RUNTIME,
    MINER_LOG_DIR,
    VAS_RULES_DIR,
    VAS_WORKSPACE_DIR,
    flush_tracing,
    trace_pipeline,
)
from .core.example_suite_intake import inspect_example_suite, materialize_example_suite
from .core.workflow import ExampleSuiteWorkflow, IssueWorkflow, WorkflowOptions
from .runtime import AgentPhase, RuntimeRouter
from .runtime.claude_code import ClaudeCodeConfig, ClaudeCodeRuntime
from .runtime.pydantic_ai import PydanticAIRuntime
from .utils.hooks import make_cli_hooks
from .utils.logger import logger, run_log_file
from .utils.models import VASFull
from .utils.workspace import Workspace

RUNTIME_IDS = ("pydantic-ai", "claude-code")
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


def _setting_sources(value: str) -> tuple[str, ...]:
    """Parse the intentionally narrow Claude settings-source policy."""

    normalized = value.strip().lower()
    if normalized in {"", "none"}:
        return ()
    sources = tuple(item.strip() for item in normalized.split(",") if item.strip())
    if set(sources) - {"user"}:
        raise argparse.ArgumentTypeError("Claude Runtime supports only 'user' or 'none' setting sources")
    return sources


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
        help="Root for source registry, checkouts, cases, caches, and run artifacts.",
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
        default=MINER_LOG_DIR,
        help="Root for append-only workflow logs.",
    )

    claude = parser.add_argument_group("Claude Code Runtime")
    claude.add_argument("--claude-command", default=CLAUDE_CODE_COMMAND)
    claude.add_argument("--claude-model", default=CLAUDE_CODE_MODEL)
    claude.add_argument(
        "--claude-settings",
        default=CLAUDE_CODE_SETTINGS,
        help="Optional auth/model settings JSON; execution customizations are stripped.",
    )
    claude.add_argument(
        "--claude-setting-sources",
        type=_setting_sources,
        default=_setting_sources(CLAUDE_CODE_SETTING_SOURCES),
        metavar="user|none",
        help="Load only user settings for auth/provider configuration, or none for environment-only auth.",
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
        "--claude-max-budget-usd",
        type=float,
        default=CLAUDE_CODE_MAX_BUDGET_USD,
        help="Optional native Claude safety cap per CLI attempt; request_limit remains the primary agent bound.",
    )
    claude.add_argument(
        "--claude-max-output-bytes",
        type=int,
        default=CLAUDE_CODE_MAX_OUTPUT_BYTES,
    )
    claude.add_argument(
        "--claude-bare",
        action="store_true",
        help="Use Claude --bare for API-key or apiKeyHelper authentication instead of OAuth-compatible isolation.",
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
    """Build both adapters without initializing either model provider."""

    phase_runtimes = dict(args.phase_runtime)
    claude_settings = Path(args.claude_settings) if args.claude_settings else None
    runtimes = {
        "pydantic-ai": PydanticAIRuntime(additional_capabilities=(make_cli_hooks(),)),
        "claude-code": ClaudeCodeRuntime(
            ClaudeCodeConfig(
                executable=args.claude_command,
                model=args.claude_model,
                effort=args.claude_effort,
                setting_sources=tuple(args.claude_setting_sources),
                settings_file=claude_settings,
                max_budget_usd=args.claude_max_budget_usd,
                default_timeout_seconds=args.claude_timeout_seconds,
                max_stdout_bytes=args.claude_max_output_bytes,
                bare=args.claude_bare,
            )
        ),
    }
    return RuntimeRouter(
        runtimes=runtimes,
        default_runtime=args.runtime,
        phase_runtimes=phase_runtimes,
    )


async def run_issue_workflow(
    issue_input: str,
    args: argparse.Namespace,
    *,
    router: RuntimeRouter,
) -> VASFull:
    """Run one complete issue pipeline under a single active trace root."""

    workspace_dir = args.workspace_dir.expanduser().resolve()
    rules_dir = args.rules_dir.expanduser().resolve()
    vas_id = Workspace.get_vas_id(issue_input, base_dir=workspace_dir)
    workspace = Workspace.from_id(
        vas_id,
        base_dir=workspace_dir,
        rules_dir=rules_dir,
    )
    workflow = IssueWorkflow(
        router,
        options=WorkflowOptions(use_cache=args.use_cache),
    )
    with run_log_file(args.log_dir.expanduser().resolve(), vas_id) as log_path:
        logger.info("Starting miner workflow for issue input: %s", issue_input)
        logger.info("Storing run log in %s", log_path)
        logger.info("Default runtime: %s", args.runtime)
        try:
            with trace_pipeline(issue_input=issue_input, vas_id=vas_id) as pipeline_span:
                result = await workflow.run(
                    issue_input,
                    vas_id=vas_id,
                    workspace=workspace,
                )
                if pipeline_span is not None:
                    pipeline_span.update(output=result.vas.model_dump(mode="json"))
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
    rules_dir = args.rules_dir.expanduser().resolve()
    inspection = inspect_example_suite(example_suite)
    vas_id = Workspace.prepare_example_suite_vas_id(
        inspection.registry_key,
        content_digest=inspection.content_digest,
        base_dir=workspace_dir,
    )
    workspace = Workspace.from_id(
        vas_id,
        base_dir=workspace_dir,
        rules_dir=rules_dir,
    )
    intake = materialize_example_suite(inspection, workspace=workspace)
    Workspace.register_example_suite(
        inspection.registry_key,
        vas_id=vas_id,
        content_digest=inspection.content_digest,
        base_dir=workspace_dir,
    )
    workflow = ExampleSuiteWorkflow(
        router,
        options=WorkflowOptions(use_cache=args.use_cache),
    )
    with run_log_file(args.log_dir.expanduser().resolve(), vas_id) as log_path:
        logger.info("Starting miner workflow for example suite: %s", example_suite)
        logger.info("Storing run log in %s", log_path)
        logger.info("Default runtime: %s", args.runtime)
        try:
            with trace_pipeline(issue_input=inspection.registry_key, vas_id=vas_id) as pipeline_span:
                result = await workflow.run(
                    intake,
                    vas_id=vas_id,
                    workspace=workspace,
                )
                if pipeline_span is not None:
                    pipeline_span.update(output=result.vas.model_dump(mode="json"))
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
