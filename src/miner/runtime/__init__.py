"""Public runtime-neutral interfaces for Miner agent execution."""

from .claude_code import ClaudeCodeConfig, ClaudeCodeRuntime
from .contracts import (
    AgentPhase,
    AgentRunResult,
    AgentRuntime,
    AgentTask,
    FileAccess,
    RunLimits,
    RuntimeArtifacts,
    RuntimeCapability,
    RuntimeIdentity,
    RuntimeUsage,
    SkillSpec,
    TaskContext,
    WorkspacePolicy,
)
from .pydantic_ai import PydanticAIRuntime
from .router import (
    RuntimeCapabilityError,
    RuntimeProtocolError,
    RuntimeRouter,
    RuntimeRoutingError,
    UnknownRuntimeError,
)

__all__ = [
    "AgentPhase",
    "AgentRunResult",
    "AgentRuntime",
    "AgentTask",
    "ClaudeCodeConfig",
    "ClaudeCodeRuntime",
    "FileAccess",
    "PydanticAIRuntime",
    "RunLimits",
    "RuntimeArtifacts",
    "RuntimeCapability",
    "RuntimeCapabilityError",
    "RuntimeIdentity",
    "RuntimeProtocolError",
    "RuntimeRouter",
    "RuntimeRoutingError",
    "RuntimeUsage",
    "SkillSpec",
    "TaskContext",
    "UnknownRuntimeError",
    "WorkspacePolicy",
]
