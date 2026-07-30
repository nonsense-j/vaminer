"""Shared post-RCA workflow for issue and Example suite inputs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent import AgentRunResult, AgentTask, RuntimeIdentity, RuntimeRouter
from ..anchors.review import review_anchors
from ..models.analysis import AnalysisSubject, RootCauseAnalysis
from ..models.issue import IssueCollectionInfo
from ..models.vas import (
    ExampleSuiteVASSource,
    IssueVASSource,
    VASCoreInfo,
    VASFull,
)
from ..utils.log import logger
from ..utils.cache import (
    AgentCache,
    load_collection_cache,
    load_root_cause_cache,
    load_rule_cache,
)
from ..utils.workspace import Workspace
from .examples import ExampleSuiteIntake
from .tasks import (
    make_example_suite_root_cause_task,
    make_issue_collection_task,
    make_root_cause_task,
    make_rule_generation_task,
)


@dataclass(frozen=True)
class WorkflowOptions:
    """Application controls independent of the selected model runtime."""

    use_cache: bool = False


@dataclass(frozen=True)
class IssueWorkflowResult:
    collection: IssueCollectionInfo
    root_cause: RootCauseAnalysis
    core: VASCoreInfo
    vas: VASFull
    agent_runs: tuple[AgentRunResult[Any], ...]


@dataclass(frozen=True)
class ExampleSuiteWorkflowResult:
    intake: ExampleSuiteIntake
    root_cause: RootCauseAnalysis
    core: VASCoreInfo
    vas: VASFull
    agent_runs: tuple[AgentRunResult[Any], ...]


def assemble_issue_vas(
    vas_id: str,
    collection: IssueCollectionInfo,
    core: VASCoreInfo,
) -> VASFull:
    return VASFull(
        vas_id=vas_id,
        category=core.category,
        language=core.language,
        sources=[
            IssueVASSource(
                issue_id=collection.issue_id,
                repo_url=collection.repo_url,
                buggy_commit=collection.buggy_commit,
                fixed_commit=collection.fixed_commit,
                root_cause_summary=core.root_cause_summary,
            )
        ],
        summary=core.summary,
        scenarios=core.scenarios,
        anchors=core.anchors,
    )


def assemble_example_suite_vas(
    vas_id: str,
    intake: ExampleSuiteIntake,
    core: VASCoreInfo,
) -> VASFull:
    """Publish portable example-suite provenance without host paths."""

    return VASFull(
        vas_id=vas_id,
        category=core.category,
        language=core.language,
        sources=[
            ExampleSuiteVASSource(
                registry_key=intake.registry_key,
                suite_name=intake.suite_name,
                content_digest=intake.content_digest,
                snapshot_ref=intake.snapshot_ref,
                files=intake.files,
                root_cause_summary=core.root_cause_summary,
            )
        ],
        summary=core.summary,
        scenarios=core.scenarios,
        anchors=core.anchors,
    )


class _WorkflowBase:
    def __init__(
        self,
        router: RuntimeRouter,
        *,
        options: WorkflowOptions | None = None,
    ) -> None:
        self.router = router
        self.options = options or WorkflowOptions()

    def _identity(self, task: AgentTask[Any]) -> RuntimeIdentity:
        identity = self.router.identity_for(task)
        logger.info(
            "Agent task selected: phase=%s task=%s runtime=%s model=%s",
            task.phase.value,
            task.task_id,
            identity.runtime_id,
            identity.model_id,
        )
        return identity

    @staticmethod
    def _cache(task: AgentTask[Any], cache_dir: Path, identity: RuntimeIdentity) -> AgentCache:
        return AgentCache(
            task.agent_name,
            cache_dir,
            runtime=identity.runtime_id,
            model=identity.model_id,
        )

    async def _run_task(self, task: AgentTask[Any]) -> AgentRunResult[Any]:
        result = await self.router.run(task)
        usage = result.usage
        logger.info(
            "Agent task completed: phase=%s task=%s runtime=%s model=%s attempts=%s "
            "requests=%s turns=%s input_tokens=%s output_tokens=%s",
            task.phase.value,
            task.task_id,
            result.runtime_id,
            result.model_id,
            result.attempts,
            usage.requests if usage is not None else None,
            usage.turns if usage is not None else None,
            usage.input_tokens if usage is not None else None,
            usage.output_tokens if usage is not None else None,
        )
        return result

    async def _run_post_rca(
        self,
        *,
        root_cause: RootCauseAnalysis,
        subject: AnalysisSubject,
        source_root: Path,
        repo_path: Path | None,
        vas_id: str,
        workspace: Workspace,
        assemble: Callable[[VASCoreInfo], VASFull],
        source_label: str,
    ) -> tuple[VASCoreInfo, VASFull, tuple[AgentRunResult[Any], ...]]:
        """Own generation, cache identity, validation, review, and persistence."""

        rule_task = make_rule_generation_task(
            root_cause,
            workspace_root=workspace.root,
            source_root=source_root,
            repo_path=repo_path,
            cases_dir=workspace.cases_dir,
            analysis_subject=subject,
            output_root=workspace.output_root,
            input_id=workspace.input_id,
            trace_id=workspace.trace_id,
        )
        identity = self._identity(rule_task)
        cache = self._cache(rule_task, workspace.cache_dir, identity)
        core = (
            load_rule_cache(
                cache,
                repo_path=repo_path,
                source_root=source_root,
                cases_dir=workspace.cases_dir,
                root_cause=root_cause,
                analysis_subject=subject,
            )
            if self.options.use_cache
            else None
        )
        agent_runs: list[AgentRunResult[Any]] = []
        if core is None:
            result = await self._run_task(rule_task)
            if result.runtime_id != identity.runtime_id or result.model_id != identity.model_id:
                raise RuntimeError("Rule Generator runtime/model identity drifted during delegation")
            agent_runs.append(result)
            core = result.output
            cache.set(core)
        else:
            logger.info("Rule Generator output loaded from cache: %s", cache.path)

        review_anchors(
            workspace.vas_id,
            core,
            source_root,
            workspace.cases_dir,
            output_path=workspace.anchor_review_path,
            source_label=source_label,
            root_cause=root_cause,
            analysis_subject=subject,
        )
        vas = assemble(core)
        rule_path = workspace.save_rule(vas)
        logger.info("VAS saved: %s", rule_path)
        return core, vas, tuple(agent_runs)


class IssueWorkflow(_WorkflowBase):
    """Issue-specific collection and RCA followed by the shared post-RCA path."""

    async def run(
        self,
        issue_input: str,
        *,
        vas_id: str,
        workspace: Workspace,
    ) -> IssueWorkflowResult:
        agent_runs: list[AgentRunResult[Any]] = []
        issue_task = make_issue_collection_task(
            issue_input,
            workspace_root=workspace.root,
            output_root=workspace.output_root,
            input_id=workspace.input_id,
            trace_id=workspace.trace_id,
        )
        issue_identity = self._identity(issue_task)
        issue_cache = self._cache(issue_task, workspace.cache_dir, issue_identity)
        collection = (
            load_collection_cache(issue_cache, workspace_root=workspace.root)
            if self.options.use_cache
            else None
        )
        if collection is None:
            issue_result = await self._run_task(issue_task)
            agent_runs.append(issue_result)
            collection = issue_result.output
            issue_cache.set(collection)

        repo_path = Path(collection.repo_path).resolve()
        root_task = make_root_cause_task(
            collection,
            workspace_root=workspace.root,
            repo_path=repo_path,
            cases_dir=workspace.cases_dir,
            output_root=workspace.output_root,
            input_id=workspace.input_id,
            trace_id=workspace.trace_id,
        )
        root_identity = self._identity(root_task)
        root_cache = self._cache(root_task, workspace.cache_dir, root_identity)
        root_cause = (
            load_root_cause_cache(
                root_cache,
                source_root=repo_path,
                cases_dir=workspace.cases_dir,
            )
            if self.options.use_cache
            else None
        )
        if root_cause is None:
            workspace.clear_cases()
            root_result = await self._run_task(root_task)
            agent_runs.append(root_result)
            root_cause = root_result.output
            root_cache.set(root_cause)
        assert root_task.context.analysis_subject is not None
        core, vas, post_runs = await self._run_post_rca(
            root_cause=root_cause,
            subject=root_task.context.analysis_subject,
            source_root=repo_path,
            repo_path=repo_path,
            vas_id=vas_id,
            workspace=workspace,
            assemble=lambda generated: assemble_issue_vas(vas_id, collection, generated),
            source_label="Repository",
        )
        return IssueWorkflowResult(
            collection=collection,
            root_cause=root_cause,
            core=core,
            vas=vas,
            agent_runs=tuple(agent_runs) + post_runs,
        )


class ExampleSuiteWorkflow(_WorkflowBase):
    """Example-suite intake/RCA followed by the shared post-RCA path."""

    async def run(
        self,
        intake: ExampleSuiteIntake,
        *,
        vas_id: str,
        workspace: Workspace,
    ) -> ExampleSuiteWorkflowResult:
        source_root = Path(intake.snapshot_path).resolve()
        root_task = make_example_suite_root_cause_task(
            intake,
            workspace_root=workspace.root,
            source_root=source_root,
            cases_dir=workspace.cases_dir,
            output_root=workspace.output_root,
            input_id=workspace.input_id,
            trace_id=workspace.trace_id,
        )
        identity = self._identity(root_task)
        cache = self._cache(root_task, workspace.cache_dir, identity)
        root_cause = (
            load_root_cause_cache(
                cache,
                source_root=source_root,
                cases_dir=workspace.cases_dir,
            )
            if self.options.use_cache
            else None
        )
        agent_runs: list[AgentRunResult[Any]] = []
        if root_cause is None:
            workspace.clear_cases()
            result = await self._run_task(root_task)
            agent_runs.append(result)
            root_cause = result.output
            cache.set(root_cause)
        assert root_task.context.analysis_subject is not None
        core, vas, post_runs = await self._run_post_rca(
            root_cause=root_cause,
            subject=root_task.context.analysis_subject,
            source_root=source_root,
            repo_path=None,
            vas_id=vas_id,
            workspace=workspace,
            assemble=lambda generated: assemble_example_suite_vas(vas_id, intake, generated),
            source_label="Example Suite Snapshot",
        )
        return ExampleSuiteWorkflowResult(
            intake=intake,
            root_cause=root_cause,
            core=core,
            vas=vas,
            agent_runs=tuple(agent_runs) + post_runs,
        )


__all__ = [
    "ExampleSuiteWorkflow",
    "ExampleSuiteWorkflowResult",
    "IssueWorkflow",
    "IssueWorkflowResult",
    "WorkflowOptions",
    "assemble_example_suite_vas",
    "assemble_issue_vas",
]
