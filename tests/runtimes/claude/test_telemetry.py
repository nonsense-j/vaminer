"""Langfuse observation tests for the Claude runtime."""

from __future__ import annotations

from contextlib import contextmanager
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

    @contextmanager
    def start_as_current_observation(self, **values: Any):
        self.started.append(values)
        try:
            yield self.observation
        finally:
            self.observation.end()


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


def _assistant(
    *,
    message_id: str,
    content: list[dict[str, Any]],
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": content,
            **({"usage": usage} if usage is not None else {}),
        },
    }


def _tool_result(
    *,
    tool_use_id: str,
    content: Any,
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ]
        },
    }


def test_trace_is_skipped_without_an_active_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        telemetry,
        "configure_tracing",
        lambda: pytest.fail(
            "Langfuse should not be configured without an active trace"
        ),
    )

    with telemetry.trace_claude_task(
        _task(tmp_path),
        model_id="claude-test",
    ) as task_trace:
        assert task_trace is None


def test_agent_observation_matches_pydantic_shape_and_aggregates_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)
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
        with telemetry.trace_claude_task(
            _task(tmp_path),
            model_id="claude-test",
        ) as task_trace:
            assert task_trace is not None
            task_trace.configure_request(
                system_prompt="System instructions",
                tool_names=("Read", "mcp__vaminer__read_patch_diff"),
            )
            task_trace.complete(result)

    started = langfuse.started[0]
    assert started["name"] == "Root Cause Analyzer run"
    assert started["as_type"] == "agent"
    assert "model" not in started
    assert started["input"] == [
        {
            "role": "user",
            "parts": [
                {
                    "type": "text",
                    "content": '{"api_key":"<redacted>"}',
                }
            ],
        }
    ]
    update = langfuse.observation.updates[-1]
    assert update["input"][0] == {
        "role": "system",
        "content": "System instructions",
    }
    assert update["output"] == {"value": 7}
    assert "usage_details" not in update
    assert update["metadata"]["aggregated_usage"] == {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "requests": 2,
        "turns": 2,
    }
    assert update["metadata"]["attempts"] == 2
    assert update["metadata"]["session_id"] == "session-1"
    assert langfuse.observation.ended is True


def test_chat_and_tool_observations_match_pydantic_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)
    assistant = _assistant(
        message_id="request-1",
        usage={
            "input_tokens": 11,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
        },
        content=[
            {"type": "thinking", "thinking": "Inspect first."},
            {"type": "text", "text": "Inspecting the repository."},
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "Read",
                "input": {"api_key": "sk-secret-value-123456"},
            },
        ],
    )
    tool_result = _tool_result(
        tool_use_id="tool-1",
        content={"secret": "sk-secret-value-123456"},
    )

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(
            _task(tmp_path),
            model_id="claude-test",
        ) as task_trace:
            assert task_trace is not None
            task_trace.configure_request(
                system_prompt="System instructions",
                tool_names=("Read",),
            )
            task_trace.observe_event(assistant, attempt=1)
            task_trace.observe_event(assistant, attempt=1)
            task_trace.observe_event(tool_result, attempt=1)
            task_trace.observe_event(tool_result, attempt=1)

    agent = langfuse.observation
    assert [values["name"] for values, _ in agent.children] == [
        "chat claude-test",
        "Read",
    ]
    chat_values, chat = agent.children[0]
    assert chat_values["as_type"] == "generation"
    assert chat_values["model"] == "claude-test"
    assert chat_values["input"] == {
        "messages": [
            {"role": "system", "content": "System instructions"},
            {
                "role": "user",
                "parts": [
                    {
                        "type": "text",
                        "content": '{"api_key":"<redacted>"}',
                    }
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "Read",
                "parameters": {"type": "object"},
            }
        ],
    }
    assert chat_values["output"] == [
        {
            "role": "assistant",
            "parts": [
                {"type": "thinking", "content": "Inspect first."},
                {
                    "type": "text",
                    "content": "Inspecting the repository.",
                },
                {
                    "type": "tool_call",
                    "id": "tool-1",
                    "name": "Read",
                    "arguments": '{"api_key":"<redacted>"}',
                },
            ],
        }
    ]
    assert chat_values["usage_details"] == {
        "input": 11,
        "output": 5,
        "input_cached_tokens": 3,
        "prompt_cache_hit_tokens": 3,
        "cache_read_input_tokens": 3,
        "total": 19,
    }
    assert chat.ended is True

    tool_values, tool = agent.children[1]
    assert tool_values["as_type"] == "tool"
    assert tool_values["input"] == {"api_key": "<redacted>"}
    assert tool.updates == [{"output": {"secret": "<redacted>"}}]
    assert tool.ended is True


def test_next_chat_receives_full_message_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(
            _task(tmp_path),
            model_id="claude-test",
        ) as task_trace:
            assert task_trace is not None
            task_trace.start_attempt("Actual runtime prompt", attempt=1)
            task_trace.observe_event(
                _assistant(
                    message_id="request-1",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "probe.py"},
                        }
                    ],
                ),
                attempt=1,
            )
            task_trace.observe_event(
                _tool_result(
                    tool_use_id="tool-1",
                    content="file contents",
                ),
                attempt=1,
            )
            task_trace.observe_event(
                _assistant(
                    message_id="request-2",
                    content=[{"type": "text", "text": "Done"}],
                ),
                attempt=1,
            )

    chats = [
        values
        for values, _ in langfuse.observation.children
        if values["as_type"] == "generation"
    ]
    assert chats[0]["input"]["messages"] == [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "Actual runtime prompt"}
            ],
        }
    ]
    assert chats[1]["input"]["messages"] == [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "Actual runtime prompt"}
            ],
        },
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": "tool-1",
                    "name": "Read",
                    "arguments": '{"file_path":"probe.py"}',
                }
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": "tool-1",
                    "name": "Read",
                    "result": "file contents",
                }
            ],
        },
    ]


def test_repeated_assistant_message_updates_one_chat_and_one_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(
            _task(tmp_path),
            model_id="claude-test",
        ) as task_trace:
            assert task_trace is not None
            task_trace.observe_event(
                _assistant(
                    message_id="request-1",
                    content=[
                        {"type": "thinking", "thinking": "Inspect first"}
                    ],
                ),
                attempt=1,
            )
            task_trace.observe_event(
                _assistant(
                    message_id="request-1",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "probe.py"},
                        }
                    ],
                ),
                attempt=1,
            )
            task_trace.observe_event(
                _tool_result(
                    tool_use_id="tool-1",
                    content="file contents",
                ),
                attempt=1,
            )

    agent = langfuse.observation
    assert [values["name"] for values, _ in agent.children] == [
        "chat claude-test",
        "Read",
    ]
    _, chat = agent.children[0]
    assert chat.updates[-1]["output"] == [
        {
            "role": "assistant",
            "parts": [
                {"type": "thinking", "content": "Inspect first"},
                {
                    "type": "tool_call",
                    "id": "tool-1",
                    "name": "Read",
                    "arguments": '{"file_path":"probe.py"}',
                },
            ],
        }
    ]


def test_synthesis_tool_is_owned_by_mcp_to_preserve_child_agent_nesting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(
            _task(tmp_path),
            model_id="claude-test",
        ) as task_trace:
            assert task_trace is not None
            task_trace.observe_event(
                _assistant(
                    message_id="request-1",
                    content=[
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": (
                                "mcp__vaminer__"
                                "synthesize_ast_grep_anchors"
                            ),
                            "input": {"anchor_intents": []},
                        }
                    ],
                ),
                attempt=1,
            )
            task_trace.observe_event(
                _tool_result(tool_use_id="tool-1", content=[]),
                attempt=1,
            )

    assert [
        values["name"]
        for values, _ in langfuse.observation.children
    ] == ["chat claude-test"]


def test_compaction_remains_a_diagnostic_span_and_updates_chat_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with telemetry.trace_claude_task(
            _task(tmp_path),
            model_id="claude-test",
        ) as task_trace:
            assert task_trace is not None
            task_trace.start_attempt("Initial prompt", attempt=1)
            task_trace.observe_event(
                _assistant(
                    message_id="request-1",
                    content=[{"type": "text", "text": "Working"}],
                ),
                attempt=1,
            )
            task_trace.observe_event(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Continue"}
                        ]
                    },
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
                _assistant(
                    message_id="request-2",
                    content=[{"type": "text", "text": "Done"}],
                ),
                attempt=1,
            )

    agent = langfuse.observation
    compact_values = next(
        values
        for values, _ in agent.children
        if values["name"] == "context_compaction"
    )
    assert compact_values["input"]["pre_tokens"] == 12345
    assert compact_values["output"]["summary"] == "Condensed context"
    chats = [
        values
        for values, _ in agent.children
        if values["as_type"] == "generation"
    ]
    next_messages = chats[1]["input"]["messages"]
    assert next_messages[-1] == {
        "role": "user",
        "parts": [
            {
                "type": "text",
                "content": (
                    "Compacted context (auto): Condensed context"
                ),
            }
        ],
    }


def test_trace_marks_failure_and_preserves_the_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    langfuse = FakeLangfuse()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: langfuse)

    with trace.use_span(_active_span()):
        with pytest.raises(RuntimeError, match="provider failed"):
            with telemetry.trace_claude_task(
                _task(tmp_path),
                model_id="claude-test",
            ):
                raise RuntimeError(
                    "provider failed with sk-secret-value-123456"
                )

    update = langfuse.observation.updates[-1]
    assert update["level"] == "ERROR"
    assert update["metadata"]["error_type"] == "RuntimeError"
    assert "<redacted>" in update["status_message"]
    assert "sk-secret" not in update["status_message"]
    assert langfuse.observation.ended is True
