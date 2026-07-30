"""Phase-level selection and capability checking for agent runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .contracts import (
    AgentPhase,
    AgentRunResult,
    AgentRuntime,
    AgentTask,
    OutputT,
    RuntimeIdentity,
)


class RuntimeRoutingError(RuntimeError):
    """Base error for invalid or unsatisfied runtime routing."""


class UnknownRuntimeError(RuntimeRoutingError):
    """Raised when configuration names a runtime that is not registered."""


class RuntimeCapabilityError(RuntimeRoutingError):
    """Raised when a selected runtime cannot honor a task contract."""


class RuntimeProtocolError(RuntimeRoutingError):
    """Raised when an adapter returns a result under a different runtime id."""


@dataclass(frozen=True)
class RuntimeRouter:
    """Choose exactly one runtime at the phase boundary and keep it fixed."""

    runtimes: Mapping[str, AgentRuntime]
    default_runtime: str
    phase_runtimes: Mapping[AgentPhase, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        runtimes = dict(self.runtimes)
        phase_runtimes = dict(self.phase_runtimes)
        if self.default_runtime not in runtimes:
            raise UnknownRuntimeError(f"default runtime is not registered: {self.default_runtime}")
        unknown = sorted(set(phase_runtimes.values()) - set(runtimes))
        if unknown:
            raise UnknownRuntimeError("phase routing references unregistered runtimes: " + ", ".join(unknown))
        object.__setattr__(self, "runtimes", MappingProxyType(runtimes))
        object.__setattr__(self, "phase_runtimes", MappingProxyType(phase_runtimes))

    def runtime_id_for(self, phase: AgentPhase) -> str:
        """Return the configured runtime id for a phase without executing it."""

        return self.phase_runtimes.get(phase, self.default_runtime)

    def resolve(self, task: AgentTask[OutputT]) -> AgentRuntime:
        """Resolve and capability-check the single adapter for a task."""

        runtime_id = self.runtime_id_for(task.phase)
        runtime = self.runtimes[runtime_id]
        missing = task.required_capabilities - runtime.capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise RuntimeCapabilityError(
                f"runtime {runtime_id!r} cannot execute {task.phase.value!r}; missing capabilities: {names}"
            )
        return runtime

    def identity_for(self, task: AgentTask[OutputT]) -> RuntimeIdentity:
        """Return the selected runtime/model identity without executing the task."""

        runtime = self.resolve(task)
        return RuntimeIdentity(
            runtime_id=runtime.runtime_id,
            model_id=runtime.model_id_for(task),
        )

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        """Run a task without fallback or mid-task adapter switching."""

        runtime = self.resolve(task)
        result = await runtime.run(task)
        if result.runtime_id != runtime.runtime_id:
            raise RuntimeProtocolError(
                f"runtime {runtime.runtime_id!r} returned result for {result.runtime_id!r}"
            )
        return result


__all__ = [
    "RuntimeCapabilityError",
    "RuntimeProtocolError",
    "RuntimeRouter",
    "RuntimeRoutingError",
    "UnknownRuntimeError",
]
