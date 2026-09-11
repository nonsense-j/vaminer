"""Host-owned Anchor Plan synthesis and VAS core assembly."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..agent.contracts import (
    AgentRunResult,
    AgentRuntime,
    AgentSession,
    AgentTask,
    RuleGenerationAuthority,
)
from ..anchors.scanner import AnchorQueryError, scan_anchors
from ..models.anchors import (
    Anchor,
    AstGrepExperience,
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
    AnchorSynthesisResult,
    MAX_SYNTHESIS_EXPERIENCES,
    QueryType,
)
from ..models.vas import RuleGenerationDraft, VASCoreInfo
from ..tools.skills import record_ast_grep_experiences
from ..utils.config import MINER_AST_GREP_MAX_PARALLEL_RUNS
from ..utils.log import logger
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
    results: list[AnchorSynthesisResult] = Field(..., min_length=1)


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


class _CallableAgentSession:
    """Compatibility bridge for tests and lightweight host executors."""

    def __init__(
        self,
        task: AgentTask[AnchorSynthesisDelta],
        execute: SynthesisExecutor,
    ) -> None:
        self._task = task
        self._execute = execute

    async def send(self, prompt: str) -> AgentRunResult[AnchorSynthesisDelta]:
        task = self._task if prompt == self._task.prompt else replace(self._task, prompt=prompt)
        return await self._execute(task)

    async def close(self) -> None:
        return None


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
        execute: SynthesisExecutor | None = None,
        runtime: AgentRuntime | None = None,
        max_parallel: int = MINER_AST_GREP_MAX_PARALLEL_RUNS,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.authority = authority
        self.workspace_root = workspace_root
        if runtime is None and execute is not None:
            owner = getattr(execute, "__self__", None)
            if owner is not None and callable(getattr(owner, "open_session", None)):
                runtime = owner
                execute = None
        self._execute = execute
        self._runtime = runtime
        if self._execute is None and self._runtime is None:
            raise ValueError("AnchorSynthesisSession requires execute or runtime")
        if self._execute is not None and self._runtime is not None:
            raise ValueError("AnchorSynthesisSession accepts execute or runtime, not both")
        self._max_parallel = max_parallel
        self._calls = 0
        self._latest: AnchorSynthesisReceipt | None = None

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
        if self._runtime is not None:
            child_session: AgentSession[AnchorSynthesisDelta] = self._runtime.open_session(task)
        else:
            assert self._execute is not None
            child_session = _CallableAgentSession(task, self._execute)
        last_delta: AnchorSynthesisDelta | None = None
        last_errors: tuple[str, ...] = ()
        experiences: list[AstGrepExperience] = []
        experience_keys: set[str] = set()
        try:
            for repair in range(1 + task.limits.output_retries):
                if repair and last_errors:
                    prompt = (
                        "The previous query failed deterministic validation:\n\n- "
                        + "\n- ".join(last_errors)
                        + "\n\nRevise only the query fields for this target anchor.\n"
                    )
                else:
                    prompt = task.prompt
                run = await child_session.send(prompt)
                last_delta = run.output
                anchor = _assemble_anchor(intent, last_delta)
                for experience in last_delta.experiences:
                    if len(experiences) >= MAX_SYNTHESIS_EXPERIENCES:
                        break
                    key = experience.identity
                    if key not in experience_keys:
                        experience_keys.add(key)
                        experiences.append(experience)
                last_errors = _query_errors(anchor, intent, self.authority)
                if not last_errors:
                    result = AnchorSynthesisResult(
                        anchor=anchor,
                        adjustments=last_delta.adjustments,
                        experiences=experiences,
                        plan_suggestion=last_delta.plan_suggestion,
                    )
                    break
            else:
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
                result = AnchorSynthesisResult(
                    anchor=disabled,
                    adjustments=[*last_delta.adjustments, "Disabled after deterministic query validation failed."],
                    experiences=experiences,
                    plan_suggestion=last_delta.plan_suggestion,
                )
        finally:
            await child_session.close()
        if experiences:
            try:
                await asyncio.to_thread(
                    record_ast_grep_experiences,
                    task.authority.skill_root,
                    experiences,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("Could not persist ast-grep synthesis experiences: %s", exc)
        return result

    async def synthesize(self, plan: AnchorPlan) -> list[AnchorSynthesisResult]:
        self._calls += 1
        if self._calls > 2:
            raise AnchorSynthesisLimitError("Rule Generation may invoke Anchor synthesis at most twice")
        normalized_plan = _normalize_anchor_plan(plan)
        errors = validate_anchor_plan(
            normalized_plan,
            self.authority.root_cause.extracted_case_files,
        )
        if errors:
            raise AnchorPlanError("Anchor Plan rejected:\n- " + "\n- ".join(errors))

        semaphore = asyncio.Semaphore(self._max_parallel)

        iteration = self._calls

        async def bounded(intent: AnchorIntent) -> AnchorSynthesisResult:
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
        self._latest = AnchorSynthesisReceipt(plan=normalized_plan, results=results)
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
    "deduplicate_query_anchors",
    "finalize_rule_generation",
    "validate_anchor_plan",
]
