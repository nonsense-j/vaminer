"""Validated output functions for the miner agents."""

from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from ..core.context import MinerContext
from ..core.validation import (
    validate_anchor_synthesis_run,
    validate_issue_checkout,
    validate_root_cause_analysis,
)
from ..utils.models import (
    AnchorSynthesisRunResult,
    IssueCollectionInfo,
    RootCauseAnalysis,
    VASCoreInfo,
)


def _require_path(path: Path | None, label: str) -> Path:
    if path is None:
        raise RuntimeError(f"{label} is missing from agent dependencies")
    return path


def _retry_on_errors(errors: list[str]) -> None:
    if errors:
        raise ModelRetry("Output validation failed:\n- " + "\n- ".join(errors))


def finalize_issue_info(
    ctx: RunContext[MinerContext],
    issue_info: IssueCollectionInfo,
) -> IssueCollectionInfo:
    # Keep model-facing artifact guidance in the Pydantic output schema.
    _retry_on_errors(validate_issue_checkout(issue_info, workspace_root=ctx.deps.workspace_root))
    return issue_info


def finalize_root_cause(
    ctx: RunContext[MinerContext],
    analysis: RootCauseAnalysis,
) -> RootCauseAnalysis:
    # This function is a deterministic output gate, not another source of prompt instructions.
    repo_path = _require_path(ctx.deps.repo_path, "repo_path")
    cases_dir = _require_path(ctx.deps.cases_dir, "cases_dir")
    _retry_on_errors(
        validate_root_cause_analysis(
            analysis,
            repo_path=repo_path,
            cases_dir=cases_dir,
        )
    )
    return analysis


def finalize_anchor_synthesis_run(
    ctx: RunContext[MinerContext],
    result: AnchorSynthesisRunResult,
) -> AnchorSynthesisRunResult:
    """Gate one isolated run before deterministic batch aggregation."""
    repo_path = _require_path(ctx.deps.repo_path, "repo_path")
    cases_dir = _require_path(ctx.deps.cases_dir, "cases_dir")
    root_cause = ctx.deps.root_cause
    if root_cause is None:
        raise RuntimeError("root_cause is missing from agent dependencies")
    errors = validate_anchor_synthesis_run(
        result,
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        run_request=ctx.deps.anchor_synthesis_run_request,
    )
    _retry_on_errors(errors)
    return result


def finalize_vas_core(
    ctx: RunContext[MinerContext],
    vas_core: VASCoreInfo,
) -> VASCoreInfo:
    # Keep the final rule coupled to the exact typed result accepted from the Synthesizer.
    root_cause = ctx.deps.root_cause
    if root_cause is None:
        raise RuntimeError("root_cause is missing from agent dependencies")
    synthesis = ctx.deps.anchor_synthesis
    if synthesis is None:
        raise ModelRetry(
            "No validated anchor synthesis exists. " "Call synthesize_ast_grep_anchors and copy its anchors unchanged."
        )
    if vas_core.anchors != synthesis.anchors:
        raise ModelRetry(
            "Final anchors differ from the validated synthesis. " "Copy each synthesized anchor unchanged."
        )
    synthesis_request = ctx.deps.anchor_synthesis_request
    if synthesis_request is None:
        raise ModelRetry("No anchor synthesis request exists. Call synthesize_ast_grep_anchors first.")
    if vas_core.summary != synthesis_request.summary:
        raise ModelRetry("Final summary differs from the summary used for anchor synthesis. " "Copy it unchanged.")
    if vas_core.language != root_cause.language:
        raise ModelRetry(
            f"Rule language {vas_core.language.value!r} differs from the validated RCA "
            f"language {root_cause.language.value!r}. Copy it unchanged."
        )
    if vas_core.root_cause_summary != root_cause.root_cause_summary:
        raise ModelRetry("Copy root_cause_summary unchanged from the validated RCA.")
    return vas_core
