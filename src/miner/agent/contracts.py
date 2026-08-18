"""Small runtime-neutral interfaces for executing Miner Agent phases."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from ..models.analysis import GroundingPolicy, RootCauseAnalysis
from ..models.anchors import AnchorPlan

OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentPhase(StrEnum):
    """Closed phase identities used by task construction and diagnostics."""

    ISSUE_COLLECTION = "issue_collection"
    ROOT_CAUSE = "root_cause"
    RULE_GENERATION = "rule_generation"
    AST_GREP_SYNTHESIS = "ast_grep_synthesis"


@dataclass(frozen=True, slots=True)
class IssueCollectionAuthority:
    issue_reference: str

    @property
    def phase(self) -> AgentPhase:
        return AgentPhase.ISSUE_COLLECTION


@dataclass(frozen=True, slots=True)
class RootCauseAuthority:
    source_root: Path
    cases_dir: Path
    grounding_policy: GroundingPolicy
    repo_path: Path | None = None
    fixed_diff: bool = False

    @property
    def phase(self) -> AgentPhase:
        return AgentPhase.ROOT_CAUSE


@dataclass(frozen=True, slots=True)
class RuleGenerationAuthority:
    source_root: Path
    cases_dir: Path
    grounding_policy: GroundingPolicy
    root_cause: RootCauseAnalysis

    @property
    def phase(self) -> AgentPhase:
        return AgentPhase.RULE_GENERATION


@dataclass(frozen=True, slots=True)
class AnchorSynthesisAuthority:
    source_root: Path
    cases_dir: Path
    grounding_policy: GroundingPolicy
    root_cause: RootCauseAnalysis
    plan: AnchorPlan
    target_anchor_id: str
    skill_root: Path

    @property
    def phase(self) -> AgentPhase:
        return AgentPhase.AST_GREP_SYNTHESIS


PhaseAuthority = (
    IssueCollectionAuthority
    | RootCauseAuthority
    | RuleGenerationAuthority
    | AnchorSynthesisAuthority
)
PhaseValidator = Callable[[OutputT, PhaseAuthority, Path], Sequence[str]]


@dataclass(frozen=True, slots=True)
class InstructionLayers:
    """Canonical instructions followed by input policy and one Adapter binding."""

    shared: str
    input_policy: str = ""

    def render(self, runtime_binding: str) -> str:
        rendered = self.shared
        for layer in (self.input_policy, runtime_binding):
            if not layer:
                continue
            separator = "" if rendered.endswith("\n\n") else "\n" if rendered.endswith("\n") else "\n\n"
            rendered += separator + layer
        return rendered if rendered.endswith("\n") else rendered + "\n"


@dataclass(frozen=True, slots=True)
class RunLimits:
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


@dataclass(frozen=True, slots=True)
class PhaseDefinition[OutputT: BaseModel]:
    """Canonical responsibility, instructions, tools, output, and limits for one phase."""

    phase: AgentPhase
    agent_name: str
    description: str
    instructions: str
    output_type: type[OutputT]
    tools: tuple[str, ...]
    limits: RunLimits
    validator: PhaseValidator[OutputT]


@dataclass(frozen=True, slots=True)
class AgentTask[OutputT: BaseModel]:
    """One complete assignment constructed from a closed Phase Authority."""

    task_id: str
    definition: PhaseDefinition[OutputT]
    authority: PhaseAuthority
    prompt: str
    workspace_root: Path
    input_policy: str = ""
    extra_tools: tuple[str, ...] = ()
    limit_override: RunLimits | None = None

    def __post_init__(self) -> None:
        if self.definition.phase is not self.authority.phase:
            raise ValueError(
                f"Phase Definition {self.definition.phase.value!r} does not match "
                f"Authority {self.authority.phase.value!r}"
            )

    @property
    def phase(self) -> AgentPhase:
        return self.authority.phase

    @property
    def agent_name(self) -> str:
        return self.definition.agent_name

    @property
    def description(self) -> str:
        return self.definition.description

    @property
    def output_type(self) -> type[OutputT]:
        return self.definition.output_type

    @property
    def instructions(self) -> InstructionLayers:
        return InstructionLayers(self.definition.instructions, self.input_policy)

    @property
    def tools(self) -> tuple[str, ...]:
        return self.definition.tools + self.extra_tools

    @property
    def limits(self) -> RunLimits:
        return self.limit_override or self.definition.limits

    def validate_output(self, value: OutputT) -> tuple[str, ...]:
        if not isinstance(value, self.output_type):
            return (
                f"output must be {self.output_type.__name__}; received {type(value).__name__}",
            )
        return tuple(self.definition.validator(value, self.authority, self.workspace_root))


@dataclass(frozen=True, slots=True)
class RuntimeUsage:
    requests: int | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    runtime_id: str
    model_id: str


RuntimeEventType = Literal[
    "thinking",
    "message",
    "tool.call",
    "tool.result",
    "compaction",
    "output",
    "error",
]
_RUNTIME_EVENT_TYPES = {
    "thinking",
    "message",
    "tool.call",
    "tool.result",
    "compaction",
    "output",
    "error",
}


@dataclass(frozen=True, slots=True)
class RuntimeLogEvent:
    type: RuntimeEventType
    content: str | dict[str, Any] | list[Any]

    def __post_init__(self) -> None:
        if self.type not in _RUNTIME_EVENT_TYPES:
            raise ValueError(f"unsupported Runtime Log Event type: {self.type}")


@dataclass(frozen=True, slots=True)
class AgentRunResult[OutputT: BaseModel]:
    output: OutputT
    identity: RuntimeIdentity
    usage: RuntimeUsage | None = None
    attempts: int = 1

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be positive")


@runtime_checkable
class AgentRuntime(Protocol):
    """Real Seam implemented by the Pydantic AI and Claude Code Adapters."""

    @property
    def identity(self) -> RuntimeIdentity: ...

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]: ...


__all__ = [
    "AgentPhase",
    "AgentRunResult",
    "AgentRuntime",
    "AgentTask",
    "AnchorSynthesisAuthority",
    "InstructionLayers",
    "IssueCollectionAuthority",
    "OutputT",
    "PhaseAuthority",
    "PhaseDefinition",
    "PhaseValidator",
    "RootCauseAuthority",
    "RuleGenerationAuthority",
    "RunLimits",
    "RuntimeEventType",
    "RuntimeIdentity",
    "RuntimeLogEvent",
    "RuntimeUsage",
]
