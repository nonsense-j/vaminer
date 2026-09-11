from dataclasses import replace
from pathlib import Path

import pytest

from src.miner.agent import AgentPhase, InstructionLayers
from src.miner.mining.tasks import (
    PHASE_DEFINITIONS,
    make_ast_grep_synthesis_task,
    make_issue_collection_task,
    make_root_cause_task,
    make_rule_generation_task,
)
from src.miner.mining.examples import ExampleSuiteIntake, inspect_example_suite
from src.miner.models import (
    AnchorIntent,
    AnchorPlan,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCollectionInfo,
    RootCauseAnalysis,
)


def _collection(repo: Path) -> IssueCollectionInfo:
    return IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="summary",
        issue_details="details",
        repo_url="https://example.invalid/repo.git",
        buggy_commit="a" * 40,
        fixed_commit=None,
        repo_path=str(repo),
    )


def _root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        language=AstGrepLanguage.C,
        root_cause_summary="unchecked copy",
        analysis="an unbounded length reaches copy",
        buggy_components=[
            BuggyComponent(
                file="bug.c",
                start_line=1,
                end_line=1,
                role="copy",
                snippet="copy(input, length);",
            )
        ],
        fixing_pattern="bound the length",
        extracted_case_files=["case1.c"],
    )


def test_phase_definitions_are_closed_and_least_privilege(tmp_path: Path):
    assert set(PHASE_DEFINITIONS) == set(AgentPhase)
    root_tools = set(PHASE_DEFINITIONS[AgentPhase.ROOT_CAUSE].tools)
    rule_tools = set(PHASE_DEFINITIONS[AgentPhase.RULE_GENERATION].tools)
    synthesis_tools = set(PHASE_DEFINITIONS[AgentPhase.AST_GREP_SYNTHESIS].tools)
    assert "write_case_artifact" in root_tools
    assert not {"write_file", "Write", "Bash"} & root_tools
    assert rule_tools == {"list_case_artifacts", "read_case_artifact", "synthesize_anchor_plan"}
    assert "write_case_artifact" not in synthesis_tools


def test_instruction_layers_preserve_shared_input_runtime_order():
    shared = "shared-sentinel"
    input_policy = "input-sentinel"
    runtime_binding = "runtime-sentinel"
    rendered = InstructionLayers(shared, input_policy).render(runtime_binding)

    assert rendered.index(shared) < rendered.index(input_policy) < rendered.index(runtime_binding)


def test_repository_root_cause_task_builds_typed_intake_and_fixed_diff_capability(tmp_path: Path):
    source_root = tmp_path / "src" / "owner" / "repo"
    cases = tmp_path / "cases"
    source_root.mkdir(parents=True)
    cases.mkdir()
    fixed_commit = "b" * 40
    task = make_root_cause_task(
        _collection(source_root).model_copy(update={"fixed_commit": fixed_commit}),
        workspace_root=tmp_path,
        source_root=source_root,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )

    assert task.prompt.splitlines()[0] == (
        "Analyze the supplied issue and extract minimal defective Case Artifacts."
    )
    assert "Issue ID: CVE-2099-0001" in task.prompt
    assert "Issue summary:\nsummary" in task.prompt
    assert "Issue details:\ndetails" in task.prompt
    assert f"Fixed commit: {fixed_commit}" in task.prompt
    assert source_root.as_posix() not in task.prompt
    assert "source_layout" not in task.prompt
    assert "fixed_revision_available" not in task.prompt
    assert "read_patch_diff" in task.tools


def test_example_suite_root_cause_task_builds_bounded_typed_intake(tmp_path: Path):
    source_root = tmp_path / "src" / "input_snapshot"
    cases = tmp_path / "cases"
    nested = source_root / "nested"
    nested.mkdir(parents=True)
    cases.mkdir()
    (source_root / "bad.c").write_text("danger();\n", encoding="utf-8")
    (nested / "good.c").write_text("safe();\n", encoding="utf-8")
    (source_root / "manifest.json").write_text('{"bad": ["bad.c"]}\n', encoding="utf-8")
    inspection = inspect_example_suite(source_root)
    suite = ExampleSuiteIntake(
        **inspection.model_dump(mode="json"),
        snapshot_path=source_root.as_posix(),
        snapshot_ref="src/input_snapshot",
    )
    task = make_root_cause_task(
        suite,
        workspace_root=tmp_path,
        source_root=source_root,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
    )

    assert task.prompt == (
        "Analyze the supplied Example Suite directory and extract minimal defective "
        "Case Artifacts from source code only.\n\n"
        f"Example Suite directory: {source_root.resolve().as_posix()}\n"
    )
    assert "danger();" not in task.prompt
    assert "safe();" not in task.prompt
    assert "bad.c" not in task.prompt
    assert "good.c" not in task.prompt
    assert "manifest.json" not in task.prompt
    assert "file_paths" not in task.prompt
    assert "content_digest" not in task.prompt
    assert "snapshot_ref" not in task.prompt
    assert "registry_key" not in task.prompt
    assert "example-suite:" not in task.prompt
    assert "Analyze source code only" in task.input_policy
    assert task.definition is PHASE_DEFINITIONS[AgentPhase.ROOT_CAUSE]
    assert set(task.tools) == set(PHASE_DEFINITIONS[AgentPhase.ROOT_CAUSE].tools)


def test_all_production_prompts_are_minimal_plain_text(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    root_cause = _root_cause()
    intent = AnchorIntent(
        id="copy-site",
        behavior_weight=4,
        behavior="copy a runtime length",
        inspect_hint="inspect its bound",
        required_cases=["case1.c"],
    )
    plan = AnchorPlan(summary="copy sites", intents=[intent])

    issue_task = make_issue_collection_task("CVE-2099-0001", workspace_root=tmp_path)
    rule_task = make_rule_generation_task(
        root_cause,
        workspace_root=tmp_path,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    synthesis_task = make_ast_grep_synthesis_task(
        plan,
        intent,
        workspace_root=tmp_path,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
        root_cause=root_cause,
    )

    assert issue_task.prompt.splitlines()[0] == (
        "Collect verified issue evidence and prepare the checkout for root-cause analysis."
    )
    assert rule_task.prompt.splitlines()[0] == (
        "Define repository-independent rule semantics and a queryless Anchor Plan from the authoritative RCA."
    )
    assert synthesis_task.prompt.splitlines()[0] == (
        'Generate and validate only the ast-grep query for target anchor "copy-site"; '
        "keep it within that anchor's behavior."
    )
    for prompt in (issue_task.prompt, rule_task.prompt, synthesis_task.prompt):
        assert not prompt.lstrip().startswith("{")
    assert "[Root Cause Analysis]" in rule_task.prompt
    assert "Declared Case Artifacts:\n- case1.c" in rule_task.prompt
    assert "[Target AnchorIntent]" in synthesis_task.prompt
    assert "Required Case Artifacts:\n- case1.c" in synthesis_task.prompt
    assert "[Root Cause Analysis]" in synthesis_task.prompt
    assert "copy(input, length);" in synthesis_task.prompt
    assert "copy sites" not in synthesis_task.prompt
    assert source.as_posix() not in rule_task.prompt


def test_issue_input_is_validated_before_task_construction(tmp_path: Path):
    try:
        make_issue_collection_task("  ", workspace_root=tmp_path)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty issue input was accepted")


def test_task_rejects_mismatched_phase_definition(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    task = make_root_cause_task(
        _collection(source),
        workspace_root=tmp_path,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    with pytest.raises(ValueError, match="does not match"):
        replace(task, definition=PHASE_DEFINITIONS[AgentPhase.ISSUE_COLLECTION])
