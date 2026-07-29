"""Runtime-neutral contracts for executing Miner agent phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from ..utils.models import AnalysisSubject, AnchorSynthesisRunRequest, RootCauseAnalysis

OutputT = TypeVar("OutputT", bound=BaseModel)
OutputValidator = Callable[[OutputT, "TaskContext"], Sequence[str]]


class AgentPhase(StrEnum):
    """Stable phase identities used for routing, cache names, and diagnostics."""

    ISSUE_COLLECTION = "issue_collection"
    ROOT_CAUSE = "root_cause"
    RULE_GENERATION = "rule_generation"


class RuntimeCapability(StrEnum):
    """Semantic capabilities a task may require from a runtime adapter."""

    STRUCTURED_OUTPUT = "structured_output"
    ISSUE_RESEARCH = "issue_research"
    WEB_RESEARCH = "web_research"
    REPOSITORY_CHECKOUT = "repository_checkout"
    REPOSITORY_READ = "repository_read"
    FIXED_DIFF = "fixed_diff"
    CASES_READ = "cases_read"
    CASES_WRITE = "cases_write"
    AST_GREP = "ast_grep"
    SKILLS = "skills"
    AGENT_DELEGATION = "agent_delegation"


class FileAccess(StrEnum):
    """Required access to one task-owned filesystem area."""

    NONE = "none"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class WorkspacePolicy:
    """Cross-runtime workspace, network, and shell requirements for one task."""

    cwd: Path
    repository: FileAccess = FileAccess.NONE
    cases: FileAccess = FileAccess.NONE
    allow_network: bool = False
    allow_shell: bool = False


@dataclass(frozen=True)
class SkillSpec:
    """A repository-owned skill that an adapter must expose without global install."""

    name: str
    root: Path


@dataclass(frozen=True)
class RunLimits:
    """Logical run limits interpreted by each runtime adapter."""

    request_limit: int | None = None
    timeout_seconds: float | None = None
    output_retries: int = 2

    def __post_init__(self) -> None:
        if self.request_limit is not None and self.request_limit < 1:
            raise ValueError("request_limit must be positive when provided")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")
        if self.output_retries < 0:
            raise ValueError("output_retries must be non-negative")


@dataclass(frozen=True)
class TaskContext:
    """Typed filesystem and domain state made available to one isolated task."""

    workspace_root: Path
    source_root: Path | None = None
    repo_path: Path | None = None
    cases_dir: Path | None = None
    root_cause: RootCauseAnalysis | None = None
    analysis_subject: AnalysisSubject | None = None
    anchor_run_request: AnchorSynthesisRunRequest | None = None


@dataclass(frozen=True)
class AgentTask[OutputT: BaseModel]:
    """One runtime-neutral, structured-output agent invocation."""

    task_id: str
    phase: AgentPhase
    agent_name: str
    description: str
    instructions: str
    prompt: str
    output_type: type[OutputT]
    context: TaskContext
    workspace: WorkspacePolicy
    required_capabilities: frozenset[RuntimeCapability]
    limits: RunLimits = field(default_factory=RunLimits)
    skills: tuple[SkillSpec, ...] = ()
    model_hint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    output_validator: OutputValidator[OutputT] | None = None

    def validate_output(self, value: OutputT) -> tuple[str, ...]:
        """Run the phase's deterministic post-schema validation."""

        if not isinstance(value, self.output_type):
            return (
                f"output must be {self.output_type.__name__}; received {type(value).__name__}",
            )
        if self.output_validator is None:
            return ()
        return tuple(self.output_validator(value, self.context))


@dataclass(frozen=True)
class RuntimeUsage:
    """Provider-independent usage values; unavailable fields remain null."""

    requests: int | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cost_usd: Decimal | None = None
    duration_ms: int | None = None
    model_usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeArtifacts:
    """Optional raw artifacts retained by subprocess or SDK adapters."""

    final_text: str | None = None
    prompt_path: Path | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    events_path: Path | None = None
    invocation_path: Path | None = None


@dataclass(frozen=True)
class RuntimeIdentity:
    """Stable pre-run identity used by cache keys and routing diagnostics."""

    runtime_id: str
    model_id: str


@dataclass(frozen=True)
class AgentRunResult[OutputT: BaseModel]:
    """Normalized successful result returned by every runtime adapter."""

    output: OutputT
    runtime_id: str
    model_id: str
    usage: RuntimeUsage | None = None
    artifacts: RuntimeArtifacts = field(default_factory=RuntimeArtifacts)
    attempts: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be positive")


@runtime_checkable
class AgentRuntime(Protocol):
    """Adapter seam implemented by Pydantic AI and agent CLI runtimes."""

    runtime_id: str
    capabilities: frozenset[RuntimeCapability]

    def model_id_for(self, task: AgentTask[Any]) -> str: ...

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]: ...


__all__ = [
    "AgentPhase",
    "AgentRunResult",
    "AgentRuntime",
    "AgentTask",
    "FileAccess",
    "OutputT",
    "OutputValidator",
    "RunLimits",
    "RuntimeArtifacts",
    "RuntimeCapability",
    "RuntimeIdentity",
    "RuntimeUsage",
    "SkillSpec",
    "TaskContext",
    "WorkspacePolicy",
]
