from pathlib import Path

import pytest
from git import Repo

from src.miner.agent import AgentPhase, AgentRunResult, RuntimeIdentity
from src.miner.mining.inputs import ExampleSuiteInput, IssueInput
from src.miner.mining.workflow import VAMiner, WorkflowOptions
from src.miner.models import (
    Anchor,
    AstGrepLanguage,
    BuggyComponent,
    IssueCategory,
    IssueCollectionInfo,
    RootCauseAnalysis,
    Scenarios,
    VASCoreInfo,
)


class ScriptedRuntime:
    def __init__(self) -> None:
        self.phases = []

    @property
    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity("scripted", "model")

    async def run(self, task):
        self.phases.append(task.phase)
        if task.phase is AgentPhase.ISSUE_COLLECTION:
            repo_path = task.workspace_root / "src" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            (repo_path / "bug.c").write_text("copy();\n", encoding="utf-8")
            repo = Repo.init(repo_path)
            repo.index.add(["bug.c"])
            commit = repo.index.commit("buggy")
            output = IssueCollectionInfo(
                issue_id="CVE-2099-0001",
                issue_summary="copy issue",
                issue_details="details",
                repo_url="https://example.invalid/repo.git",
                buggy_commit=commit.hexsha,
                repo_path=str(repo_path),
            )
        elif task.phase is AgentPhase.ROOT_CAUSE:
            source_files = sorted(task.authority.source_root.rglob("*.c"))
            source_file = source_files[0]
            relative = source_file.relative_to(task.authority.source_root).as_posix()
            task.authority.cases_dir.mkdir(exist_ok=True)
            (task.authority.cases_dir / "case1.c").write_text("copy();\n", encoding="utf-8")
            output = RootCauseAnalysis(
                language=AstGrepLanguage.C,
                root_cause_summary="unchecked copy",
                analysis="length reaches copy",
                buggy_components=[
                    BuggyComponent(file=relative, start_line=1, end_line=1, role="copy", snippet="copy();")
                ],
                fixing_pattern="bound length",
                extracted_case_files=["case1.c"],
            )
        else:
            output = VASCoreInfo(
                category=IssueCategory.SECURITY,
                language=AstGrepLanguage.C,
                root_cause_summary=task.authority.root_cause.root_cause_summary,
                summary="detect copy",
                scenarios=Scenarios(unsafe=["unchecked"], safe=["checked"]),
                anchors=[
                    Anchor(
                        id="copy-site",
                        behavior_weight=3,
                        query_weight=1,
                        type="pattern",
                        query="",
                        behavior="copy",
                        inspect_hint="bound",
                    )
                ],
            )
        return AgentRunResult(output=output, identity=self.identity)


def _options(tmp_path: Path, *, cache: bool = False) -> WorkflowOptions:
    return WorkflowOptions(
        use_cache=cache,
        workspace_dir=tmp_path / "workspaces",
        output_dir=tmp_path / "output",
        rules_dir=tmp_path / "rules",
    )


@pytest.mark.asyncio
async def test_issue_and_example_inputs_share_one_post_prepare_workflow(tmp_path: Path):
    issue_runtime = ScriptedRuntime()
    issue_vas = await VAMiner(issue_runtime, options=_options(tmp_path / "issue")).mine(
        IssueInput(reference="CVE-2099-0001")
    )
    assert issue_runtime.phases == [AgentPhase.ISSUE_COLLECTION, AgentPhase.ROOT_CAUSE, AgentPhase.RULE_GENERATION]
    assert issue_vas.sources[0].type == "issue"

    suite = tmp_path / "suite" / "CWE-120"
    suite.mkdir(parents=True)
    (suite / "bad.c").write_text("copy();\n", encoding="utf-8")
    example_runtime = ScriptedRuntime()
    example_vas = await VAMiner(example_runtime, options=_options(tmp_path / "example")).mine(
        ExampleSuiteInput(path=suite)
    )
    assert example_runtime.phases == [AgentPhase.ROOT_CAUSE, AgentPhase.RULE_GENERATION]
    assert example_vas.sources[0].type == "example_suite"


@pytest.mark.asyncio
async def test_cache_uses_runtime_identity_and_skips_agent_runs(tmp_path: Path):
    runtime = ScriptedRuntime()
    miner = VAMiner(runtime, options=_options(tmp_path, cache=True))
    await miner.mine(IssueInput(reference="CVE-2099-0001"))
    runtime.phases.clear()
    await miner.mine(IssueInput(reference="CVE-2099-0001"))
    assert runtime.phases == []


@pytest.mark.asyncio
async def test_workflow_owns_root_cause_failure_case_cleanup(tmp_path: Path):
    class FailingRuntime:
        cases_dir: Path | None = None

        @property
        def identity(self) -> RuntimeIdentity:
            return RuntimeIdentity("failing", "model")

        async def run(self, task):
            assert task.phase is AgentPhase.ROOT_CAUSE
            self.cases_dir = task.authority.cases_dir
            (self.cases_dir / "partial.c").write_text("partial();\n", encoding="utf-8")
            raise RuntimeError("root cause failed")

    suite = tmp_path / "suite" / "CWE-120"
    suite.mkdir(parents=True)
    (suite / "bad.c").write_text("copy();\n", encoding="utf-8")
    runtime = FailingRuntime()

    with pytest.raises(RuntimeError, match="root cause failed"):
        await VAMiner(runtime, options=_options(tmp_path)).mine(ExampleSuiteInput(path=suite))

    assert runtime.cases_dir is not None
    assert list(runtime.cases_dir.iterdir()) == []
