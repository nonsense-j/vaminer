from pathlib import Path

import pytest
from pydantic import ValidationError

from src.miner.agent import AgentRunResult, RuleGenerationAuthority, RuntimeIdentity
from src.miner.anchors.scanner import AnchorExecutionError, AnchorQueryError
from src.miner.mining.validation.vas import validate_vas_core
from src.miner.models import (
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCategory,
    QueryType,
    RootCauseAnalysis,
    RuleGenerationDraft,
    Scenarios,
)
from src.miner.mining import synthesis as synthesis_module
from src.miner.mining.tasks import make_ast_grep_synthesis_task
from src.miner.mining.synthesis import (
    AnchorPlanError,
    AnchorSynthesisLimitError,
    AnchorSynthesisSession,
)


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
async def test_session_owns_immutable_fields_latest_batch_and_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1_var1.c").write_text("copy();\n", encoding="utf-8")
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    monkeypatch.setattr(synthesis_module, "_query_errors", lambda *_args: ())

    async def execute(task):
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                target_anchor_id=task.authority.target_anchor_id,
                type=QueryType.PATTERN,
                query="copy($A)",
                query_weight=3,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake-model"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, execute=execute)
    first = await session.synthesize(_plan("first summary"))
    second = await session.synthesize(_plan("second summary"))
    assert first[0].anchor.behavior == "copy a runtime length"
    assert second[0].anchor.inspect_hint == "inspect the bound"
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
    with pytest.raises(AnchorSynthesisLimitError):
        await session.synthesize(_plan("third"))


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
                target_anchor_id=task.authority.target_anchor_id,
                type="pattern",
                query="x",
                query_weight=1,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, execute=execute)
    await session.synthesize(_plan("accepted"))
    bad = _plan("bad").model_copy(update={"intents": [_plan().intents[0].model_copy(update={"required_cases": ["unknown.c"]})]})
    with pytest.raises(AnchorPlanError):
        await session.synthesize(bad)
    assert session.receipt is not None and session.receipt.plan.summary == "accepted"


def test_plan_rejects_noncanonical_or_duplicate_case_names():
    intent = _plan().intents[0]
    plan = _plan().model_copy(
        update={
            "intents": [
                intent.model_copy(
                    update={"required_cases": ["case1.c", "case1.c", "nested/case1_var1.c"]}
                )
            ]
        }
    )
    errors = synthesis_module.validate_anchor_plan(plan, _rca().extracted_case_files)
    assert any("repeats" in error for error in errors)
    assert any("invalid Case Artifact names" in error for error in errors)


def test_delta_wire_shape_forbids_intent_fields():
    with pytest.raises(ValidationError):
        AnchorSynthesisDelta.model_validate(
            {
                "target_anchor_id": "copy-site",
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

    assert task.agent_name == "AST-Grep Synthesizer [2/2]"
    assert "Repository checkout" in task.input_policy
    assert "RCA-declared source span" in task.input_policy

    suite_task = make_ast_grep_synthesis_task(
        plan,
        intents[1],
        workspace_root=tmp_path,
        source_root=tmp_path,
        cases_dir=tmp_path,
        grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
        root_cause=_rca(),
    )
    assert "Example Suite snapshot" in suite_task.input_policy
    assert "bad/unsafe spans" in suite_task.input_policy
    assert "repository" not in suite_task.input_policy.lower()
    assert "repository" not in suite_task.prompt.lower()
    assert "repository" not in suite_task.instructions.render("").lower()


@pytest.mark.asyncio
async def test_query_failures_degrade_but_scanner_execution_failures_propagate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    authority = RuleGenerationAuthority(source, cases, GroundingPolicy.REPOSITORY_EVIDENCE, _rca())
    calls = 0

    async def execute(task):
        nonlocal calls
        calls += 1
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                target_anchor_id=task.authority.target_anchor_id,
                type="pattern",
                query="broken(",
                query_weight=1,
                adjustments=[],
                plan_suggestion="",
            ),
            identity=RuntimeIdentity(runtime_id="fake", model_id="fake"),
        )

    monkeypatch.setattr(
        synthesis_module,
        "scan_anchors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AnchorQueryError("invalid pattern")),
    )
    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, execute=execute)
    result = await session.synthesize(_plan())
    assert calls == 3
    assert result[0].anchor.query == ""

    calls = 0
    monkeypatch.setattr(
        synthesis_module,
        "scan_anchors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AnchorExecutionError("binary missing")),
    )
    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, execute=execute)
    with pytest.raises(AnchorExecutionError, match="binary missing"):
        await session.synthesize(_plan())
    assert calls == 1
