"""Langfuse tracing for Claude Code tasks and live stream events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from opentelemetry import trace

from ...agent.contracts import AgentRunResult, AgentTask
from ...utils.telemetry import configure_tracing
from .artifacts import clip, redact, sanitize_json

_EVENT_PAYLOAD_LIMIT = 16_000


@dataclass
class _PendingCompact:
    trigger: str | None = None
    pre_tokens: int | None = None
    summary: str | None = None
    summary_truncated: bool = False
    session_id: str | None = None
    parent_tool_use_id: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None


class ClaudeTaskTrace:
    def __init__(
        self,
        observation: Any,
        metadata: dict[str, Any],
        *,
        model_id: str,
        initial_prompt: str,
    ) -> None:
        self._observation = observation
        self._metadata = metadata
        self._model_id = model_id
        self._seen_message_ids: set[tuple[int, str | None, str]] = set()
        self._seen_tool_use_ids: set[tuple[int, str | None, str]] = set()
        self._seen_tool_result_ids: set[tuple[int, str | None, str]] = set()
        self._seen_user_events: set[tuple[int, str | None, str]] = set()
        self._pending_response_inputs: dict[tuple[int, str | None], list[Any]] = {
            (1, None): [_bounded_payload(initial_prompt)]
        }
        self._pending_compacts: dict[
            tuple[int, str | None], _PendingCompact
        ] = {}
        self._subagent_observations: dict[tuple[int, str], Any] = {}
        self._subagent_types: dict[tuple[int, str], str] = {}
        self._open_subagents: set[tuple[int, str]] = set()
        self._response_count = 0
        self._tool_call_count = 0
        self._tool_result_count = 0
        self._terminal_count = 0
        self._compact_count = 0
        self._subagent_count = 0

    def start_attempt(self, prompt: str, *, attempt: int) -> None:
        """Attach the actual Claude prompt to the next response in an attempt."""

        self._pending_response_inputs[(attempt, None)] = [_bounded_payload(prompt)]

    def complete(self, result: AgentRunResult[Any]) -> None:
        usage = result.usage
        metadata = {
            **self._progress_metadata(),
            "attempts": result.attempts,
            "requests": usage.requests if usage is not None else None,
            "turns": usage.turns if usage is not None else None,
            "duration_ms": usage.duration_ms if usage is not None else None,
            "session_id": result.metadata.get("session_id"),
        }
        update: dict[str, Any] = {
            "output": sanitize_json(result.output.model_dump(mode="json", by_alias=True)),
            "metadata": metadata,
        }
        usage_details = _usage_details(result)
        if usage_details:
            update["usage_details"] = usage_details
        self._update(**update)

    def fail(self, error: BaseException) -> None:
        self._update(
            level="ERROR",
            status_message=redact(clip(str(error), 2_000)),
            metadata={
                **self._progress_metadata(),
                "error_type": type(error).__name__,
            },
        )

    def observe_event(self, event: Mapping[str, Any], *, attempt: int) -> None:
        """Export completed stream events while the aggregate task is still running."""

        try:
            event_type = event.get("type")
            if event_type == "assistant":
                self._record_assistant(event, attempt=attempt)
            elif event_type == "user":
                self._record_user(event, attempt=attempt)
            elif event_type == "system" and event.get("subtype") == "compact_boundary":
                self._record_compact_boundary(event, attempt=attempt)
            elif event.get("hook_event_name") == "PostCompact":
                self._record_post_compact(event, attempt=attempt)
            elif event_type in {"task_notification", "task_result"}:
                self._record_subagent_lifecycle(event, attempt=attempt)
            elif event_type == "result":
                self._record_terminal(event, attempt=attempt)
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return

    def close(self) -> None:
        for compact_key in list(self._pending_compacts):
            self._flush_pending_compact(compact_key)
        for subagent_key in list(self._open_subagents):
            self._finish_subagent(
                subagent_key,
                output={"status": "trace_closed"},
                status="trace_closed",
            )
        try:
            self._observation.end()
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return

    def _record_assistant(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        parent_tool_use_id = _parent_tool_use_id(event)
        compact_key = (attempt, parent_tool_use_id)
        self._flush_pending_compact(compact_key)
        message = event.get("message")
        if not isinstance(message, Mapping):
            return

        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            dedupe_key = (attempt, parent_tool_use_id, message_id)
            if dedupe_key in self._seen_message_ids:
                return
            self._seen_message_ids.add(dedupe_key)

        self._response_count += 1
        owner = self._actor_owner(
            attempt=attempt,
            parent_tool_use_id=parent_tool_use_id,
        )
        metadata = {
            **self._metadata,
            "attempt": attempt,
            "response": self._response_count,
            "message_id": message_id if isinstance(message_id, str) else None,
            **self._actor_metadata(attempt, parent_tool_use_id),
        }
        response = self._start_child(
            parent=owner,
            name=f"Claude response {self._response_count}",
            as_type="generation",
            input=self._take_response_input(compact_key),
            output=_bounded_payload(message.get("content")),
            metadata=metadata,
            model=self._model_id,
            usage_details=_usage_details_from_mapping(message.get("usage")),
        )
        if response is None:
            return

        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                self._record_tool_call(
                    response,
                    block,
                    attempt=attempt,
                    response=self._response_count,
                    actor_parent_tool_use_id=parent_tool_use_id,
                )
        self._end(response)

    def _record_tool_call(
        self,
        parent: Any,
        block: Mapping[str, Any],
        *,
        attempt: int,
        response: int,
        actor_parent_tool_use_id: str | None,
    ) -> None:
        tool_use_id = block.get("id")
        if isinstance(tool_use_id, str) and tool_use_id:
            dedupe_key = (attempt, actor_parent_tool_use_id, tool_use_id)
            if dedupe_key in self._seen_tool_use_ids:
                return
            self._seen_tool_use_ids.add(dedupe_key)

        self._tool_call_count += 1
        tool_name = str(block.get("name") or "unknown")
        child = self._start_child(
            parent=parent,
            name=f"Claude tool: {tool_name}",
            as_type="tool",
            input=_bounded_payload(block.get("input")),
            metadata={
                **self._metadata,
                "attempt": attempt,
                "response": response,
                "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
                **self._actor_metadata(attempt, actor_parent_tool_use_id),
            },
        )
        self._end(child)
        if (
            tool_name in {"Agent", "Task"}
            and isinstance(tool_use_id, str)
            and tool_use_id
        ):
            self._start_subagent(
                attempt=attempt,
                tool_use_id=tool_use_id,
                block=block,
                parent_tool_use_id=actor_parent_tool_use_id,
                response=response,
            )

    def _record_user(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if content is None:
            return
        parent_tool_use_id = _parent_tool_use_id(event)
        actor_key = (attempt, parent_tool_use_id)

        event_key = json.dumps(
            sanitize_json(content),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        dedupe_key = (attempt, parent_tool_use_id, event_key)
        if dedupe_key in self._seen_user_events:
            return
        self._seen_user_events.add(dedupe_key)
        self._pending_response_inputs.setdefault(actor_key, []).append(
            _bounded_payload(content)
        )

        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                dedupe_key = (attempt, parent_tool_use_id, tool_use_id)
                if dedupe_key in self._seen_tool_result_ids:
                    continue
                self._seen_tool_result_ids.add(dedupe_key)
            self._tool_result_count += 1
            is_error = bool(block.get("is_error"))
            subagent_key = (
                (attempt, tool_use_id)
                if isinstance(tool_use_id, str)
                and (attempt, tool_use_id) in self._subagent_observations
                else None
            )
            subagent_type = (
                self._subagent_types.get(subagent_key)
                if subagent_key is not None
                else None
            )
            child = self._start_child(
                parent=(
                    self._subagent_observations[subagent_key]
                    if subagent_key is not None
                    else self._actor_owner(
                        attempt=attempt,
                        parent_tool_use_id=parent_tool_use_id,
                    )
                ),
                name=(
                    f"Claude subagent result: {subagent_type}"
                    if subagent_type
                    else "Claude tool result"
                ),
                as_type="span",
                input={
                    "attempt": attempt,
                    "tool_use_id": tool_use_id,
                },
                output=_bounded_payload(block.get("content")),
                metadata={
                    **self._metadata,
                    "attempt": attempt,
                    "tool_use_id": tool_use_id,
                    **self._actor_metadata(
                        attempt,
                        subagent_key[1] if subagent_key is not None else parent_tool_use_id,
                    ),
                    "result_kind": "subagent" if subagent_key else "tool",
                },
                level="ERROR" if is_error else None,
                status_message="Claude tool returned an error" if is_error else None,
            )
            self._end(child)
            if subagent_key is not None:
                self._finish_subagent(
                    subagent_key,
                    output=block.get("content"),
                    status="error" if is_error else "completed",
                    is_error=is_error,
                )

    def _record_terminal(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        for compact_key in [
            key for key in self._pending_compacts if key[0] == attempt
        ]:
            self._flush_pending_compact(compact_key)
        parent_tool_use_id = _parent_tool_use_id(event)
        self._terminal_count += 1
        is_error = bool(event.get("is_error"))
        child = self._start_child(
            parent=self._actor_owner(
                attempt=attempt,
                parent_tool_use_id=parent_tool_use_id,
            ),
            name=f"Claude result attempt {attempt}",
            as_type="span",
            input={
                "attempt": attempt,
                "responses_observed": self._response_count,
                "tool_calls_observed": self._tool_call_count,
            },
            output=_bounded_payload(event),
            metadata={
                **self._metadata,
                "attempt": attempt,
                "terminal_reason": event.get("terminal_reason"),
                "session_id": event.get("session_id"),
                **self._actor_metadata(attempt, parent_tool_use_id),
            },
            level="ERROR" if is_error else None,
            status_message=(
                redact(clip(str(event.get("result") or "Claude task failed"), 2_000))
                if is_error
                else None
            ),
        )
        self._end(child)
        if parent_tool_use_id is not None:
            self._finish_subagent(
                (attempt, parent_tool_use_id),
                output=event,
                status="error" if is_error else "completed",
                is_error=is_error,
            )

    def _record_subagent_lifecycle(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        parent_tool_use_id = _parent_tool_use_id(event)
        task_id = event.get("task_id")
        if parent_tool_use_id is None and isinstance(task_id, str):
            if (attempt, task_id) in self._subagent_observations:
                parent_tool_use_id = task_id
        actor_metadata = (
            self._actor_metadata(attempt, parent_tool_use_id)
            if parent_tool_use_id is not None
            else {
                "actor": "subagent",
                "parent_tool_use_id": None,
                "subagent_type": str(event.get("agent_type") or "unknown"),
            }
        )
        child = self._start_child(
            parent=self._actor_owner(
                attempt=attempt,
                parent_tool_use_id=parent_tool_use_id,
            ),
            name=f"Claude subagent {event.get('type')}",
            as_type="span",
            input={
                "attempt": attempt,
                "task_id": task_id,
                "status": event.get("status"),
            },
            output=_bounded_payload(event),
            metadata={
                **self._metadata,
                "attempt": attempt,
                **actor_metadata,
            },
        )
        self._end(child)
        if event.get("type") == "task_result" and parent_tool_use_id is not None:
            status = str(event.get("status") or "completed")
            self._finish_subagent(
                (attempt, parent_tool_use_id),
                output=event,
                status=status,
                is_error=status.lower() in {"error", "failed"},
            )

    def _record_compact_boundary(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        metadata = event.get("compactMetadata")
        parent_tool_use_id = _parent_tool_use_id(event)
        compact_key = (attempt, parent_tool_use_id)
        compact = self._pending_compacts.get(compact_key)
        if compact is None:
            candidates = [
                key
                for key, pending in self._pending_compacts.items()
                if key[0] == attempt
                and pending.pre_tokens is None
                and pending.summary is not None
            ]
            if len(candidates) == 1:
                compact = self._pending_compacts.pop(candidates[0])
            else:
                compact = _PendingCompact()
            self._pending_compacts[compact_key] = compact
        compact.parent_tool_use_id = parent_tool_use_id
        if isinstance(metadata, Mapping):
            trigger = metadata.get("trigger")
            if isinstance(trigger, str):
                compact.trigger = trigger
            pre_tokens = metadata.get("preTokens")
            if isinstance(pre_tokens, int) and not isinstance(pre_tokens, bool):
                compact.pre_tokens = pre_tokens
        self._emit_compact_if_complete(compact_key)

    def _record_post_compact(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        parent_tool_use_id = _parent_tool_use_id(event)
        hook_agent_type = event.get("agent_type")
        if (
            parent_tool_use_id is None
            and isinstance(hook_agent_type, str)
            and hook_agent_type
        ):
            matching_boundaries = [
                key
                for key, pending in self._pending_compacts.items()
                if key[0] == attempt
                and key[1] is not None
                and pending.summary is None
                and self._subagent_types.get((key[0], key[1]))
                == hook_agent_type
            ]
            if len(matching_boundaries) == 1:
                parent_tool_use_id = matching_boundaries[0][1]
        compact_key = (attempt, parent_tool_use_id)
        compact = self._pending_compacts.get(compact_key)
        if compact is None and parent_tool_use_id is None:
            candidates = [
                key
                for key, pending in self._pending_compacts.items()
                if key[0] == attempt and pending.summary is None
            ]
            if len(candidates) == 1:
                compact_key = candidates[0]
                compact = self._pending_compacts[compact_key]
        if compact is None:
            compact = _PendingCompact()
            self._pending_compacts[compact_key] = compact
        trigger = event.get("trigger")
        if isinstance(trigger, str):
            compact.trigger = trigger
        summary = event.get("compact_summary")
        if isinstance(summary, str):
            compact.summary = summary
        compact.summary_truncated = bool(event.get("summary_truncated"))
        session_id = event.get("session_id")
        if isinstance(session_id, str):
            compact.session_id = session_id
        agent_id = event.get("agent_id")
        if isinstance(agent_id, str):
            compact.agent_id = agent_id
        agent_type = event.get("agent_type")
        if isinstance(agent_type, str):
            compact.agent_type = agent_type
        compact.parent_tool_use_id = compact_key[1]
        self._emit_compact_if_complete(compact_key)

    def _emit_compact_if_complete(
        self,
        compact_key: tuple[int, str | None],
    ) -> None:
        compact = self._pending_compacts.get(compact_key)
        if compact is None or compact.summary is None or compact.pre_tokens is None:
            return
        self._flush_pending_compact(compact_key)

    def _flush_pending_compact(
        self,
        compact_key: tuple[int, str | None],
    ) -> None:
        compact = self._pending_compacts.pop(compact_key, None)
        if compact is None:
            return

        attempt, parent_tool_use_id = compact_key
        self._compact_count += 1
        compact_input = {
            "attempt": attempt,
            "trigger": compact.trigger,
            "pre_tokens": compact.pre_tokens,
        }
        compact_output = {
            "summary": compact.summary,
            "summary_available": compact.summary is not None,
            "summary_truncated": compact.summary_truncated,
        }
        child = self._start_child(
            parent=self._actor_owner(
                attempt=attempt,
                parent_tool_use_id=parent_tool_use_id,
            ),
            name="Claude context compacted",
            as_type="span",
            input=_bounded_payload(compact_input),
            output=_bounded_payload(compact_output),
            metadata={
                **self._metadata,
                "attempt": attempt,
                "compact": self._compact_count,
                "session_id": compact.session_id,
                "agent_id": compact.agent_id,
                "hook_agent_type": compact.agent_type,
                **self._actor_metadata(attempt, parent_tool_use_id),
            },
        )
        self._end(child)
        compact_context = _bounded_payload(
            {
                "compacted": True,
                "trigger": compact.trigger,
                "pre_tokens": compact.pre_tokens,
                "summary": compact.summary,
                "summary_available": compact.summary is not None,
                "summary_truncated": compact.summary_truncated,
            }
        )
        pending_inputs = self._pending_response_inputs.get(compact_key, [])
        self._pending_response_inputs[compact_key] = [
            compact_context,
            *pending_inputs,
        ]

    def _start_subagent(
        self,
        *,
        attempt: int,
        tool_use_id: str,
        block: Mapping[str, Any],
        parent_tool_use_id: str | None,
        response: int,
    ) -> None:
        subagent_key = (attempt, tool_use_id)
        if subagent_key in self._subagent_observations:
            return
        tool_input = block.get("input")
        agent_type = (
            tool_input.get("subagent_type")
            if isinstance(tool_input, Mapping)
            else None
        )
        normalized_type = str(agent_type or "unknown")
        self._subagent_types[subagent_key] = normalized_type
        subagent_prompt = (
            tool_input.get("prompt")
            if isinstance(tool_input, Mapping)
            else None
        )
        self._pending_response_inputs[subagent_key] = [
            _bounded_payload(
                subagent_prompt if subagent_prompt is not None else tool_input
            )
        ]
        self._subagent_count += 1
        observation = self._start_child(
            parent=self._actor_owner(
                attempt=attempt,
                parent_tool_use_id=parent_tool_use_id,
            ),
            name=f"Claude subagent: {normalized_type}",
            as_type="agent",
            input=_bounded_payload(tool_input),
            metadata={
                **self._metadata,
                "attempt": attempt,
                "response": response,
                "actor": "subagent",
                "subagent_type": normalized_type,
                "parent_tool_use_id": tool_use_id,
                "spawned_by_parent_tool_use_id": parent_tool_use_id,
            },
        )
        if observation is None:
            return
        self._subagent_observations[subagent_key] = observation
        self._open_subagents.add(subagent_key)

    def _actor_owner(
        self,
        *,
        attempt: int,
        parent_tool_use_id: str | None,
    ) -> Any:
        if parent_tool_use_id is None:
            return self._observation
        return self._subagent_observations.get(
            (attempt, parent_tool_use_id),
            self._observation,
        )

    def _actor_metadata(
        self,
        attempt: int,
        parent_tool_use_id: str | None,
    ) -> dict[str, Any]:
        if parent_tool_use_id is None:
            return {
                "actor": "parent",
                "parent_tool_use_id": None,
                "subagent_type": None,
            }
        return {
            "actor": "subagent",
            "parent_tool_use_id": parent_tool_use_id,
            "subagent_type": self._subagent_types.get(
                (attempt, parent_tool_use_id),
                "unknown",
            ),
        }

    def _finish_subagent(
        self,
        subagent_key: tuple[int, str],
        *,
        output: Any,
        status: str,
        is_error: bool = False,
    ) -> None:
        if subagent_key not in self._open_subagents:
            return
        observation = self._subagent_observations.get(subagent_key)
        if observation is None:
            return
        self._update_observation(
            observation,
            output=_bounded_payload(output),
            metadata={
                **self._metadata,
                "attempt": subagent_key[0],
                "actor": "subagent",
                "parent_tool_use_id": subagent_key[1],
                "subagent_type": self._subagent_types.get(
                    subagent_key,
                    "unknown",
                ),
                "status": status,
            },
            level="ERROR" if is_error else None,
            status_message=(
                "Claude subagent returned an error" if is_error else None
            ),
        )
        self._end(observation)
        self._open_subagents.discard(subagent_key)

    def _start_child(
        self,
        *,
        name: str,
        as_type: str,
        parent: Any | None = None,
        **values: Any,
    ) -> Any | None:
        try:
            owner = parent if parent is not None else self._observation
            return owner.start_observation(
                name=name,
                as_type=as_type,
                **{key: value for key, value in values.items() if value is not None},
            )
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return None

    @staticmethod
    def _end(observation: Any | None) -> None:
        if observation is None:
            return
        try:
            observation.end()
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return

    def _progress_metadata(self) -> dict[str, Any]:
        return {
            **self._metadata,
            "live_responses": self._response_count,
            "live_tool_calls": self._tool_call_count,
            "live_tool_results": self._tool_result_count,
            "live_terminal_events": self._terminal_count,
            "live_compactions": self._compact_count,
            "live_subagents": self._subagent_count,
            "open_subagents": len(self._open_subagents),
        }

    def _take_response_input(
        self,
        actor_key: tuple[int, str | None],
    ) -> Any:
        inputs = self._pending_response_inputs.pop(actor_key, [])
        if not inputs:
            return {
                "attempt": actor_key[0],
                "source": "claude-stream",
                "request_payload_available": False,
                **self._actor_metadata(*actor_key),
            }
        return inputs[0] if len(inputs) == 1 else inputs

    def _update(self, **values: Any) -> None:
        try:
            self._observation.update(**values)
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return

    @staticmethod
    def _update_observation(observation: Any, **values: Any) -> None:
        try:
            observation.update(
                **{key: value for key, value in values.items() if value is not None}
            )
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return


@contextmanager
def trace_claude_task(
    task: AgentTask[Any],
    *,
    model_id: str,
) -> Iterator[ClaudeTaskTrace | None]:
    """Trace one aggregate Claude task under the active workflow trace."""

    if not trace.get_current_span().get_span_context().is_valid:
        yield None
        return

    langfuse = configure_tracing()
    if langfuse is None:
        yield None
        return

    metadata = {
        "runtime": "claude-code",
        "phase": task.phase.value,
        "task_id": task.task_id,
        "agent_name": task.agent_name,
        "output_type": task.output_type.__name__,
    }
    try:
        observation = langfuse.start_observation(
            name=f"Claude Code: {task.agent_name}",
            as_type="generation",
            input=redact(task.prompt),
            metadata=metadata,
            model=model_id,
        )
    except Exception:  # noqa: BLE001 - optional tracing must never block mining.
        yield None
        return

    task_trace = ClaudeTaskTrace(
        observation,
        metadata,
        model_id=model_id,
        initial_prompt=task.prompt,
    )
    try:
        yield task_trace
    except BaseException as error:
        task_trace.fail(error)
        raise
    finally:
        task_trace.close()


def _usage_details(result: AgentRunResult[Any]) -> dict[str, int]:
    usage = result.usage
    if usage is None:
        return {}
    values = {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
    }
    return {name: value for name, value in values.items() if value is not None}


def _usage_details_from_mapping(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    fields = {
        "input": value.get("input_tokens"),
        "output": value.get("output_tokens"),
        "cache_creation_input_tokens": value.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": value.get("cache_read_input_tokens"),
    }
    usage = {
        name: item
        for name, item in fields.items()
        if isinstance(item, int) and not isinstance(item, bool)
    }
    return usage or None


def _parent_tool_use_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("parent_tool_use_id")
    return value if isinstance(value, str) and value else None


def _bounded_payload(value: Any) -> Any:
    sanitized = sanitize_json(value)
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    if len(serialized) <= _EVENT_PAYLOAD_LIMIT:
        return sanitized
    return {
        "preview": clip(serialized, _EVENT_PAYLOAD_LIMIT),
        "truncated": True,
    }
