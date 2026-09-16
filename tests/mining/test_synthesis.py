"""Behavior tests for anchor synthesis planning and execution."""

import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.miner.agent import (
    AgentRunResult,
    RuleGenerationAuthority,
    RunLimits,
    RuntimeIdentity,
    RuntimeUsage,
    TurnBudgetExceeded,
)
from src.miner.anchors.scanner import AnchorExecutionError, AnchorQueryError
from src.miner.mining import synthesis as synthesis_module
from src.miner.mining.synthesis import (
    AnchorPlanError,
    AnchorSynthesisSession,
    deduplicate_query_anchors,
)
from src.miner.mining.tasks import make_ast_grep_synthesis_task
from src.miner.mining.validation.vas import validate_vas_core
from src.miner.models import (
    Anchor,
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
    AnchorSynthesisResult,
    AstGrepExperience,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCategory,
    QueryType,
    RootCauseAnalysis,
    RuleGenerationDraft,
    Scenarios,
)


class ScriptedRuntime:
    """Run each test script in a session with cumulative turn usage."""

    def __init__(self, respond):
        self.respond = respond

    def open_session(self, task):
        respond = self.respond

        class Session:
            turns = 0

            async def send(self, prompt, *, request_limit_extension=0):
                result = await respond(replace(task, prompt=prompt))
                if result.usage is not None and result.usage.turns is not None:
                    self.turns += result.usage.turns
                    result = replace(result, usage=replace(result.usage, turns=self.turns))
                return result

            async def close(self):
                pass

        return Session()


def _rca() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        language=AstGrepLanguage.C,
        root_cause_summary="unchecked copy",
        analysis="length reaches copy",
        buggy_components=[BuggyComponent(file="bug.c", start_line=1, end_line=1, role="copy", snippet="copy();")],
        fixing_pattern="bound length",
        extracted_case_files=["case1.c", "case1_var1.c"],
    )


def _plan(summary: str = "detect unchecked copies") -> AnchorPlan:
    return AnchorPlan(
        summary=summary,
        intents=[
            AnchorIntent(
                id="copy-site",
                behavior_weight=4,
                behavior="copy a runtime length",
                inspect_hint="inspect the bound",
                required_cases=["case1.c", "case1_var1.c"],
            )
        ],
    )


@pytest.mark.asyncio
async def test_session_owns_immutable_fields_and_accepts_repeated_replans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1_var1.c").write_text("copy();\n", encoding="utf-8")
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())

    task_labels: list[tuple[str, str]] = []

    async def execute(task):
        task_labels.append((task.task_id, task.agent_name))
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type=QueryType.PATTERN,
                query="copy($A)",
                query_weight=3,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake-model"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    first = await session.synthesize(_plan("first summary"))
    second = await session.synthesize(_plan("second summary"))
    assert first[0].anchor.behavior == "copy a runtime length"
    assert second[0].anchor.inspect_hint == "inspect the bound"
    assert task_labels == [
        ("ast-grep-synthesis:1:copy-site", "AST-Grep Synthesizer [1.1/1]"),
        ("ast-grep-synthesis:2:copy-site", "AST-Grep Synthesizer [2.1/1]"),
    ]
    core = session.finalize(
        RuleGenerationDraft(
            category=IssueCategory.SECURITY,
            scenarios=Scenarios(unsafe=["unbounded copy"], safe=["bounded copy"]),
        )
    )
    assert core.summary == "second summary"
    assert core.language is AstGrepLanguage.C
    assert core.root_cause_summary == _rca().root_cause_summary
    wrong_language = core.model_copy(update={"language": AstGrepLanguage.CPP})
    language_errors = validate_vas_core(
        wrong_language,
        source_root=source,
        cases_dir=cases,
        root_cause=_rca(),
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    assert [error for error in language_errors if "language" in error] == [
        "rule language cpp does not match RCA language c"
    ]
    for summary in ("third", "fourth", "fifth"):
        invalid = _plan().model_copy(update={"intents": [
            _plan().intents[0].model_copy(update={"required_cases": ["unknown.c"]}),
        ]})
        before = session.receipt
        with pytest.raises(AnchorPlanError):
            await session.synthesize(invalid)
        assert session.receipt is before
        await session.synthesize(_plan(summary))
        assert session.receipt.plan.summary == summary
    assert len(task_labels) == 5
    assert task_labels[-1][0] == "ast-grep-synthesis:5:copy-site"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft_query",
    [
        "copy($A)",
        "any:\n  - pattern: copy_one($A)\n  - pattern: copy_two($A)\n",
        "any:\n  - pattern: [unfinished YAML",
    ],
)
async def test_replan_passes_merged_query_draft_to_only_its_synthesizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    draft_query: str,
):
    authority = RuleGenerationAuthority(
        tmp_path / "src", tmp_path / "cases", GroundingPolicy.REPOSITORY_EVIDENCE, _rca()
    )
    tasks = {}
    validated_queries = []

    async def execute(task):
        anchor_id = task.authority.anchor_id
        tasks[anchor_id] = task
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=anchor_id,
                type="pattern",
                query=f"{anchor_id.replace('-', '_')}($A)",
                query_weight=2,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    def validate_output(anchor, _intent, _authority):
        validated_queries.append(anchor.query)
        return ()

    monkeypatch.setattr(synthesis_module, "_query_errors", validate_output)
    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    original = AnchorPlan(
        summary="copy sites",
        intents=[
            _plan().intents[0].model_copy(update={"id": "copy-one", "required_cases": ["case1.c"]}),
            _plan().intents[0].model_copy(update={"id": "copy-two", "required_cases": ["case1_var1.c"]}),
        ],
    )
    await session.synthesize(original)
    assert all("[Draft Query]" not in task.prompt for task in tasks.values())
    tasks.clear()
    validated_queries.clear()

    # Exercise the tool's plan input shape, including a merged anchor with a new ID.
    intent_fields = _plan().intents[0].model_dump(exclude={"id", "draft_query"})
    revised = AnchorPlan.model_validate(
        {
            "summary": "revised copy sites",
            "intents": [
                {**intent_fields, "id": "merged-copy", "draft_query": draft_query},
                {**intent_fields, "id": "new-site", "draft_query": None},
            ],
        }
    )
    results = await session.synthesize(revised)

    assert draft_query in tasks["merged-copy"].prompt
    assert "[Draft Query]" in tasks["merged-copy"].prompt
    assert "Start from this unvalidated draft" in tasks["merged-copy"].prompt
    assert "[Draft Query]" not in tasks["new-site"].prompt
    assert draft_query not in tasks["new-site"].prompt
    assert [result.anchor.id for result in results] == ["merged-copy", "new-site"]
    assert sorted(validated_queries) == ["merged_copy($A)", "new_site($A)"]
    assert session.receipt is not None
    assert session.receipt.plan.intents[0].draft_query == draft_query

    core = session.finalize(
        RuleGenerationDraft(
            category=IssueCategory.SECURITY,
            scenarios=Scenarios(unsafe=["unbounded copy"], safe=["bounded copy"]),
        )
    )
    assert core.anchors[0].query == "merged_copy($A)"
    assert all("draft_query" not in anchor.model_dump() for anchor in core.anchors)


@pytest.mark.asyncio
async def test_rejected_second_plan_keeps_first_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())

    async def execute(task):
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="x",
                query_weight=1,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    await session.synthesize(_plan("accepted"))
    bad = _plan("bad").model_copy(update={"intents": [_plan().intents[0].model_copy(update={"required_cases": ["unknown.c"]})]})
    with pytest.raises(AnchorPlanError):
        await session.synthesize(bad)
    assert session.receipt is not None and session.receipt.plan.summary == "accepted"


@pytest.mark.asyncio
async def test_session_waits_for_sibling_cleanup_before_propagating_failure(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    sibling_started = asyncio.Event()
    sibling_stopped = asyncio.Event()

    async def execute(task):
        if task.authority.anchor_id == "copy-site":
            await sibling_started.wait()
            raise RuntimeError("synthesis failed")
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_stopped.set()

    second = _plan().intents[0].model_copy(update={"id": "second-site"})
    plan = _plan().model_copy(update={"intents": [*_plan().intents, second]})
    session = AnchorSynthesisSession(
        authority,
        workspace_root=tmp_path,
        runtime=ScriptedRuntime(execute),
        max_parallel=2,
    )

    with pytest.raises(RuntimeError, match="synthesis failed"):
        await session.synthesize(plan)
    assert sibling_stopped.is_set()


def test_plan_validation_only_rejects_invalid_or_unknown_case_names():
    intent = _plan().intents[0]
    plan = _plan().model_copy(
        update={
            "intents": [
                intent.model_copy(
                    update={"required_cases": ["nested/case1_var1.c", "unknown.c"]}
                )
            ]
        }
    )
    errors = synthesis_module.validate_anchor_plan(plan, _rca().extracted_case_files)
    assert any("invalid Case Artifact names" in error for error in errors)
    assert any("unknown Case Artifacts" in error for error in errors)


@pytest.mark.asyncio
async def test_plan_normalizes_duplicate_case_references_after_collective_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(
        source,
        cases,
        GroundingPolicy.REPOSITORY_EVIDENCE,
        _rca(),
    )
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())
    observed_required_cases: list[list[str]] = []

    async def execute(task):
        intent = task.authority.plan.intents[0]
        observed_required_cases.append(intent.required_cases)
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="copy($A)",
                query_weight=1,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    intent = _plan().intents[0].model_copy(
        update={"required_cases": ["case1_var1.c", "case1.c", "case1_var1.c"]}
    )
    plan = _plan().model_copy(update={"intents": [intent]})
    assert synthesis_module.validate_anchor_plan(
        plan,
        _rca().extracted_case_files,
    ) == ()

    session = AnchorSynthesisSession(
        authority,
        workspace_root=tmp_path,
        runtime=ScriptedRuntime(execute),
    )
    await session.synthesize(plan)

    assert observed_required_cases == [["case1_var1.c", "case1.c"]]
    assert session.receipt is not None
    assert session.receipt.plan.intents[0].required_cases == ["case1_var1.c", "case1.c"]


def test_plan_requires_every_declared_case_to_be_assigned_to_an_intent():
    plan = _plan().model_copy(
        update={
            "intents": [
                _plan().intents[0].model_copy(update={"required_cases": ["case1.c"]})
            ]
        }
    )

    errors = synthesis_module.validate_anchor_plan(
        plan,
        _rca().extracted_case_files,
    )

    assert errors == (
        "Anchor Plan does not assign every declared Case Artifact to an intent: case1_var1.c",
    )


@pytest.mark.asyncio
async def test_plan_has_no_fixed_intent_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())

    intents = [
        AnchorIntent(
            id=f"site-{index}",
            behavior_weight=1,
            behavior=f"behavior {index}",
            inspect_hint=f"inspect {index}",
            required_cases=["case1.c", "case1_var1.c"],
        )
        for index in range(1, 10)
    ]
    plan = AnchorPlan(summary="many independent sites", intents=intents)

    async def execute(task):
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type=QueryType.PATTERN,
                query="copy($A)",
                query_weight=1,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake-model"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    results = await session.synthesize(plan)

    assert len(results) == 9
    assert session.receipt is not None
    assert len(session.receipt.plan.intents) == 9


def test_delta_wire_shape_forbids_intent_fields():
    with pytest.raises(ValidationError):
        AnchorSynthesisDelta.model_validate(
            {
                "anchor_id": "copy-site",
                "type": "pattern",
                "query": "x",
                "query_weight": 1,
                "adjustments": [],
                "plan_suggestion": "",
                "behavior": "drift",
            }
        )


def test_synthesizer_task_name_includes_plan_position(tmp_path: Path):
    intents = [
        AnchorIntent(
            id=f"site-{index}",
            behavior_weight=1,
            behavior=f"behavior {index}",
            inspect_hint=f"inspect {index}",
            required_cases=["case1.c"],
        )
        for index in (1, 2)
    ]
    plan = AnchorPlan(summary="two sites", intents=intents)

    task = make_ast_grep_synthesis_task(
        plan,
        intents[1],
        workspace_root=tmp_path,
        source_root=tmp_path,
        cases_dir=tmp_path,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause=_rca(),
    )

    assert task.task_id == "ast-grep-synthesis:1:site-2"
    assert task.agent_name == "AST-Grep Synthesizer [1.2/2]"


def test_query_grounding_accepts_a_match_elsewhere_in_an_rca_component_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    root_cause = _rca().model_copy(
        update={
            "buggy_components": [
                BuggyComponent(
                    file="bug.c",
                    start_line=10,
                    end_line=10,
                    role="copy",
                    snippet="copy();",
                )
            ]
        }
    )
    authority = RuleGenerationAuthority(
        source,
        cases,
        GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause,
    )
    anchor = Anchor(
        id="copy-site",
        behavior_weight=4,
        query_weight=2,
        type="pattern",
        query="copy($A)",
        behavior="copy a runtime length",
        inspect_hint="inspect the bound",
    )
    intent = _plan().intents[0].model_copy(
        update={"required_cases": ["case1.c"]}
    )
    scans = iter(
        (
            type("Scan", (), {"matches": [type("Match", (), {"file": "case1.c"})()]})(),
            type("Scan", (), {"matches": [type("Match", (), {"file": "bug.c"})()]})(),
        )
    )
    monkeypatch.setattr(
        synthesis_module,
        "scan_anchors",
        lambda *_args, **_kwargs: next(scans),
    )

    assert synthesis_module._query_errors(anchor, intent, authority) == ()


@pytest.mark.asyncio
async def test_turn_budget_exception_resumes_once_with_exhaustion_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(
        source,
        cases,
        GroundingPolicy.REPOSITORY_EVIDENCE,
        _rca(),
    )
    monkeypatch.setattr(synthesis_module, "make_ast_grep_synthesis_task", partial(
        make_ast_grep_synthesis_task,
        limits=RunLimits(request_limit=5),
    ))
    prompts: list[str] = []

    async def execute(task):
        prompts.append(task.prompt)
        if len(prompts) == 1:
            raise TurnBudgetExceeded(5)
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="",
                query_weight=1,
                adjustments=[],
                plan_suggestion="Revise the intent after query synthesis exhausted its turns.",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    result = await AnchorSynthesisSession(
        authority,
        workspace_root=tmp_path,
        runtime=ScriptedRuntime(execute),
    ).synthesize(_plan())

    assert len(prompts) == 2
    assert prompts[0].startswith("Generate and validate only the ast-grep query")
    assert prompts[1].startswith("The query-writing budget of 5 model turns is exhausted")
    assert result[0].anchor.query == ""
    assert result[0].plan_suggestion.startswith("Revise the intent")


@pytest.mark.asyncio
async def test_query_repairs_use_turn_budget_but_execution_failures_propagate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    calls = 0
    task_labels: list[tuple[str, str]] = []
    prompts: list[str] = []
    monkeypatch.setattr(synthesis_module, "make_ast_grep_synthesis_task", partial(
        make_ast_grep_synthesis_task, limits=RunLimits(request_limit=5),
    ))

    async def execute(task):
        nonlocal calls
        calls += 1
        task_labels.append((task.task_id, task.agent_name))
        prompts.append(task.prompt)
        exhausted = task.prompt.startswith("The query-writing budget")
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="" if exhausted else "broken(",
                query_weight=1,
                adjustments=[],
                plan_suggestion=(
                    "Split the intent because every attempted query missed its required evidence."
                    if exhausted
                    else ""
                ),
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    monkeypatch.setattr(
        synthesis_module,
        "scan_anchors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AnchorQueryError("invalid pattern")),
    )
    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    result = await session.synthesize(_plan())
    assert calls == 6
    assert task_labels == [
        ("ast-grep-synthesis:1:copy-site", "AST-Grep Synthesizer [1.1/1]"),
    ] * 6
    assert prompts[0].startswith("Generate and validate only the ast-grep query")
    assert prompts[1] == prompts[2]
    assert prompts[1].startswith("The previous query failed deterministic validation:")
    assert "ast-grep validation failed: invalid pattern" in prompts[1]
    assert "Revise only the query fields for this target anchor." in prompts[1]
    assert prompts[0] not in prompts[1]
    assert "query-writing budget of 5 model turns is exhausted" in prompts[-1]
    assert "query set to an empty string" in prompts[-1]
    assert result[0].anchor.query == ""
    assert result[0].plan_suggestion == (
        "Split the intent because every attempted query missed its required evidence."
    )
    assert session.receipt is not None

    calls = 0
    task_labels.clear()
    monkeypatch.setattr(
        synthesis_module,
        "scan_anchors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AnchorExecutionError("binary missing")),
    )
    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    with pytest.raises(AnchorExecutionError, match="binary missing"):
        await session.synthesize(_plan())
    assert calls == 1


@pytest.mark.asyncio
async def test_session_returns_and_persists_deduplicated_synthesis_experiences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())
    recorded: list[AstGrepExperience] = []

    def record(_skill_root, experiences):
        recorded.extend(experiences)
        return len(experiences)

    monkeypatch.setattr(synthesis_module, "record_ast_grep_experiences", record)
    lesson = AstGrepExperience(
        mode="ADD",
        lesson_id="C-1",
        lesson="A C call fragment may require statement context.",
    )

    async def execute(task):
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="copy($A)",
                query_weight=1,
                adjustments=[],
                experiences=[lesson],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    result = await session.synthesize(_plan())

    assert result[0].experiences == [lesson]
    assert recorded == [lesson]


def test_delta_normalizes_and_deduplicates_query_writing_experience_updates():
    common = {
        "anchor_id": "copy-site",
        "type": "pattern",
        "query": "copy($A)",
        "query_weight": 1,
        "adjustments": [],
        "plan_suggestion": "",
    }
    lessons = [
        {"mode": "ADD", "lesson_id": "ALL-1", "lesson": " initial lesson "},
        {"mode": "ADD", "lesson_id": "ALL-2", "lesson": "first\n lesson"},
        {"mode": "REPLACE", "lesson_id": "ALL-1", "lesson": "updated guidance"},
        {"mode": "ADD", "lesson_id": "ALL-3", "lesson": "third lesson"},
        {"mode": "ADD", "lesson_id": "ALL-4", "lesson": "fourth lesson"},
    ]

    delta = AnchorSynthesisDelta.model_validate({**common, "experiences": lessons})

    assert [(experience.lesson_id, experience.lesson) for experience in delta.experiences] == [
        ("ALL-1", "updated guidance"),
        ("ALL-2", "first lesson"),
        ("ALL-3", "third lesson"),
    ]

    long_lesson = AnchorSynthesisDelta.model_validate(
        {
            **common,
            "experiences": [
                {"mode": "ADD", "lesson_id": "ALL-99", "lesson": "x" * 300}
            ],
        }
    )
    assert long_lesson.experiences[0].lesson == "x" * 300


@pytest.mark.asyncio
async def test_session_skips_experiences_when_turns_are_below_half_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())
    recorded: list[AstGrepExperience] = []
    monkeypatch.setattr(
        synthesis_module,
        "record_ast_grep_experiences",
        lambda _root, experiences: recorded.extend(experiences) or len(experiences),
    )

    async def execute(task):
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="copy($A)",
                query_weight=1,
                adjustments=[],
                experiences=[
                    AstGrepExperience(
                        mode="ADD",
                        lesson_id="ALL-1",
                        lesson="A reusable lesson discovered after debugging.",
                    )
                ],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
            usage=RuntimeUsage(turns=19),
        )

    result = await AnchorSynthesisSession(
        authority,
        workspace_root=tmp_path,
        runtime=ScriptedRuntime(execute),
    ).synthesize(_plan())

    assert result[0].experiences == []
    assert recorded == []


def test_delta_caps_distinct_experience_updates_at_three():
    common = {
        "anchor_id": "copy-site",
        "type": "pattern",
        "query": "copy($A)",
        "query_weight": 1,
        "adjustments": [],
        "plan_suggestion": "",
    }
    delta = AnchorSynthesisDelta.model_validate(
        {
            **common,
            "experiences": [
                {
                    "mode": "ADD",
                    "lesson_id": f"ALL-{index}",
                    "lesson": f"lesson {index}",
                }
                for index in range(1, 31)
            ],
        }
    )

    assert [experience.lesson_id for experience in delta.experiences] == [
        "ALL-1",
        "ALL-2",
        "ALL-3",
    ]


def test_delta_normalizes_lesson_whitespace():
    common = {
        "anchor_id": "copy-site",
        "type": "pattern",
        "query": "copy($A)",
        "query_weight": 1,
        "adjustments": [],
        "plan_suggestion": "",
    }
    delta = AnchorSynthesisDelta.model_validate(
        {
            **common,
            "experiences": [
                {"mode": "ADD", "lesson_id": "ALL-1", "lesson": " a lesson "},
            ],
        }
    )

    assert delta.experiences[0].lesson == "a lesson"


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [False, True])
async def test_deterministic_repairs_resume_one_runtime_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovers):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    validations = iter([("query misses case1.c",)] * (4 if recovers else 5) + [()])
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: next(validations))
    monkeypatch.setattr(synthesis_module, "make_ast_grep_synthesis_task", partial(
        make_ast_grep_synthesis_task, limits=RunLimits(request_limit=5),
    ))

    class Conversation:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.closed = False

        async def send(self, prompt: str, *, request_limit_extension: int = 0):
            self.prompts.append(prompt)
            exhausted = prompt.startswith("The query-writing budget")
            return AgentRunResult(
                output=AnchorSynthesisDelta(
                    anchor_id="copy-site",
                    type="pattern",
                    query="" if exhausted else "copy($A)",
                    query_weight=1,
                    adjustments=[],
                    plan_suggestion=(
                        "Revise the intent because the attempted queries missed case1.c."
                        if exhausted
                        else ""
                    ),
                ),
                identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
            )

        async def close(self):
            self.closed = True

    class Runtime:
        identity = RuntimeIdentity(runtime_id="fake", model_id="fake")

        def __init__(self) -> None:
            self.sessions: list[Conversation] = []

        def open_session(self, _task):
            session = Conversation()
            self.sessions.append(session)
            return session

    runtime = Runtime()
    session = AnchorSynthesisSession(
        authority,
        workspace_root=tmp_path,
        runtime=runtime,
    )
    if recovers:
        result = await session.synthesize(_plan())
        assert result[0].anchor.query == "copy($A)"
    else:
        result = await session.synthesize(_plan())
        assert result[0].anchor.query == ""
        assert result[0].plan_suggestion == (
            "Revise the intent because the attempted queries missed case1.c."
        )
        assert session.receipt is not None
    assert len(runtime.sessions) == 1
    conversation = runtime.sessions[0]
    assert conversation.closed
    assert len(conversation.prompts) == (5 if recovers else 6)
    assert conversation.prompts[0].startswith("Generate and validate only the ast-grep query")
    assert all("query misses case1.c" in prompt for prompt in conversation.prompts[1:])
    assert all(
        "Revise only the query fields for this target anchor." in prompt
        for prompt in conversation.prompts[1:5]
    )
    if not recovers:
        assert "query-writing budget of 5 model turns is exhausted" in conversation.prompts[-1]


def test_query_dedup_keeps_weighted_representatives_and_disabled_anchors():
    def result(
        anchor_id: str,
        query: str,
        *,
        query_weight: int,
        behavior_weight: int,
        query_type: QueryType = QueryType.PATTERN,
    ) -> AnchorSynthesisResult:
        return AnchorSynthesisResult(
            anchor=Anchor(
                id=anchor_id,
                behavior_weight=behavior_weight,
                query_weight=query_weight,
                type=query_type,
                query=query,
                behavior=f"behavior {anchor_id}",
                inspect_hint=f"inspect {anchor_id}",
            ),
            adjustments=[],
            experiences=[],
            plan_suggestion="",
        )

    results = [
        result("first", "x\r\n", query_weight=1, behavior_weight=5),
        result("second", " x ", query_weight=2, behavior_weight=2),
        result("third", "x", query_weight=2, behavior_weight=4),
        result("fourth", "x", query_weight=2, behavior_weight=4),
        result("different-type", "x", query_weight=1, behavior_weight=1, query_type=QueryType.RULE),
        result("different-space", "x  \n y", query_weight=1, behavior_weight=1),
        result("disabled-one", "", query_weight=1, behavior_weight=1),
        result("disabled-two", "   ", query_weight=1, behavior_weight=1),
    ]

    anchors = deduplicate_query_anchors(results, language="c")

    assert [anchor.id for anchor in anchors] == [
        "third",
        "different-type",
        "different-space",
        "disabled-one",
        "disabled-two",
    ]
    assert anchors[0].behavior == "behavior third"
    assert anchors[0].inspect_hint == "inspect third"
    assert anchors[0].query_weight == 2


@pytest.mark.asyncio
async def test_repair_attempts_keep_only_final_experience_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    validations = iter((("first miss",), ("second miss",), ()))
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: next(validations))
    recorded_batches: list[list[AstGrepExperience]] = []
    monkeypatch.setattr(
        synthesis_module,
        "record_ast_grep_experiences",
        lambda _root, experiences: recorded_batches.append(list(experiences))
        or len(experiences),
    )
    calls = 0

    async def execute(task):
        nonlocal calls
        calls += 1
        lessons = {
            1: [
                AstGrepExperience(
                    mode="ADD",
                    lesson_id="ALL-1",
                    lesson="First attempt produced an obsolete lesson.",
                ),
                AstGrepExperience(
                    mode="ADD",
                    lesson_id="ALL-2",
                    lesson="First attempt produced another obsolete lesson.",
                ),
            ],
            2: [
                AstGrepExperience(
                    mode="ADD",
                    lesson_id="ALL-3",
                    lesson="Second attempt produced an obsolete lesson.",
                ),
            ],
            3: [
                AstGrepExperience(
                    mode="ADD",
                    lesson_id="ALL-4",
                    lesson="Final attempt produced the reusable lesson.",
                ),
            ],
        }[calls]
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id,
                type="pattern",
                query="copy($A)",
                query_weight=1,
                adjustments=[],
                experiences=lessons,
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    result = await AnchorSynthesisSession(
        authority,
        workspace_root=tmp_path,
        runtime=ScriptedRuntime(execute),
    ).synthesize(_plan())

    assert calls == 3
    assert [experience.lesson_id for experience in result[0].experiences] == ["ALL-4"]
    assert recorded_batches == [result[0].experiences]
