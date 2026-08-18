import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.miner.agent import AgentPhase, RootCauseAuthority
from src.miner.mining.tasks import (
    PHASE_DEFINITIONS,
    make_issue_collection_task,
    make_root_cause_task,
)
from src.miner.mining.examples import ExampleSuiteIntake
from src.miner.models import GroundingPolicy, IssueCollectionInfo


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


def test_phase_definitions_are_closed_and_least_privilege(tmp_path: Path):
    assert set(PHASE_DEFINITIONS) == set(AgentPhase)
    root_tools = set(PHASE_DEFINITIONS[AgentPhase.ROOT_CAUSE].tools)
    rule_tools = set(PHASE_DEFINITIONS[AgentPhase.RULE_GENERATION].tools)
    synthesis_tools = set(PHASE_DEFINITIONS[AgentPhase.AST_GREP_SYNTHESIS].tools)
    assert "write_case_artifact" in root_tools
    assert not {"write_file", "Write", "Bash"} & root_tools
    assert rule_tools == {"list_case_artifacts", "read_case_artifact", "synthesize_anchor_plan"}
    assert "write_case_artifact" not in synthesis_tools


def test_instruction_layers_preserve_shared_then_input_then_runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    cases = workspace / "cases"
    source.mkdir(parents=True)
    cases.mkdir()
    task = make_root_cause_task(
        _collection(source),
        workspace_root=workspace,
        source_root=source,
        cases_dir=cases,
        grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
    )
    assert isinstance(task.authority, RootCauseAuthority)
    rendered = task.instructions.render("# Runtime Binding\n\nexact tools")
    assert rendered.count(task.instructions.shared) == 1
    assert rendered.count(task.instructions.input_policy) == 1
    assert rendered.index(task.instructions.shared) < rendered.index(task.instructions.input_policy)
    assert rendered.index(task.instructions.input_policy) < rendered.index("# Runtime Binding")


def test_repository_root_cause_context_declares_bound_root_and_revisions(tmp_path: Path):
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

    payload = json.loads(task.prompt)
    assert payload["intake"]["source_layout"] == "repository_checkout"
    assert payload["src_tools"] == {
        "root": source_root.resolve().as_posix(),
        "path_arguments": "relative_to_root",
    }
    assert source_root.resolve().as_posix() in task.input_policy
    assert _collection(source_root).buggy_commit in task.input_policy
    assert fixed_commit in task.input_policy
    assert "read_patch_diff" in task.input_policy
    assert "read_patch_diff" in task.tools


def test_example_suite_root_cause_context_declares_contrastive_evidence(tmp_path: Path):
    source_root = tmp_path / "src" / "input_snapshot"
    cases = tmp_path / "cases"
    source_root.mkdir(parents=True)
    cases.mkdir()
    suite = ExampleSuiteIntake(
        registry_key="example-suite:sample",
        suite_name="sample",
        source_path=(tmp_path / "sample").as_posix(),
        content_digest="a" * 64,
        file_count=3,
        total_bytes=3,
        source_files=["bad.c", "good.c"],
        files=[
            {"path": "bad.c", "size": 1, "sha256": "b" * 64, "source": True},
            {"path": "good.c", "size": 1, "sha256": "c" * 64, "source": True},
            {"path": "manifest.json", "size": 1, "sha256": "d" * 64, "source": False},
        ],
        manifest_path="manifest.json",
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

    payload = json.loads(task.prompt)
    assert payload["intake"]["source_layout"] == "example_suite_snapshot"
    assert payload["intake"]["file_count"] == 3
    assert payload["intake"]["source_file_count"] == 2
    assert "files" not in payload["intake"]
    assert "source_files" not in payload["intake"]
    assert source_root.resolve().as_posix() in task.input_policy
    assert "bad/unsafe" in task.input_policy
    assert "good/safe" in task.input_policy
    assert "read_patch_diff" not in task.tools


def test_issue_input_is_validated_before_task_construction(tmp_path: Path):
    try:
        make_issue_collection_task("  ", workspace_root=tmp_path)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty issue input was accepted")
    instructions = PHASE_DEFINITIONS[AgentPhase.ISSUE_COLLECTION].instructions
    assert "ask if not provided" not in instructions


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
