"""Langfuse observation tests for the Claude runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
)
from pydantic import BaseModel

from src.miner.agent.contracts import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    RuntimeUsage,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.runtimes.claude import telemetry


class ProbeOutput(BaseModel):
    value: int


class FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.children: list[tuple[dict[str, Any], FakeObservation]] = []
        self.ended = False

    def start_observation(self, **values: Any) -> FakeObservation:
        child = FakeObservation()
        self.children.append((values, child))
        return child

    def update(self, **values: Any) -> None:
        self.updates.append(values)

    def end(self) -> None:
        self.ended = True


class FakeLangfuse:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.observation = FakeObservation()

    def start_observation(self, **values: Any) -> FakeObservation:
        self.started.append(values)
        return self.observation


def _task(tmp_path: Path) -> AgentTask[ProbeOutput]:
    return AgentTask(
        task_id="probe-task",
        phase=AgentPhase.ROOT_CAUSE,
        agent_name="Root Cause Analyzer",
        description="probe",
        instructions="probe",
        prompt='{"api_key":"sk-secret-value-123456"}',
        output_type=ProbeOutput,
        context=TaskContext(workspace_root=tmp_path),
        workspace=WorkspacePolicy(cwd=tmp_path),
        required_capabilities=frozenset(),
    )


def _active_span() -> NonRecordingSpan:
    return NonRecordingSpan(
        SpanContext(
            trace_id=1,
            span_id=1,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
    )


def test_trace_is_skipped_without_an_active_workflow(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        telemetry,
        "configure_tracing",
        lambda: pytest.fail("Langfuse should not be configured without an active trace"),
    )

    with telemetry.trace_claude_task(_task(tmp_path), model_id="claude-test") as task_trace:
        assert task_trace is None


def test_trace_records_aggregate_result_and_usage(monkeypatch, tmp_path: Path):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)
    task = _task(tmp_path)
    result = AgentRunResult(
        output=ProbeOutput(value=7),
        runtime_id="claude-code",
        model_id="claude-test",
        usage=RuntimeUsage(
            requests=2,
            turns=2,
            input_tokens=11,
            output_tokens=5,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=3,
            duration_ms=40,
        ),
        attempts=2,
        metadata={"session_id": "session-1"},
    )

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(task, model_id="claude-test") as task_trace:
            assert task_trace is not None
            task_trace.complete(result)

    started = langfuse.started[0]
    assert started["name"] == "Claude Code: Root Cause Analyzer"
    assert started["as_type"] == "generation"
    assert started["model"] == "claude-test"
    assert started["input"] == '{"api_key":"<redacted>"}'
    update = langfuse.observation.updates[0]
    assert update["output"] == {"value": 7}
    assert update["usage_details"] == {
        "input": 11,
        "output": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
    }
    assert "cost_details" not in update
    assert update["metadata"]["attempts"] == 2
    assert update["metadata"]["requests"] == 2
    assert update["metadata"]["session_id"] == "session-1"
    assert langfuse.observation.ended is True


def test_trace_exports_completed_stream_events_as_children(monkeypatch, tmp_path: Path):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)
    assistant = {
        "type": "assistant",
        "message": {
            "id": "request-1",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 5,
            },
            "content": [
                {"type": "text", "text": "Inspecting the repository."},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "read_file",
                    "input": {"api_key": "sk-secret-value-123456"},
                },
            ],
        },
    }
    tool_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": {"secret": "sk-secret-value-123456"},
                }
            ]
        },
    }
    terminal = {
        "type": "result",
        "is_error": False,
        "session_id": "session-1",
        "terminal_reason": "end_turn",
        "total_cost_usd": 0.01,
        "result": '{"value": 7}',
    }
    result = AgentRunResult(
        output=ProbeOutput(value=7),
        runtime_id="claude-code",
        model_id="claude-test",
        usage=RuntimeUsage(requests=1, turns=1),
        metadata={"session_id": "session-1"},
    )

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(_task(tmp_path), model_id="claude-test") as task_trace:
            assert task_trace is not None
            task_trace.observe_event(assistant, attempt=1)
            task_trace.observe_event(assistant, attempt=1)
            task_trace.observe_event(tool_result, attempt=1)
            task_trace.observe_event(tool_result, attempt=1)
            task_trace.observe_event(terminal, attempt=1)
            task_trace.complete(result)

    aggregate = langfuse.observation
    assert [values["name"] for values, _ in aggregate.children] == [
        "Claude response 1",
        "Claude tool result",
        "Claude result attempt 1",
    ]
    response_values, response = aggregate.children[0]
    assert response_values["as_type"] == "generation"
    assert response_values["model"] == "claude-test"
    assert response_values["input"] == '{"api_key":"<redacted>"}'
    assert response_values["usage_details"] == {"input": 11, "output": 5}
    assert response.ended is True

    tool_values, tool = response.children[0]
    assert tool_values["name"] == "Claude tool: read_file"
    assert tool_values["as_type"] == "tool"
    assert tool_values["input"] == {"api_key": "<redacted>"}
    assert tool.ended is True

    tool_result_values, tool_result_observation = aggregate.children[1]
    assert tool_result_values["input"] == {
        "attempt": 1,
        "tool_use_id": "tool-1",
    }
    assert tool_result_values["output"] == {"secret": "<redacted>"}
    assert tool_result_observation.ended is True

    terminal_values, terminal_observation = aggregate.children[2]
    assert terminal_values["input"] == {
        "attempt": 1,
        "responses_observed": 1,
        "tool_calls_observed": 1,
    }
    assert "total_cost_usd" not in terminal_values["output"]
    assert terminal_observation.ended is True
    assert aggregate.updates[0]["metadata"]["live_responses"] == 1
    assert aggregate.updates[0]["metadata"]["live_tool_calls"] == 1
    assert aggregate.updates[0]["metadata"]["live_tool_results"] == 1
    assert aggregate.updates[0]["metadata"]["live_terminal_events"] == 1


def test_trace_uses_tool_results_as_the_next_response_input(monkeypatch, tmp_path: Path):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(_task(tmp_path), model_id="claude-test") as task_trace:
            assert task_trace is not None
            task_trace.start_attempt("Actual runtime prompt", attempt=1)
            task_trace.observe_event(
                {
                    "type": "assistant",
                    "message": {
                        "id": "request-1",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "read_file",
                                "input": {"path": "probe.py"},
                            }
                        ],
                    },
                },
                attempt=1,
            )
            user_event = {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "file contents",
                        }
                    ]
                },
            }
            task_trace.observe_event(user_event, attempt=1)
            task_trace.observe_event(user_event, attempt=1)
            task_trace.observe_event(
                {
                    "type": "assistant",
                    "message": {
                        "id": "request-2",
                        "content": [{"type": "text", "text": "Done"}],
                    },
                },
                attempt=1,
            )

    response_values = [
        values
        for values, _ in langfuse.observation.children
        if values["as_type"] == "generation"
    ]
    assert response_values[0]["input"] == "Actual runtime prompt"
    assert response_values[1]["input"] == [
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "file contents",
        }
    ]


def test_trace_nests_forwarded_subagent_events(monkeypatch, tmp_path: Path):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(_task(tmp_path), model_id="claude-test") as task_trace:
            assert task_trace is not None
            task_trace.observe_event(
                {
                    "type": "assistant",
                    "message": {
                        "id": "shared-request-id",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "agent-1",
                                "name": "Agent",
                                "input": {
                                    "subagent_type": "vaminer:rule-generator",
                                    "prompt": "Generate the rule",
                                },
                            }
                        ],
                    },
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "assistant",
                    "parent_tool_use_id": "agent-1",
                    "message": {
                        "id": "shared-request-id",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "child-tool-1",
                                "name": "Read",
                                "input": {"file_path": "probe.py"},
                            }
                        ],
                    },
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "user",
                    "parent_tool_use_id": "agent-1",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "child-tool-1",
                                "content": "source",
                            }
                        ]
                    },
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "agent-1",
                                "content": "rule generated",
                            }
                        ]
                    },
                },
                attempt=1,
            )

    aggregate = langfuse.observation
    parent_values, parent_response = aggregate.children[0]
    subagent_values, subagent = aggregate.children[1]
    assert parent_values["metadata"]["actor"] == "parent"
    assert parent_response.children[0][0]["name"] == "Claude tool: Agent"
    assert subagent_values["name"] == "Claude subagent: vaminer:rule-generator"
    assert subagent_values["as_type"] == "agent"
    assert subagent_values["metadata"]["parent_tool_use_id"] == "agent-1"

    child_response_values, child_response = subagent.children[0]
    assert child_response_values["name"] == "Claude response 2"
    assert child_response_values["input"] == "Generate the rule"
    assert child_response_values["metadata"]["actor"] == "subagent"
    assert child_response_values["metadata"]["subagent_type"] == (
        "vaminer:rule-generator"
    )
    assert child_response.children[0][0]["name"] == "Claude tool: Read"
    assert subagent.children[1][0]["name"] == "Claude tool result"
    assert subagent.children[2][0]["name"] == (
        "Claude subagent result: vaminer:rule-generator"
    )
    assert subagent.children[2][0]["metadata"]["actor"] == "subagent"
    assert subagent.updates[0]["metadata"]["status"] == "completed"
    assert subagent.ended is True


def test_trace_records_compaction_and_updates_next_input(monkeypatch, tmp_path: Path):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(_task(tmp_path), model_id="claude-test") as task_trace:
            assert task_trace is not None
            task_trace.start_attempt("Initial prompt", attempt=1)
            task_trace.observe_event(
                {
                    "type": "assistant",
                    "message": {"id": "request-1", "content": []},
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "Continue"}]},
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compactMetadata": {
                        "trigger": "auto",
                        "preTokens": 12345,
                    },
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "hook",
                    "hook_event_name": "PostCompact",
                    "trigger": "auto",
                    "compact_summary": "Condensed context",
                    "session_id": "session-1",
                },
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "assistant",
                    "message": {"id": "request-2", "content": []},
                },
                attempt=1,
            )

    aggregate = langfuse.observation
    compact_values = next(
        values
        for values, _ in aggregate.children
        if values["name"] == "Claude context compacted"
    )
    assert compact_values["input"]["pre_tokens"] == 12345
    assert compact_values["output"]["summary"] == "Condensed context"
    response_values = [
        values
        for values, _ in aggregate.children
        if values["as_type"] == "generation"
    ]
    assert response_values[1]["input"] == [
        {
            "compacted": True,
            "trigger": "auto",
            "pre_tokens": 12345,
            "summary": "Condensed context",
            "summary_available": True,
            "summary_truncated": False,
        },
        [{"type": "text", "text": "Continue"}],
    ]


def test_trace_marks_failure_and_preserves_the_exception(monkeypatch, tmp_path: Path):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with pytest.raises(RuntimeError, match="provider failed"):
            with telemetry.trace_claude_task(_task(tmp_path), model_id="claude-test"):
                raise RuntimeError("provider failed with sk-secret-value-123456")

    update = langfuse.observation.updates[0]
    assert update["level"] == "ERROR"
    assert update["metadata"]["error_type"] == "RuntimeError"
    assert "<redacted>" in update["status_message"]
    assert "sk-secret" not in update["status_message"]
    assert langfuse.observation.ended is True
