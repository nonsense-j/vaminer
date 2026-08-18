"""Host-owned Anchor Plan synthesis and VAS core assembly."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..agent.contracts import AgentRunResult, AgentTask, RuleGenerationAuthority
from ..anchors.scanner import AnchorQueryError, scan_anchors
from ..models.analysis import GroundingPolicy
from ..models.anchors import (
    Anchor,
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
    AnchorSynthesisResult,
    QueryType,
)
from ..models.vas import RuleGenerationDraft, VASCoreInfo
from ..utils.config import MINER_AST_GREP_MAX_PARALLEL_RUNS
from .tasks import make_ast_grep_synthesis_task

SynthesisExecutor = Callable[
    [AgentTask[AnchorSynthesisDelta]],
    Awaitable[AgentRunResult[AnchorSynthesisDelta]],
]
_CASE_NAME = re.compile(r"^case(?P<number>\d+)(?:_var(?P<variant>\d+))?(?P<suffix>\.[A-Za-z0-9]+)$")


class AnchorSynthesisError(RuntimeError):
    """Base failure raised by the host-owned synthesis Module."""


class AnchorPlanError(AnchorSynthesisError):
    pass


class AnchorSynthesisLimitError(AnchorSynthesisError):
    pass


class AnchorSynthesisAcceptanceError(AnchorSynthesisError):
    pass


class AnchorSynthesisReceipt(BaseModel):
    """Latest accepted, ordered batch retained by one Rule Generation run."""

    model_config = ConfigDict(extra="forbid")

    plan: AnchorPlan
    results: list[AnchorSynthesisResult] = Field(..., min_length=1, max_length=8)


def validate_anchor_plan(plan: AnchorPlan, declared_cases: Sequence[str]) -> tuple[str, ...]:
    """Validate plan ownership and Case Artifact coverage before any child runs."""

    declared = set(declared_cases)
    errors: list[str] = []
    assigned: set[str] = set()
    for intent in plan.intents:
        required = set(intent.required_cases)
        if len(required) != len(intent.required_cases):
            errors.append(f"intent {intent.id!r} repeats a required Case Artifact")
        invalid = sorted(name for name in required if Path(name).name != name or _CASE_NAME.fullmatch(name) is None)
        if invalid:
            errors.append(
                f"intent {intent.id!r} has invalid Case Artifact names: {', '.join(invalid)}"
            )
        unknown = sorted(required - declared)
        if unknown:
            errors.append(f"intent {intent.id!r} references unknown Case Artifacts: {', '.join(unknown)}")
        assigned.update(required)
        for name in sorted(required):
            match = _CASE_NAME.fullmatch(name)
            if match is None or match.group("variant") is None:
                continue
            original = f"case{match.group('number')}{match.group('suffix')}"
            if original not in required:
                errors.append(f"intent {intent.id!r} includes {name!r} without its original {original!r}")
    missing = sorted(declared - assigned)
    if missing:
        errors.append("Anchor Plan does not assign declared Case Artifacts: " + ", ".join(missing))
    return tuple(errors)


def _assemble_anchor(intent: AnchorIntent, delta: AnchorSynthesisDelta) -> Anchor:
    if delta.target_anchor_id != intent.id:
        raise AnchorSynthesisAcceptanceError(
            f"synthesis output targets {delta.target_anchor_id!r}; expected {intent.id!r}"
        )
    if delta.query_weight > intent.behavior_weight:
        raise AnchorSynthesisAcceptanceError(
            f"query_weight for {intent.id!r} exceeds canonical behavior_weight"
        )
    return Anchor(
        id=intent.id,
        behavior_weight=intent.behavior_weight,
        query_weight=delta.query_weight,
        type=delta.query_type,
        query=delta.query,
        behavior=intent.behavior,
        inspect_hint=intent.inspect_hint,
    )


def _query_errors(
    anchor: Anchor,
    intent: AnchorIntent,
    authority: RuleGenerationAuthority,
) -> tuple[str, ...]:
    if not anchor.query.strip():
        return ()
    payload = [anchor.model_dump(mode="json", by_alias=True)]
    try:
        case_scan = scan_anchors(payload, authority.cases_dir, authority.root_cause.language.value)
        source_scan = scan_anchors(payload, authority.source_root, authority.root_cause.language.value)
    except AnchorQueryError as exc:
        return (f"ast-grep validation failed: {exc}",)
    matched_cases = {match.file for match in case_scan.matches}
    missing = sorted({Path(item).name for item in intent.required_cases} - matched_cases)
    errors: list[str] = []
    if missing:
        errors.append("query misses required Case Artifacts: " + ", ".join(missing))
    spans = authority.root_cause.buggy_components
    grounded = any(
        Path(match.file).as_posix().removeprefix("./")
        == Path(span.file).as_posix().removeprefix("./")
        and match.start_line <= span.end_line
        and match.end_line >= span.start_line
        for match in source_scan.matches
        for span in spans
    )
    if not grounded:
        label = (
            "RCA-declared bad span"
            if authority.grounding_policy is GroundingPolicy.BAD_SPAN_COVERAGE
            else "RCA-declared repository span"
        )
        errors.append(f"query does not overlap any {label}")
    return tuple(errors)


def finalize_rule_generation(
    authority: RuleGenerationAuthority,
    draft: RuleGenerationDraft,
    receipt: AnchorSynthesisReceipt | None,
) -> VASCoreInfo:
    """Assemble immutable fields before the Phase Definition accepts the result."""

    if receipt is None:
        raise AnchorSynthesisAcceptanceError("Rule Generation completed without an accepted Anchor Plan")
    return VASCoreInfo(
        category=draft.category,
        language=authority.root_cause.language,
        root_cause_summary=authority.root_cause.root_cause_summary,
        summary=receipt.plan.summary,
        scenarios=draft.scenarios,
        anchors=[item.anchor for item in receipt.results],
    )


class AnchorSynthesisSession:
    """Deep Module that owns plan limits, child execution, and canonical assembly."""

    def __init__(
        self,
        authority: RuleGenerationAuthority,
        *,
        workspace_root: Path,
        execute: SynthesisExecutor,
        max_parallel: int = MINER_AST_GREP_MAX_PARALLEL_RUNS,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.authority = authority
        self.workspace_root = workspace_root
        self._execute = execute
        self._max_parallel = max_parallel
        self._calls = 0
        self._latest: AnchorSynthesisReceipt | None = None

    @property
    def receipt(self) -> AnchorSynthesisReceipt | None:
        return self._latest

    async def _synthesize_one(self, plan: AnchorPlan, intent: AnchorIntent) -> AnchorSynthesisResult:
        task = make_ast_grep_synthesis_task(
            plan,
            intent,
            workspace_root=self.workspace_root,
            source_root=self.authority.source_root,
            cases_dir=self.authority.cases_dir,
            grounding_policy=self.authority.grounding_policy,
            root_cause=self.authority.root_cause,
        )
        last_delta: AnchorSynthesisDelta | None = None
        last_errors: tuple[str, ...] = ()
        for repair in range(1 + task.limits.output_retries):
            repair_task = task
            if repair and last_errors:
                repair_task = replace(
                    task,
                    prompt=(
                        task.prompt
                        + "\n\nDeterministic query validation failed. Return a corrected delta only:\n- "
                        + "\n- ".join(last_errors)
                    ),
                )
            run = await self._execute(repair_task)
            last_delta = run.output
            anchor = _assemble_anchor(intent, last_delta)
            last_errors = _query_errors(anchor, intent, self.authority)
            if not last_errors:
                return AnchorSynthesisResult(
                    anchor=anchor,
                    adjustments=last_delta.adjustments,
                    plan_suggestion=last_delta.plan_suggestion,
                )
        assert last_delta is not None
        disabled = Anchor(
            id=intent.id,
            behavior_weight=intent.behavior_weight,
            query_weight=min(last_delta.query_weight, intent.behavior_weight),
            type=last_delta.query_type if last_delta else QueryType.PATTERN,
            query="",
            behavior=intent.behavior,
            inspect_hint=intent.inspect_hint,
        )
        return AnchorSynthesisResult(
            anchor=disabled,
            adjustments=[*last_delta.adjustments, "Disabled after deterministic query validation failed."],
            plan_suggestion=last_delta.plan_suggestion,
        )

    async def synthesize(self, plan: AnchorPlan) -> list[AnchorSynthesisResult]:
        self._calls += 1
        if self._calls > 2:
            raise AnchorSynthesisLimitError("Rule Generation may invoke Anchor synthesis at most twice")
        errors = validate_anchor_plan(plan, self.authority.root_cause.extracted_case_files)
        if errors:
            raise AnchorPlanError("Anchor Plan rejected:\n- " + "\n- ".join(errors))

        semaphore = asyncio.Semaphore(self._max_parallel)

        async def bounded(intent: AnchorIntent) -> AnchorSynthesisResult:
            async with semaphore:
                return await self._synthesize_one(plan, intent)

        results = list(await asyncio.gather(*(bounded(intent) for intent in plan.intents)))
        self._latest = AnchorSynthesisReceipt(plan=plan, results=results)
        return results

    def finalize(self, draft: RuleGenerationDraft) -> VASCoreInfo:
        return finalize_rule_generation(self.authority, draft, self._latest)


__all__ = [
    "AnchorPlanError",
    "AnchorSynthesisAcceptanceError",
    "AnchorSynthesisError",
    "AnchorSynthesisLimitError",
    "AnchorSynthesisReceipt",
    "AnchorSynthesisSession",
    "SynthesisExecutor",
    "finalize_rule_generation",
    "validate_anchor_plan",
]
