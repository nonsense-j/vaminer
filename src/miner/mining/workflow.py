"""One deep VAMiner Module for every supported Mining Input."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..agent.contracts import AgentRuntime
from ..anchors.review import review_anchors
from ..models.analysis import RootCauseAnalysis
from ..models.issue import IssueCollectionInfo
from ..models.vas import ExampleSuiteVASSource, IssueVASSource, VASCoreInfo, VASFull
from ..utils.config import MINER_OUTPUT_DIR, VAS_RULES_DIR, VAS_WORKSPACE_DIR
from ..utils.log import logger, run_log_file
from ..utils.telemetry import trace_pipeline
from ..utils.workspace import Workspace
from .examples import ExampleSuiteInspection
from .inputs import (
    ExampleSuiteInputAdapter,
    InputRun,
    IssueInput,
    IssueInputAdapter,
    MiningInput,
    PreparedAnalysis,
)
from .tasks import make_root_cause_task, make_rule_generation_task


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    use_cache: bool = False
    workspace_dir: Path = VAS_WORKSPACE_DIR
    output_dir: Path = MINER_OUTPUT_DIR
    rules_dir: Path = VAS_RULES_DIR
    log_dir: Path | None = None


def _assemble_vas(vas_id: str, prepared: PreparedAnalysis, core: VASCoreInfo) -> VASFull:
    if isinstance(prepared.source, IssueCollectionInfo):
        source = IssueVASSource(
            issue_id=prepared.source.issue_id,
            repo_url=prepared.source.repo_url,
            buggy_commit=prepared.source.buggy_commit,
            fixed_commit=prepared.source.fixed_commit,
            root_cause_summary=core.root_cause_summary,
        )
    else:
        source = ExampleSuiteVASSource(
            registry_key=prepared.source.registry_key,
            suite_name=prepared.source.suite_name,
            content_digest=prepared.source.content_digest,
            snapshot_ref=prepared.source.snapshot_ref,
            files=prepared.source.files,
            root_cause_summary=core.root_cause_summary,
        )
    return VASFull(
        vas_id=vas_id,
        category=core.category,
        language=core.language,
        sources=[source],
        summary=core.summary,
        scenarios=core.scenarios,
        anchors=core.anchors,
    )


class VAMiner:
    """Hide input preparation, phase ordering, cache, validation, review, and persistence."""

    def __init__(self, runtime: AgentRuntime, *, options: WorkflowOptions | None = None) -> None:
        self.runtime = runtime
        self.options = options or WorkflowOptions()
        self._issue_inputs = IssueInputAdapter()
        self._example_inputs = ExampleSuiteInputAdapter()

    def _resolve(self, value: MiningInput) -> tuple[str, str, IssueInput | ExampleSuiteInspection]:
        workspace_dir = self.options.workspace_dir.expanduser().resolve()
        if isinstance(value, IssueInput):
            return self._issue_inputs.resolve(value, workspace_dir=workspace_dir)
        return self._example_inputs.resolve(value, workspace_dir=workspace_dir)

    async def _prepare(
        self,
        value: IssueInput | ExampleSuiteInspection,
        run: InputRun,
    ) -> PreparedAnalysis:
        if isinstance(value, IssueInput):
            return await self._issue_inputs.prepare(value, run)
        return await self._example_inputs.prepare(value, run)

    async def _mine_in_workspace(
        self,
        value: IssueInput | ExampleSuiteInspection,
        *,
        workspace: Workspace,
    ) -> VASFull:
        run = InputRun(runtime=self.runtime, workspace=workspace, use_cache=self.options.use_cache)
        prepared = await self._prepare(value, run)

        root_task = make_root_cause_task(
            prepared.source,
            workspace_root=workspace.root,
            source_root=prepared.source_root,
            cases_dir=workspace.cases_dir,
            grounding_policy=prepared.grounding_policy,
        )
        root_cause = run.load(root_task)
        if root_cause is None:
            workspace.clear_cases()
            try:
                root_cause = (await run.execute(root_task)).output
            except BaseException:
                workspace.clear_cases()
                raise
            run.cache(root_task).set(root_cause)
        assert isinstance(root_cause, RootCauseAnalysis)

        rule_task = make_rule_generation_task(
            root_cause,
            workspace_root=workspace.root,
            source_root=prepared.source_root,
            cases_dir=workspace.cases_dir,
            grounding_policy=prepared.grounding_policy,
        )
        core = run.load(rule_task)
        if core is None:
            core = (await run.execute(rule_task)).output
            run.cache(rule_task).set(core)
        assert isinstance(core, VASCoreInfo)

        review_anchors(
            workspace.vas_id,
            core,
            prepared.source_root,
            workspace.cases_dir,
            output_path=workspace.anchor_review_path,
            source_label=prepared.source_label,
            root_cause=root_cause,
            grounding_policy=prepared.grounding_policy,
        )
        vas = _assemble_vas(workspace.vas_id, prepared, core)
        logger.info("VAS saved: %s", workspace.save_rule(vas))
        return vas

    async def mine(self, value: MiningInput) -> VASFull:
        output_dir = self.options.output_dir.expanduser().resolve()
        with trace_pipeline(
            mining_input=value,
            runtime_id=self.runtime.identity.runtime_id,
        ) as pipeline:
            vas_id, input_id, resolved = self._resolve(value)
            with pipeline.bind(vas_id):
                workspace = Workspace.from_id(
                    vas_id,
                    base_dir=self.options.workspace_dir.expanduser().resolve(),
                    input_id=input_id,
                    output_root=output_dir,
                    trace_id=pipeline.trace_id,
                    rules_dir=self.options.rules_dir.expanduser().resolve(),
                )
                log_root = self.options.log_dir or output_dir / "logs" / "miner"
                with run_log_file(
                    log_root.expanduser().resolve(),
                    vas_id,
                    input_id=workspace.input_id,
                    trace_id=pipeline.trace_id,
                    runtime=self.runtime.identity.runtime_id,
                ) as log_path:
                    logger.info("Starting VAMiner input=%s runtime=%s", input_id, self.runtime.identity.runtime_id)
                    logger.info("Trace ID: %s; run log: %s", pipeline.trace_id, log_path)
                    vas = await self._mine_in_workspace(resolved, workspace=workspace)
                    pipeline.update(output=vas.model_dump(mode="json"))
                    return vas


__all__ = ["VAMiner", "WorkflowOptions"]
