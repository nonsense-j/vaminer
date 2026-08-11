"""Tests for deterministic issue workflow orchestration."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from git import Actor, Repo

from src.miner.mining.workflow import IssueWorkflow, WorkflowOptions
from src.miner.agent import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    RuntimeCapability,
    RuntimeRouter,
)
from src.miner.models import IssueCollectionInfo, RootCauseAnalysis, VASCoreInfo
from src.miner.utils.workspace import Workspace
from tests.support.factories import (
    SOURCE,
    root_cause as _root_cause,
    vas_core as _core,
)


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
    workspace = Workspace(
        workspace_root,
        "VAS-0001",
        input_id="CVE-2099-0001",
        output_root=tmp_path / "output",
        trace_id="0123456789abcdef0123456789abcdef",
        rules_dir=tmp_path / "rules",
    )
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
    assert workspace.anchor_review_path.is_file()
    assert workspace.rule_path.is_file()
    assert {path.name for path in workspace.root.iterdir()} == {"src", "cases"}
    assert workspace.cache_dir == (
        tmp_path
        / "output"
        / "miner"
        / "VAS-0001"
        / "CVE-2099-0001"
        / "caches"
    )
    cache_names = {path.name for path in workspace.cache_dir.glob("*.json")}
    assert len(cache_names) == 3
    assert any(name.startswith("issue_collector.") for name in cache_names)
    assert any(name.startswith("root_cause_analyzer.") for name in cache_names)
    assert any(name.startswith("rule_generator.") for name in cache_names)
    assert any("pydantic-ai.sdk-model" in name for name in cache_names)
    assert any("claude-code.cli-model" in name for name in cache_names)
    assert all(
        task.context.trace_id == "0123456789abcdef0123456789abcdef"
        for task in [*sdk.calls, *cli.calls]
    )

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
