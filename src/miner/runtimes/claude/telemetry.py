"""Pydantic-shaped Langfuse observations for Claude Code tasks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from opentelemetry import trace

from ...agent.contracts import AgentRunResult, AgentTask
from ...utils.telemetry import configure_tracing
from .artifacts import clip, redact, sanitize_json

_EVENT_PAYLOAD_LIMIT = 16_000
_SYNTHESIS_TOOL = "synthesize_ast_grep_anchors"

_ActorKey = tuple[int, str | None]
_ToolKey = tuple[int, str | None, str]
_MessageKey = tuple[int, str | None, str]


@dataclass
class _OpenChat:
    observation: Any
    message_id: str | None
    output_message: dict[str, Any]


@dataclass
class _ToolCall:
    name: str
    input: Any
    observation: Any | None


@dataclass
class _PendingCompact:
    trigger: str | None = None
    pre_tokens: int | None = None
    summary: str | None = None
    summary_truncated: bool = False
    session_id: str | None = None


class ClaudeTaskTrace:
    """Map Claude stream events onto Pydantic AI's Agent/chat/tool model."""

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
        self._system_prompt: str | None = None
        self._tool_definitions: list[dict[str, Any]] = []
        self._messages: dict[_ActorKey, list[dict[str, Any]]] = {}
        self._open_chats: dict[_ActorKey, _OpenChat] = {}
        self._assistant_messages: dict[_MessageKey, dict[str, Any]] = {}
        self._seen_message_ids: set[_MessageKey] = set()
        self._seen_tool_use_ids: set[_ToolKey] = set()
        self._seen_tool_result_ids: set[_ToolKey] = set()
        self._seen_user_events: set[tuple[int, str | None, str]] = set()
        self._tool_calls: dict[_ToolKey, _ToolCall] = {}
        self._pending_compacts: dict[_ActorKey, _PendingCompact] = {}
        self._response_count = 0
        self._tool_call_count = 0
        self._tool_result_count = 0
        self._terminal_count = 0
        self._permission_denial_count = 0
        self._compact_count = 0
        self._last_terminal: Any = None
        self.start_attempt(initial_prompt, attempt=1)

    def configure_request(
        self,
        *,
        system_prompt: str,
        tool_names: Sequence[str],
    ) -> None:
        """Attach the actual Claude system prompt and visible tool catalog."""

        self._system_prompt = redact(system_prompt)
        self._tool_definitions = [
            {
                "type": "function",
                "name": _display_tool_name(name),
                "parameters": {"type": "object"},
            }
            for name in tool_names
        ]
        for actor_key, messages in self._messages.items():
            self._messages[actor_key] = self._with_system_message(messages)
        self._update(input=self._all_messages())

    def start_attempt(self, prompt: str, *, attempt: int) -> None:
        """Start one independent Claude process conversation."""

        actor_key = (attempt, None)
        self._close_chat(actor_key)
        self._messages[actor_key] = self._with_system_message(
            [_user_text_message(prompt)]
        )

    def complete(self, result: AgentRunResult[Any]) -> None:
        self._close_live_observations()
        usage = result.usage
        metadata = {
            **self._progress_metadata(),
            "attempts": result.attempts,
            "requests": usage.requests if usage is not None else None,
            "turns": usage.turns if usage is not None else None,
            "duration_ms": usage.duration_ms if usage is not None else None,
            "session_id": result.metadata.get("session_id"),
            "aggregated_usage": _runtime_usage(result),
        }
        self._update(
            input=self._all_messages(),
            output=sanitize_json(
                result.output.model_dump(mode="json", by_alias=True)
            ),
            metadata=metadata,
        )

    def fail(self, error: BaseException) -> None:
        self._close_live_observations()
        self._update(
            input=self._all_messages(),
            level="ERROR",
            status_message=redact(clip(str(error), 2_000)),
            metadata={
                **self._progress_metadata(),
                "error_type": type(error).__name__,
            },
        )

    def observe_event(self, event: Mapping[str, Any], *, attempt: int) -> None:
        """Export completed Claude events without affecting task execution."""

        try:
            event_type = event.get("type")
            if event_type == "assistant":
                self._record_assistant(event, attempt=attempt)
            elif event_type == "user":
                self._record_user(event, attempt=attempt)
            elif (
                event_type == "system"
                and event.get("subtype") == "compact_boundary"
            ):
                self._record_compact_boundary(event, attempt=attempt)
            elif event.get("hook_event_name") == "PostCompact":
                self._record_post_compact(event, attempt=attempt)
            elif event_type == "result":
                self._record_terminal(event, attempt=attempt)
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return

    def close(self) -> None:
        """Close child observations; the surrounding context owns the Agent."""

        for actor_key in list(self._pending_compacts):
            self._flush_pending_compact(actor_key)
        self._close_live_observations()

    def _record_assistant(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        message = event.get("message")
        if not isinstance(message, Mapping):
            return
        response_model = message.get("model")
        model_id = (
            response_model
            if isinstance(response_model, str) and response_model
            else self._model_id
        )
        parent_tool_use_id = _parent_tool_use_id(event)
        actor_key = (attempt, parent_tool_use_id)
        self._flush_pending_compact(actor_key)
        message_id_value = message.get("id")
        message_id = (
            message_id_value
            if isinstance(message_id_value, str) and message_id_value
            else None
        )
        message_key = (
            (attempt, parent_tool_use_id, message_id)
            if message_id is not None
            else None
        )

        existing = self._open_chats.get(actor_key)
        repeated_open_message = (
            existing is not None
            and message_id is not None
            and existing.message_id == message_id
        )
        if not repeated_open_message:
            self._close_chat(actor_key)

        incoming = _assistant_message(message)
        if repeated_open_message:
            assert existing is not None
            merged = _merge_assistant_messages(existing.output_message, incoming)
            existing.output_message = merged
            if message_key is not None:
                self._assistant_messages[message_key] = merged
            self._update_observation(
                existing.observation,
                output=[_message_payload(merged)],
                usage_details=_usage_details_from_mapping(message.get("usage")),
            )
        elif message_key is not None and message_key in self._seen_message_ids:
            pass
        else:
            self._response_count += 1
            if message_key is not None:
                self._seen_message_ids.add(message_key)
                self._assistant_messages[message_key] = incoming
            response = self._start_child(
                parent=self._observation,
                name=f"chat {model_id}",
                as_type="generation",
                input={
                    "messages": _message_payload(
                        self._messages_for(actor_key)
                    ),
                    "tools": _message_payload(self._tool_definitions),
                },
                output=[_message_payload(incoming)],
                metadata={
                    **self._metadata,
                    "attempt": attempt,
                    "response": self._response_count,
                    "message_id": message_id,
                    "actor": (
                        "subagent"
                        if parent_tool_use_id is not None
                        else "parent"
                    ),
                    "parent_tool_use_id": parent_tool_use_id,
                },
                model=model_id,
                usage_details=_usage_details_from_mapping(message.get("usage")),
            )
            if response is not None:
                self._open_chats[actor_key] = _OpenChat(
                    observation=response,
                    message_id=message_id,
                    output_message=incoming,
                )

        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, Mapping)
                    and block.get("type") == "tool_use"
                ):
                    self._record_tool_call(
                        block,
                        attempt=attempt,
                        parent_tool_use_id=parent_tool_use_id,
                    )

    def _record_tool_call(
        self,
        block: Mapping[str, Any],
        *,
        attempt: int,
        parent_tool_use_id: str | None,
    ) -> None:
        tool_use_id_value = block.get("id")
        if not isinstance(tool_use_id_value, str) or not tool_use_id_value:
            return
        tool_key = (attempt, parent_tool_use_id, tool_use_id_value)
        if tool_key in self._seen_tool_use_ids:
            return
        self._seen_tool_use_ids.add(tool_key)
        self._tool_call_count += 1
        raw_name = str(block.get("name") or "unknown")
        display_name = _display_tool_name(raw_name)
        tool_input = _bounded_payload(block.get("input"))
        tool_observation = None
        if display_name != _SYNTHESIS_TOOL:
            tool_observation = self._start_child(
                parent=self._observation,
                name=display_name,
                as_type="tool",
                input=tool_input,
                metadata={
                    **self._metadata,
                    "attempt": attempt,
                    "tool_use_id": tool_use_id_value,
                    "tool_name": display_name,
                    "claude_tool_name": raw_name,
                    "parent_tool_use_id": parent_tool_use_id,
                },
            )
        self._tool_calls[tool_key] = _ToolCall(
            name=display_name,
            input=tool_input,
            observation=tool_observation,
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
        self._close_chat(actor_key)

        blocks = content if isinstance(content, list) else [
            {"type": "text", "text": content}
        ]
        parts: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                parts.append(
                    {"type": "text", "content": _string_content(block)}
                )
                continue
            if block.get("type") != "tool_result":
                text = block.get("text", block.get("content"))
                parts.append(
                    {"type": "text", "content": _string_content(text)}
                )
                continue
            tool_use_id_value = block.get("tool_use_id")
            tool_use_id = (
                tool_use_id_value
                if isinstance(tool_use_id_value, str)
                else ""
            )
            tool_key = (attempt, parent_tool_use_id, tool_use_id)
            tool_call = self._tool_calls.get(tool_key)
            tool_name = tool_call.name if tool_call is not None else "unknown"
            result = _bounded_payload(block.get("content"))
            parts.append(
                {
                    "type": "tool_call_response",
                    "id": tool_use_id,
                    "name": tool_name,
                    "result": _string_content(result),
                }
            )
            if tool_key in self._seen_tool_result_ids:
                continue
            self._seen_tool_result_ids.add(tool_key)
            self._tool_result_count += 1
            is_error = bool(block.get("is_error"))
            if tool_call is not None and tool_call.observation is not None:
                self._update_observation(
                    tool_call.observation,
                    output=result,
                    level="ERROR" if is_error else None,
                    status_message=(
                        "Claude tool returned an error" if is_error else None
                    ),
                )
                self._end(tool_call.observation)
                tool_call.observation = None

        if parts:
            self._messages_for(actor_key).append(
                {"role": "user", "parts": parts}
            )

    def _record_terminal(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        actor_key = (attempt, _parent_tool_use_id(event))
        self._flush_pending_compact(actor_key)
        self._close_chat(actor_key)
        self._terminal_count += 1
        permission_denials = event.get("permission_denials") or []
        if isinstance(permission_denials, (list, tuple)):
            self._permission_denial_count += len(permission_denials)
        self._last_terminal = _bounded_payload(event)
        if bool(event.get("is_error")):
            self._update(
                level="ERROR",
                status_message=redact(
                    clip(
                        str(event.get("result") or "Claude task failed"),
                        2_000,
                    )
                ),
            )

    def _record_compact_boundary(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        actor_key = (attempt, _parent_tool_use_id(event))
        compact = self._pending_compacts.setdefault(
            actor_key,
            _PendingCompact(),
        )
        metadata = event.get("compactMetadata")
        if isinstance(metadata, Mapping):
            trigger = metadata.get("trigger")
            if isinstance(trigger, str):
                compact.trigger = trigger
            pre_tokens = metadata.get("preTokens")
            if isinstance(pre_tokens, int) and not isinstance(pre_tokens, bool):
                compact.pre_tokens = pre_tokens
        self._emit_compact_if_complete(actor_key)

    def _record_post_compact(
        self,
        event: Mapping[str, Any],
        *,
        attempt: int,
    ) -> None:
        actor_key = (attempt, _parent_tool_use_id(event))
        compact = self._pending_compacts.setdefault(
            actor_key,
            _PendingCompact(),
        )
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
        self._emit_compact_if_complete(actor_key)

    def _emit_compact_if_complete(self, actor_key: _ActorKey) -> None:
        compact = self._pending_compacts.get(actor_key)
        if (
            compact is not None
            and compact.summary is not None
            and compact.pre_tokens is not None
        ):
            self._flush_pending_compact(actor_key)

    def _flush_pending_compact(self, actor_key: _ActorKey) -> None:
        compact = self._pending_compacts.pop(actor_key, None)
        if compact is None:
            return
        self._close_chat(actor_key)
        self._compact_count += 1
        compact_input = {
            "trigger": compact.trigger,
            "pre_tokens": compact.pre_tokens,
        }
        compact_output = {
            "summary": compact.summary,
            "summary_available": compact.summary is not None,
            "summary_truncated": compact.summary_truncated,
        }
        child = self._start_child(
            parent=self._observation,
            name="context_compaction",
            as_type="span",
            input=_bounded_payload(compact_input),
            output=_bounded_payload(compact_output),
            metadata={
                **self._metadata,
                "attempt": actor_key[0],
                "compact": self._compact_count,
                "session_id": compact.session_id,
                "parent_tool_use_id": actor_key[1],
            },
        )
        self._end(child)
        if compact.summary is not None:
            self._messages_for(actor_key).append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "type": "text",
                            "content": (
                                "Compacted context"
                                f" ({compact.trigger or 'unknown'}): "
                                f"{redact(compact.summary)}"
                            ),
                        }
                    ],
                }
            )

    def _messages_for(self, actor_key: _ActorKey) -> list[dict[str, Any]]:
        return self._messages.setdefault(
            actor_key,
            self._with_system_message([]),
        )

    def _with_system_message(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        without_system = [
            message
            for message in messages
            if message.get("role") != "system"
        ]
        if self._system_prompt is None:
            return list(without_system)
        return [
            {"role": "system", "content": self._system_prompt},
            *without_system,
        ]

    def _all_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for actor_key in sorted(
            self._messages,
            key=lambda item: (item[0], item[1] or ""),
        ):
            messages.extend(self._messages[actor_key])
        return _message_payload(messages)

    def _close_chat(self, actor_key: _ActorKey) -> None:
        chat = self._open_chats.pop(actor_key, None)
        if chat is None:
            return
        self._messages_for(actor_key).append(chat.output_message)
        self._end(chat.observation)

    def _close_live_observations(self) -> None:
        for actor_key in list(self._open_chats):
            self._close_chat(actor_key)
        for tool_call in self._tool_calls.values():
            if tool_call.observation is None:
                continue
            self._update_observation(
                tool_call.observation,
                output={"status": "trace_closed_without_tool_result"},
                level="WARNING",
                status_message="Claude stream ended before the tool result",
            )
            self._end(tool_call.observation)
            tool_call.observation = None

    def _start_child(
        self,
        *,
        name: str,
        as_type: str,
        parent: Any,
        **values: Any,
    ) -> Any | None:
        try:
            return parent.start_observation(
                name=name,
                as_type=as_type,
                **{
                    key: value
                    for key, value in values.items()
                    if value is not None
                },
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

    @staticmethod
    def _update_observation(observation: Any, **values: Any) -> None:
        try:
            observation.update(
                **{
                    key: value
                    for key, value in values.items()
                    if value is not None
                }
            )
        except Exception:  # noqa: BLE001 - optional tracing must never block mining.
            return

    def _update(self, **values: Any) -> None:
        self._update_observation(self._observation, **values)

    def _progress_metadata(self) -> dict[str, Any]:
        return {
            **self._metadata,
            "live_responses": self._response_count,
            "live_tool_calls": self._tool_call_count,
            "live_tool_results": self._tool_result_count,
            "live_terminal_events": self._terminal_count,
            "live_permission_denials": self._permission_denial_count,
            "live_compactions": self._compact_count,
            "open_tools": sum(
                call.observation is not None
                for call in self._tool_calls.values()
            ),
            "terminal": self._last_terminal,
        }


@contextmanager
def trace_claude_task(
    task: AgentTask[Any],
    *,
    model_id: str,
) -> Iterator[ClaudeTaskTrace | None]:
    """Trace Claude with the same Agent/chat/tool hierarchy as Pydantic AI."""

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
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": task.agent_name,
    }
    initial_messages = [_user_text_message(task.prompt)]
    try:
        manager = langfuse.start_as_current_observation(
            name=f"{task.agent_name} run",
            as_type="agent",
            input=_message_payload(initial_messages),
            metadata=metadata,
        )
        observation = manager.__enter__()
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
        task_trace.close()
        try:
            manager.__exit__(type(error), error, error.__traceback__)
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        task_trace.close()
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def _runtime_usage(result: AgentRunResult[Any]) -> dict[str, int]:
    usage = result.usage
    if usage is None:
        return {}
    values = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "requests": usage.requests,
        "turns": usage.turns,
    }
    return {
        name: value
        for name, value in values.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _usage_details_from_mapping(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = _integer(value.get("input_tokens"))
    output_tokens = _integer(value.get("output_tokens"))
    cache_creation = _integer(value.get("cache_creation_input_tokens"))
    cache_read = _integer(value.get("cache_read_input_tokens"))
    values = {
        "input": input_tokens,
        "output": output_tokens,
        "input_cached_tokens": cache_read,
        "prompt_cache_hit_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }
    usage = {
        name: item
        for name, item in values.items()
        if item is not None
    }
    total = sum(
        item
        for item in (
            input_tokens,
            output_tokens,
            cache_creation,
            cache_read,
        )
        if item is not None
    )
    if usage:
        usage["total"] = total
    return usage or None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parent_tool_use_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("parent_tool_use_id")
    return value if isinstance(value, str) and value else None


def _display_tool_name(value: str) -> str:
    marker = "__"
    if value.startswith("mcp__") and marker in value[5:]:
        return value.rsplit(marker, 1)[-1]
    return value


def _user_text_message(value: Any) -> dict[str, Any]:
    return {
        "role": "user",
        "parts": [{"type": "text", "content": _string_content(value)}],
    }


def _assistant_message(message: Mapping[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    blocks = content if isinstance(content, list) else [
        {"type": "text", "text": content}
    ]
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            parts.append(
                {"type": "text", "content": _string_content(block)}
            )
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(
                {
                    "type": "text",
                    "content": _string_content(block.get("text")),
                }
            )
        elif block_type == "thinking":
            parts.append(
                {
                    "type": "thinking",
                    "content": _string_content(
                        block.get("thinking", block.get("text"))
                    ),
                }
            )
        elif block_type == "tool_use":
            parts.append(
                {
                    "type": "tool_call",
                    "id": str(block.get("id") or ""),
                    "name": _display_tool_name(
                        str(block.get("name") or "unknown")
                    ),
                    "arguments": json.dumps(
                        sanitize_json(block.get("input")),
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    ),
                }
            )
        else:
            parts.append(
                {
                    "type": str(block_type or "unknown"),
                    "content": _string_content(dict(block)),
                }
            )
    output: dict[str, Any] = {"role": "assistant", "parts": parts}
    finish_reason = message.get("stop_reason", message.get("finish_reason"))
    if isinstance(finish_reason, str) and finish_reason:
        output["finish_reason"] = finish_reason
    return output


def _merge_assistant_messages(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    parts = [
        dict(part) if isinstance(part, Mapping) else part
        for part in existing.get("parts", [])
    ]
    identities = {
        _part_identity(part): index
        for index, part in enumerate(parts)
        if isinstance(part, Mapping)
    }
    for part in incoming.get("parts", []):
        if not isinstance(part, Mapping):
            if part not in parts:
                parts.append(part)
            continue
        identity = _part_identity(part)
        if identity in identities:
            parts[identities[identity]] = dict(part)
        else:
            identities[identity] = len(parts)
            parts.append(dict(part))
    merged["parts"] = parts
    if incoming.get("finish_reason"):
        merged["finish_reason"] = incoming["finish_reason"]
    return merged


def _part_identity(part: Mapping[str, Any]) -> tuple[str, str]:
    part_type = str(part.get("type") or "unknown")
    if part_type == "tool_call":
        return part_type, str(part.get("id") or "")
    return part_type, str(part.get("content") or "")


def _string_content(value: Any) -> str:
    sanitized = sanitize_json(value)
    if isinstance(sanitized, str):
        return sanitized
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def _message_payload(value: Any) -> Any:
    """Redact and clip leaf strings without destroying message structure."""

    sanitized = sanitize_json(value)
    if isinstance(sanitized, Mapping):
        return {
            str(key): _message_payload(item)
            for key, item in sanitized.items()
        }
    if isinstance(sanitized, list):
        return [_message_payload(item) for item in sanitized]
    if isinstance(sanitized, str):
        return clip(sanitized, _EVENT_PAYLOAD_LIMIT)
    return sanitized


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
