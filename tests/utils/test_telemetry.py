"""Tests for runtime-neutral workflow tracing."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from src.miner.utils import telemetry


class FakeLangfuse:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.observation = FakeObservation()

    @contextmanager
    def start_as_current_observation(self, **values: Any):
        self.started.append(values)
        yield self.observation


class FakeObservation:
    trace_id = "0123456789abcdef0123456789abcdef"

    def update(self, **values: Any) -> None:
        self.updated = values


@pytest.mark.parametrize(
    ("runtime_ids", "expected_name", "expected_metadata"),
    [
        (
            ("pydantic-ai",),
            "VAS-0001 Miner Workflow @pydantic-ai",
            ("pydantic-ai",),
        ),
        (
            ("pydantic-ai", "claude-code", "pydantic-ai"),
            "VAS-0001 Miner Workflow @pydantic-ai+claude-code",
            ("pydantic-ai", "claude-code"),
        ),
    ],
)
def test_pipeline_trace_name_includes_routed_runtimes(
    monkeypatch,
    runtime_ids: tuple[str, ...],
    expected_name: str,
    expected_metadata: tuple[str, ...],
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with telemetry.trace_pipeline(
        issue_input="CVE-2099-0001",
        vas_id="VAS-0001",
        runtime_ids=runtime_ids,
    ) as pipeline_trace:
        assert pipeline_trace.observation is langfuse.observation
        assert pipeline_trace.trace_id == langfuse.observation.trace_id

    started = langfuse.started[0]
    assert started["name"] == expected_name
    assert started["as_type"] == "chain"
    assert started["metadata"] == {
        "vas_id": "VAS-0001",
        "runtimes": expected_metadata,
    }
