"""End-to-end tests for the shared issue/example-suite workflow."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from git import Actor, Repo

from src.miner.core.example_suite_intake import (
    inspect_example_suite,
    materialize_example_suite,
)
from src.miner.core.workflow import ExampleSuiteWorkflow, IssueWorkflow, WorkflowOptions
from src.miner.runtime import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    RuntimeCapability,
    RuntimeRouter,
)
from src.miner.utils.models import IssueCollectionInfo, RootCauseAnalysis, VASCoreInfo
from src.miner.utils.workspace import Workspace

SOURCE = "void trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


def _prepare_repo(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    source = path / "bug.c"
    source.write_text(SOURCE, encoding="utf-8")
    repo = Repo.init(path)
    actor = Actor("VAS Test", "vas-test@example.com")
    repo.index.add(["bug.c"])
    buggy = repo.index.commit("buggy", author=actor, committer=actor)
    source.write_text(
        "void trigger(void) { guard(1); danger(1); }\n",
        encoding="utf-8",
    )
    repo.index.add(["bug.c"])
    fixed = repo.index.commit("fixed", author=actor, committer=actor)
    repo.create_head("buggy", buggy)
    repo.create_head("fixed", fixed)
    repo.heads.buggy.checkout()
    return buggy.hexsha, fixed.hexsha


def _root_cause(*, file: str = "bug.c") -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The dangerous call is reached before the guard establishes the invariant.",
            "buggy_components": [
                {
                    "file": file,
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": SOURCE.rstrip(),
                }
            ],
            "fixing_pattern": "Establish the required guard before invoking danger.",
            "extracted_case_files": ["case1.c"],
        }
    )


def _core(root_cause: RootCauseAnalysis) -> VASCoreInfo:
    return VASCoreInfo.model_validate(
        {
            "category": "SECURITY",
            "language": root_cause.language,
            "root_cause_summary": root_cause.root_cause_summary,
            "summary": "Dangerous operations must run only after the required guard is established.",
            "scenarios": {
                "unsafe": ["The dangerous operation executes before its required guard."],
                "safe": ["The required guard is established before the dangerous operation."],
            },
            "anchors": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "query_weight": 5,
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": BEHAVIOR,
                    "inspect_hint": INSPECT_HINT,
                }
            ],
        }
    )


@dataclass
class ScriptedRuntime:
    runtime_id: str
    model_id: str
    root_cause: RootCauseAnalysis
    core: VASCoreInfo
    collection: IssueCollectionInfo | None = None
    capabilities: frozenset[RuntimeCapability] = frozenset(RuntimeCapability)
    calls: list[AgentTask[Any]] = field(default_factory=list)

    def model_id_for(self, task: AgentTask[Any]) -> str:
        return task.model_hint or self.model_id

    async def run(self, task: AgentTask[Any]) -> AgentRunResult[Any]:
        self.calls.append(task)
        if task.phase is AgentPhase.ISSUE_COLLECTION:
            assert self.collection is not None
            output: Any = self.collection
        elif task.phase is AgentPhase.ROOT_CAUSE:
            assert task.context.cases_dir is not None
            (task.context.cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
            output = self.root_cause
        else:
            assert task.phase is AgentPhase.RULE_GENERATION
            output = self.core
        assert task.validate_output(output) == ()
        return AgentRunResult(
            output=output,
            runtime_id=self.runtime_id,
            model_id=self.model_id_for(task),
        )


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_issue_workflow_routes_one_complete_rule_generation_and_caches_by_runtime(
    tmp_path: Path,
):
    workspace_root = tmp_path / "workspaces" / "VAS-0001"
    Workspace._ensure_structure(workspace_root)
    workspace = Workspace(workspace_root, "VAS-0001", rules_dir=tmp_path / "rules")
    repo_path = workspace.root / "src" / "fixture" / "project"
    buggy_sha, fixed_sha = _prepare_repo(repo_path)
    root_cause = _root_cause()
    core = _core(root_cause)
    collection = IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="Fixture issue",
        issue_details="A dangerous operation lacks its required guard.",
        repo_url="https://github.com/example/project",
        repo_path=repo_path.as_posix(),
        buggy_commit=buggy_sha,
        fixed_commit=fixed_sha,
    )
    sdk = ScriptedRuntime("pydantic-ai", "sdk-model", root_cause, core, collection)
    cli = ScriptedRuntime("claude-code", "cli-model", root_cause, core, collection)
    router = RuntimeRouter(
        runtimes={sdk.runtime_id: sdk, cli.runtime_id: cli},
        default_runtime=sdk.runtime_id,
        phase_runtimes={AgentPhase.RULE_GENERATION: cli.runtime_id},
    )

    result = await IssueWorkflow(router).run(
        "CVE-2099-0001",
        vas_id=workspace.vas_id,
        workspace=workspace,
    )

    assert [task.phase for task in sdk.calls] == [
        AgentPhase.ISSUE_COLLECTION,
        AgentPhase.ROOT_CAUSE,
    ]
    assert [task.phase for task in cli.calls] == [AgentPhase.RULE_GENERATION]
    assert result.core == core
    assert result.vas.sources[0].type == "issue"
    assert len(result.agent_runs) == 3
    assert workspace.analysis_path.is_file()
    assert workspace.anchor_review_path.is_file()
    assert workspace.rule_path.is_file()
    cache_names = {path.name for path in workspace.cache_dir.glob("*.json")}
    assert any("pydantic-ai.sdk-model" in name for name in cache_names)
    assert any("claude-code.cli-model" in name for name in cache_names)

    sdk.calls.clear()
    cli.calls.clear()
    cached = await IssueWorkflow(
        router,
        options=WorkflowOptions(use_cache=True),
    ).run(
        "CVE-2099-0001",
        vas_id=workspace.vas_id,
        workspace=workspace,
    )

    assert sdk.calls == []
    assert cli.calls == []
    assert cached.core == result.core
    assert cached.agent_runs == ()


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
async def test_example_suite_workflow_uses_snapshot_as_source_and_publishes_portable_provenance(
    tmp_path: Path,
):
    suite_dir = tmp_path / "CWE-2099-fixture"
    suite_dir.mkdir()
    (suite_dir / "bad.c").write_text(SOURCE, encoding="utf-8")
    workspace_root = tmp_path / "workspaces" / "VAS-0001"
    Workspace._ensure_structure(workspace_root)
    workspace = Workspace(workspace_root, "VAS-0001", rules_dir=tmp_path / "rules")
    intake = materialize_example_suite(
        inspect_example_suite(suite_dir),
        workspace=workspace,
    )
    root_cause = _root_cause(file="bad.c")
    core = _core(root_cause)
    runtime = ScriptedRuntime("claude-code", "cli-model", root_cause, core)
    router = RuntimeRouter(
        runtimes={runtime.runtime_id: runtime},
        default_runtime=runtime.runtime_id,
    )

    result = await ExampleSuiteWorkflow(router).run(
        intake,
        vas_id=workspace.vas_id,
        workspace=workspace,
    )

    assert [task.phase for task in runtime.calls] == [
        AgentPhase.ROOT_CAUSE,
        AgentPhase.RULE_GENERATION,
    ]
    assert all(task.context.repo_path is None for task in runtime.calls)
    assert all(task.context.source_root == workspace.example_suite_snapshot_dir for task in runtime.calls)
    source = result.vas.sources[0]
    assert source.type == "example_suite"
    assert source.registry_key == "example-suite:CWE-2099-fixture"
    assert source.snapshot_ref == "input_snapshot"
    published = json.loads(workspace.rule_path.read_text(encoding="utf-8"))
    rendered = json.dumps(published)
    assert intake.source_path not in rendered
    assert intake.snapshot_path not in rendered
    assert "case_bundle" not in rendered
