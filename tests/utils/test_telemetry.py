"""Tests for runtime-neutral workflow tracing."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
)
from pydantic import BaseModel

from src.miner.agent import (
    AgentPhase,
    AgentTask,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.runtimes.claude import telemetry as claude_telemetry
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


def test_w3c_trace_context_round_trips_through_subprocess_environment():
    source_context = SpanContext(
        trace_id=int("0123456789abcdef0123456789abcdef", 16),
        span_id=int("0123456789abcdef", 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )

    with trace.use_span(
        NonRecordingSpan(source_context),
        end_on_exit=False,
    ):
        environment = telemetry.propagated_trace_environment()

    assert environment["VAMINER_OTEL_TRACEPARENT"].startswith(
        "00-0123456789abcdef0123456789abcdef-0123456789abcdef-"
    )
    with telemetry.use_propagated_trace_environment(environment):
        restored = trace.get_current_span().get_span_context()
        assert restored.trace_id == source_context.trace_id
        assert restored.span_id == source_context.span_id
        assert restored.is_remote is True


def test_tool_observation_is_the_parent_of_a_claude_synthesizer_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.shared-telemetry")

    class Observation:
        def update(self, **_values: Any) -> None:
            pass

    class OtelLangfuse:
        @contextmanager
        def start_as_current_observation(
            self,
            *,
            name: str,
            **_values: Any,
        ):
            with tracer.start_as_current_span(name):
                yield Observation()

    monkeypatch.setattr(
        telemetry,
        "configure_tracing",
        lambda: OtelLangfuse(),
    )
    monkeypatch.setattr(
        claude_telemetry,
        "configure_tracing",
        lambda: OtelLangfuse(),
    )

    class SynthesisOutput(BaseModel):
        query: str

    task = AgentTask(
        task_id="ast-grep-synthesis:danger-call",
        phase=AgentPhase.AST_GREP_SYNTHESIS,
        agent_name="AST-Grep Synthesizer",
        description="Compile one structural intent.",
        instructions="Compile the supplied intent.",
        prompt="compile one anchor",
        output_type=SynthesisOutput,
        context=TaskContext(workspace_root=tmp_path),
        workspace=WorkspacePolicy(cwd=tmp_path),
        required_capabilities=frozenset(),
    )

    with tracer.start_as_current_span("Rule Generator run"):
        with telemetry.trace_tool_observation(
            name="synthesize_ast_grep_anchors",
            input={"anchor_intents": []},
        ):
            with claude_telemetry.trace_claude_task(
                task,
                model_id="claude-test",
            ) as task_trace:
                assert task_trace is not None

    spans = {
        span.name: span
        for span in exporter.get_finished_spans()
    }
    tool = spans["synthesize_ast_grep_anchors"]
    synthesizer = spans["AST-Grep Synthesizer run"]
    assert synthesizer.parent is not None
    assert synthesizer.parent.span_id == tool.context.span_id
