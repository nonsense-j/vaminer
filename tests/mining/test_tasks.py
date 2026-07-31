"""Tests for runtime-neutral task construction."""

from __future__ import annotations

import json
from pathlib import Path

from src.miner.agent import AgentPhase, FileAccess, RuntimeCapability
from src.miner.models import IssueCollectionInfo, VASCoreInfo
from src.miner.mining.tasks import (
    make_issue_collection_task,
    make_root_cause_task,
    make_rule_generation_task,
)
from tests.support.factories import analysis_subject, root_cause


def test_factories_apply_phase_authority_and_conditional_fixed_diff(
    tmp_path: Path,
):
    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    repo_path.mkdir()
    cases_dir.mkdir()
    collection = IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="Fixture summary",
        issue_details="Fixture details",
        repo_url="https://github.com/example/project",
        repo_path=repo_path.as_posix(),
        buggy_commit="deadbeef",
        fixed_commit=None,
    )

    issue_task = make_issue_collection_task(
        "CVE-2099-0001",
        workspace_root=tmp_path,
    )
    rca_task = make_root_cause_task(
        collection,
        workspace_root=tmp_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
    )
    rule_task = make_rule_generation_task(
        root_cause(),
        workspace_root=tmp_path,
        source_root=repo_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(repo_path, cases_dir),
    )

    assert issue_task.workspace.native_workspace_access is FileAccess.NONE
    assert rca_task.workspace.native_workspace_access is FileAccess.READ_WRITE
    assert rule_task.workspace.native_workspace_access is FileAccess.READ_ONLY
    assert rca_task.context.source_root == repo_path
    assert RuntimeCapability.FIXED_DIFF not in rca_task.required_capabilities
    assert rule_task.phase is AgentPhase.RULE_GENERATION
    assert rule_task.output_type is VASCoreInfo
    assert RuntimeCapability.AGENT_DELEGATION in rule_task.required_capabilities
    assert [skill.name for skill in rule_task.skills] == ["ast-grep"]
    assert json.loads(issue_task.prompt) == {"issue_input": "CVE-2099-0001"}
    rule_prompt = json.loads(rule_task.prompt)
    assert set(rule_prompt) == {
        "root_cause_analysis",
        "available_directories",
    }
    assert set(rule_prompt["available_directories"]) == {"cases"}
    assert rule_task.input_instructions.startswith("# Input Policy\n")
