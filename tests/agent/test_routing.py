"""Tests for runtime-neutral phase routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.miner.agent import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    RuntimeCapability,
    RuntimeCapabilityError,
    RuntimeRouter,
    TaskContext,
    WorkspacePolicy,
)


class ProbeOutput(BaseModel):
    value: str


@dataclass
class FakeRuntime:
    runtime_id: str
    capabilities: frozenset[RuntimeCapability]
    calls: list[AgentTask] = field(default_factory=list)

    async def run(self, task):
        self.calls.append(task)
        return AgentRunResult(
            output=ProbeOutput(value=self.runtime_id),
            runtime_id=self.runtime_id,
            model_id="fixture-model",
        )


def _probe_task(
    *,
    phase: AgentPhase,
    required: frozenset[RuntimeCapability],
) -> AgentTask[ProbeOutput]:
    root = Path("/tmp/runtime-contract-probe")
    return AgentTask(
        task_id="probe",
        phase=phase,
        agent_name="Probe",
        description="Probe routing.",
        instructions="Return the probe output.",
        prompt="probe",
        output_type=ProbeOutput,
        context=TaskContext(workspace_root=root),
        workspace=WorkspacePolicy(cwd=root),
        required_capabilities=required,
    )


async def test_router_selects_one_runtime_for_the_phase():
    sdk = FakeRuntime(
        "pydantic-ai",
        frozenset({RuntimeCapability.STRUCTURED_OUTPUT}),
    )
    claude = FakeRuntime(
        "claude-code",
        frozenset(
            {
                RuntimeCapability.STRUCTURED_OUTPUT,
                RuntimeCapability.WORKSPACE_READ,
            }
        ),
    )
    router = RuntimeRouter(
        runtimes={sdk.runtime_id: sdk, claude.runtime_id: claude},
        default_runtime=sdk.runtime_id,
        phase_runtimes={AgentPhase.ROOT_CAUSE: claude.runtime_id},
    )
    task = _probe_task(
        phase=AgentPhase.ROOT_CAUSE,
        required=frozenset(
            {
                RuntimeCapability.STRUCTURED_OUTPUT,
                RuntimeCapability.WORKSPACE_READ,
            }
        ),
    )

    result = await router.run(task)

    assert result.runtime_id == "claude-code"
    assert sdk.calls == []
    assert claude.calls == [task]


def test_router_rejects_missing_capabilities_without_fallback():
    sdk = FakeRuntime(
        "pydantic-ai",
        frozenset({RuntimeCapability.STRUCTURED_OUTPUT}),
    )
    fallback = FakeRuntime("claude-code", frozenset(RuntimeCapability))
    router = RuntimeRouter(
        runtimes={sdk.runtime_id: sdk, fallback.runtime_id: fallback},
        default_runtime=sdk.runtime_id,
    )
    task = _probe_task(
        phase=AgentPhase.ROOT_CAUSE,
        required=frozenset(
            {
                RuntimeCapability.STRUCTURED_OUTPUT,
                RuntimeCapability.WORKSPACE_READ,
            }
        ),
    )

    with pytest.raises(RuntimeCapabilityError, match="workspace_read"):
        router.resolve(task)
    assert fallback.calls == []
