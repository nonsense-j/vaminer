"""Two real Input Adapters converging on one Prepared Analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..agent.contracts import AgentRunResult, AgentRuntime, AgentTask
from ..models.analysis import GroundingPolicy
from ..models.issue import IssueCollectionInfo
from ..utils.cache import AgentCache, load_agent_cache
from ..utils.log import logger
from ..utils.workspace import Workspace
from .examples import (
    ExampleSuiteInspection,
    ExampleSuiteIntake,
    inspect_example_suite,
    materialize_example_suite,
)
from .tasks import make_issue_collection_task


class IssueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(..., min_length=1)


class ExampleSuiteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path


MiningInput = IssueInput | ExampleSuiteInput
InputT = TypeVar("InputT")


@dataclass(frozen=True, slots=True)
class PreparedAnalysis:
    input_id: str
    source_root: Path
    grounding_policy: GroundingPolicy
    source: IssueCollectionInfo | ExampleSuiteIntake
    source_label: str


@dataclass(slots=True)
class InputRun:
    runtime: AgentRuntime
    workspace: Workspace
    use_cache: bool

    def cache(self, task: AgentTask[Any]) -> AgentCache:
        identity = self.runtime.identity
        return AgentCache(
            task.agent_name,
            self.workspace.cache_dir,
            runtime=identity.runtime_id,
            model=identity.model_id,
        )

    def load(self, task: AgentTask[Any]) -> BaseModel | None:
        if not self.use_cache:
            return None
        return load_agent_cache(
            self.cache(task),
            task.output_type,
            task.validate_output,
            label=task.agent_name,
        )

    async def execute(self, task: AgentTask[Any]) -> AgentRunResult[Any]:
        identity = self.runtime.identity
        logger.info(
            "Agent task selected: phase=%s task=%s runtime=%s model=%s",
            task.phase.value,
            task.task_id,
            identity.runtime_id,
            identity.model_id,
        )
        result = await self.runtime.run(task)
        if result.identity != identity:
            raise RuntimeError("Runtime Adapter identity drifted during Agent execution")
        usage = result.usage
        logger.info(
            "Agent task completed: phase=%s attempts=%s requests=%s turns=%s input_tokens=%s output_tokens=%s",
            task.phase.value,
            result.attempts,
            usage.requests if usage else None,
            usage.turns if usage else None,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
        )
        return result

    async def load_or_execute(self, task: AgentTask[Any]) -> BaseModel:
        cached = self.load(task)
        if cached is not None:
            logger.info("%s output loaded from cache: %s", task.agent_name, self.cache(task).path)
            return cached
        result = await self.execute(task)
        self.cache(task).set(result.output)
        return result.output


class InputAdapter(Protocol[InputT]):
    async def prepare(self, value: InputT, run: InputRun) -> PreparedAnalysis: ...


class IssueInputAdapter:
    @staticmethod
    def resolve(value: IssueInput, *, workspace_dir: Path) -> tuple[str, str, IssueInput]:
        vas_id = Workspace.get_vas_id(value.reference, base_dir=workspace_dir)
        return vas_id, value.reference, value

    async def prepare(self, value: IssueInput, run: InputRun) -> PreparedAnalysis:
        task = make_issue_collection_task(value.reference, workspace_root=run.workspace.root)
        collection = await run.load_or_execute(task)
        assert isinstance(collection, IssueCollectionInfo)
        return PreparedAnalysis(
            input_id=value.reference,
            source_root=Path(collection.repo_path).resolve(),
            grounding_policy=GroundingPolicy.REPOSITORY_EVIDENCE,
            source=collection,
            source_label="Repository",
        )


class ExampleSuiteInputAdapter:
    @staticmethod
    def resolve(
        value: ExampleSuiteInput,
        *,
        workspace_dir: Path,
    ) -> tuple[str, str, ExampleSuiteInspection]:
        inspection = inspect_example_suite(value.path)
        vas_id = Workspace.prepare_example_suite_vas_id(
            inspection.registry_key,
            content_digest=inspection.content_digest,
            base_dir=workspace_dir,
        )
        return vas_id, inspection.registry_key, inspection

    async def prepare(self, value: ExampleSuiteInspection, run: InputRun) -> PreparedAnalysis:
        intake = materialize_example_suite(value, workspace=run.workspace)
        Workspace.register_example_suite(
            value.registry_key,
            vas_id=run.workspace.vas_id,
            content_digest=value.content_digest,
            base_dir=run.workspace.root.parent,
        )
        return PreparedAnalysis(
            input_id=value.registry_key,
            source_root=Path(intake.snapshot_path).resolve(),
            grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
            source=intake,
            source_label="Example Suite Snapshot",
        )


__all__ = [
    "ExampleSuiteInput",
    "ExampleSuiteInputAdapter",
    "InputAdapter",
    "InputRun",
    "IssueInput",
    "IssueInputAdapter",
    "MiningInput",
    "PreparedAnalysis",
]
