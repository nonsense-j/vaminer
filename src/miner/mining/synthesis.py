"""Host-owned Anchor Plan synthesis and VAS core assembly."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..agent.contracts import (
    AgentRuntime,
    AgentSession,
    RuleGenerationAuthority,
)
from ..anchors.scanner import AnchorQueryError, scan_anchors
from ..models.anchors import (
    Anchor,
    AnchorIntent,
    AnchorPlan,
    AnchorPlanRequest,
    AnchorReuse,
    AnchorSynthesisDelta,
    AnchorSynthesisResult,
    QueryType,
)
from ..models.vas import RuleGenerationDraft, VASCoreInfo
from ..tools.errors import ToolInputError
from ..tools.skills import record_ast_grep_experiences
from ..utils.config import MINER_AST_GREP_MAX_PARALLEL_RUNS
from ..utils.log import logger
from ..utils.workspace import atomic_write_json
from .tasks import make_ast_grep_synthesis_task

_CASE_NAME = re.compile(r"^case(?P<number>\d+)(?:_var(?P<variant>\d+))?(?P<suffix>\.[A-Za-z0-9]+)$")


class AnchorSynthesisError(RuntimeError):
    """Base failure raised by the host-owned synthesis Module."""


class AnchorPlanError(AnchorSynthesisError, ToolInputError):
    pass


class AnchorSynthesisAcceptanceError(AnchorSynthesisError):
    pass


class AnchorSynthesisReceipt(BaseModel):
    """Latest accepted, ordered batch retained by one Rule Generation run."""

    model_config = ConfigDict(extra="forbid")

    plan: AnchorPlan
    results: list[AnchorSynthesisResult] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_canonical_results(self) -> "AnchorSynthesisReceipt":
        if [item.anchor.id for item in self.results] != [intent.id for intent in self.plan.intents]:
            raise ValueError("synthesis results must match the complete plan in order")
        for intent, result in zip(self.plan.intents, self.results, strict=True):
            anchor = result.anchor
            if (
                anchor.behavior != intent.behavior
                or anchor.inspect_hint != intent.inspect_hint
                or anchor.behavior_weight != intent.behavior_weight
            ):
                raise ValueError(f"synthesis result drifted from intent {intent.id!r}")
        return self


def validate_anchor_plan(plan: AnchorPlan, declared_cases: Sequence[str]) -> tuple[str, ...]:
    """Validate Case Artifact references and collective plan coverage."""

    declared = set(declared_cases)
    errors: list[str] = []
    assigned: set[str] = set()
    for intent in plan.intents:
        required = set(intent.required_cases)
        assigned.update(required)
        invalid = sorted(name for name in required if Path(name).name != name or _CASE_NAME.fullmatch(name) is None)
        if invalid:
            errors.append(
                f"intent {intent.id!r} has invalid Case Artifact names: {', '.join(invalid)}"
            )
        unknown = sorted(required - declared)
        if unknown:
            errors.append(f"intent {intent.id!r} references unknown Case Artifacts: {', '.join(unknown)}")
    unassigned = sorted(declared - assigned)
    if unassigned:
        errors.append(
            "Anchor Plan does not assign every declared Case Artifact to an intent: "
            + ", ".join(unassigned)
        )
    return tuple(errors)


def _normalize_anchor_plan(plan: AnchorPlan) -> AnchorPlan:
    """Return a plan with duplicate per-intent Case Artifact references removed."""
    intents = [
        intent.model_copy(
            update={"required_cases": list(dict.fromkeys(intent.required_cases))}
        )
        for intent in plan.intents
    ]
    return plan.model_copy(update={"intents": intents})


def _assemble_anchor(intent: AnchorIntent, delta: AnchorSynthesisDelta) -> Anchor:
    if delta.anchor_id != intent.id:
        raise AnchorSynthesisAcceptanceError(
            f"synthesis output targets {delta.anchor_id!r}; expected {intent.id!r}"
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


def _normalized_query(query: str) -> str:
    """Normalize only line endings and outer whitespace for deduplication."""

    return query.replace("\r\n", "\n").strip()


def deduplicate_query_anchors(
    results: Sequence[AnchorSynthesisResult],
    *,
    language: str,
) -> list[Anchor]:
    """Keep the strongest representative for each executable query.

    Empty queries intentionally remain one-for-one with their disabled anchors. For
    enabled queries, internal whitespace is left untouched and the plan order is the
    final tie-breaker after query and behavior weights.
    """

    selected: dict[tuple[str, str, str], tuple[int, AnchorSynthesisResult]] = {}
    disabled: list[tuple[int, AnchorSynthesisResult]] = []
    for position, result in enumerate(results):
        anchor = result.anchor
        if not anchor.query.strip():
            disabled.append((position, result))
            continue
        key = (language, anchor.query_type.value, _normalized_query(anchor.query))
        previous = selected.get(key)
        if previous is None:
            selected[key] = (position, result)
            continue
        previous_position, previous_result = previous
        previous_anchor = previous_result.anchor
        candidate_score = (anchor.query_weight, anchor.behavior_weight, -position)
        previous_score = (
            previous_anchor.query_weight,
            previous_anchor.behavior_weight,
            -previous_position,
        )
        if candidate_score > previous_score:
            selected[key] = (position, result)

    kept = [*disabled, *selected.values()]
    kept.sort(key=lambda item: item[0])
    return [result.anchor for _, result in kept]


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
    component_files = {
        Path(component.file).as_posix().removeprefix("./")
        for component in authority.root_cause.buggy_components
    }
    grounded = any(
        Path(match.file).as_posix().removeprefix("./") in component_files
        for match in source_scan.matches
    )
    if not grounded:
        errors.append("query does not match any RCA-declared source file")
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
        anchors=deduplicate_query_anchors(
            receipt.results,
            language=authority.root_cause.language.value,
        ),
    )


class AnchorSynthesisSession:
    """Deep Module that owns plan acceptance, child execution, and canonical assembly."""

    def __init__(
        self,
        authority: RuleGenerationAuthority,
        *,
        workspace_root: Path,
        runtime: AgentRuntime,
        max_parallel: int = MINER_AST_GREP_MAX_PARALLEL_RUNS,
        initial_receipt: AnchorSynthesisReceipt | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.authority = authority
        self.workspace_root = workspace_root
        self._runtime = runtime
        self._max_parallel = max_parallel
        self._calls = 0
        self._latest = initial_receipt

    @property
    def receipt(self) -> AnchorSynthesisReceipt | None:
        return self._latest

    async def _synthesize_one(
        self,
        plan: AnchorPlan,
        intent: AnchorIntent,
        *,
        iteration: int,
    ) -> AnchorSynthesisResult:
        task = make_ast_grep_synthesis_task(
            plan,
            intent,
            workspace_root=self.workspace_root,
            source_root=self.authority.source_root,
            cases_dir=self.authority.cases_dir,
            grounding_policy=self.authority.grounding_policy,
            root_cause=self.authority.root_cause,
            iteration=iteration,
        )
        child_session: AgentSession[AnchorSynthesisDelta] = self._runtime.open_session(task)
        last_errors: tuple[str, ...] = ()
        synthesizer_turns: int | None = None
        observed_turns = 0
        max_turns = task.limits.request_limit
        try:
            while True:
                if max_turns is not None and observed_turns >= max_turns:
                    raise AnchorSynthesisError(
                        f"{task.task_id} exhausted its model request limit of {max_turns}:\n- "
                        + "\n- ".join(last_errors)
                    )
                if last_errors:
                    prompt = (
                        "The previous query failed deterministic validation:\n\n- "
                        + "\n- ".join(last_errors)
                        + "\n\nRevise only the query fields for this target anchor.\n"
                    )
                else:
                    prompt = task.prompt
                run = await child_session.send(prompt)
                last_delta = run.output
                # Runtime sessions report cumulative usage across resumed sends.
                if run.usage is not None and run.usage.turns is not None:
                    synthesizer_turns = run.usage.turns
                observed_turns = max(observed_turns + 1, synthesizer_turns or 0)
                anchor = _assemble_anchor(intent, last_delta)
                last_errors = _query_errors(anchor, intent, self.authority)
                if not last_errors:
                    result = AnchorSynthesisResult(
                        anchor=anchor,
                        adjustments=last_delta.adjustments,
                        experiences=last_delta.experiences,
                        plan_suggestion=last_delta.plan_suggestion,
                    )
                    break
        finally:
            await child_session.close()
        if (
            result.experiences
            and max_turns is not None
            and synthesizer_turns is not None
            and synthesizer_turns * 2 < max_turns
        ):
            result = result.model_copy(update={"experiences": []})
        if result.experiences:
            try:
                await asyncio.to_thread(
                    record_ast_grep_experiences,
                    task.authority.skill_root,
                    result.experiences,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("Could not persist ast-grep synthesis experiences: %s", exc)
        return result

    def _resolve_plan(
        self, request: AnchorPlanRequest | AnchorPlan,
    ) -> tuple[AnchorPlan, dict[str, AnchorSynthesisResult]]:
        previous = {
            intent.id: (intent, result)
            for intent, result in zip(self._latest.plan.intents, self._latest.results, strict=True)
        } if self._latest is not None else {}
        intents: list[AnchorIntent] = []
        reused: dict[str, AnchorSynthesisResult] = {}
        for entry in request.intents:
            if isinstance(entry, AnchorReuse):
                if entry.reuse_anchor_id not in previous:
                    raise AnchorPlanError(
                        f"cannot reuse unknown anchor {entry.reuse_anchor_id!r}; "
                        "reference an anchor from the latest successful batch or submit a complete intent"
                    )
                intent, result = previous[entry.reuse_anchor_id]
                intents.append(intent.model_copy(deep=True))
                reused[intent.id] = result.model_copy(deep=True)
            else:
                intents.append(entry)
        return _normalize_anchor_plan(AnchorPlan(summary=request.summary, intents=intents)), reused

    async def synthesize(self, plan: AnchorPlanRequest | AnchorPlan) -> list[AnchorSynthesisResult]:
        normalized_plan, reused = self._resolve_plan(plan)
        errors = validate_anchor_plan(
            normalized_plan,
            self.authority.root_cause.extracted_case_files,
        )
        if errors:
            raise AnchorPlanError("Anchor Plan rejected:\n- " + "\n- ".join(errors))

        for intent in normalized_plan.intents:
            if intent.id in reused:
                errors = _query_errors(reused[intent.id].anchor, intent, self.authority)
                if errors:
                    raise AnchorPlanError(
                        f"anchor {intent.id!r} cannot be reused; submit a complete intent to synthesize it again:\n- "
                        + "\n- ".join(errors)
                    )

        semaphore = asyncio.Semaphore(self._max_parallel)

        self._calls += 1
        iteration = self._calls

        async def bounded(intent: AnchorIntent) -> AnchorSynthesisResult:
            if intent.id in reused:
                return reused[intent.id]
            async with semaphore:
                return await self._synthesize_one(
                    normalized_plan,
                    intent,
                    iteration=iteration,
                )

        tasks = [asyncio.create_task(bounded(intent)) for intent in normalized_plan.intents]
        try:
            results = list(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        receipt = AnchorSynthesisReceipt(plan=normalized_plan, results=results)
        if self.authority.synthesis_cache_path is not None:
            atomic_write_json(self.authority.synthesis_cache_path, receipt.model_dump(mode="json", by_alias=True))
        self._latest = receipt
        return results

    def finalize(self, draft: RuleGenerationDraft) -> VASCoreInfo:
        return finalize_rule_generation(self.authority, draft, self._latest)


__all__ = [
    "AnchorPlanError",
    "AnchorSynthesisAcceptanceError",
    "AnchorSynthesisError",
    "AnchorSynthesisReceipt",
    "AnchorSynthesisSession",
    "deduplicate_query_anchors",
    "finalize_rule_generation",
    "validate_anchor_plan",
]
