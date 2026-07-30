"""Decode Claude's subprocess protocol and validate structured responses."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ...agent.contracts import AgentTask, OutputT, RuntimeUsage
from ...utils.log import logger
from .artifacts import clip, redact, sanitize_json
from .config import ClaudeCodeConfig
from .errors import (
    ClaudeCodeConfigurationError,
    ClaudeCodePermissionError,
    ClaudeCodeProcessError,
    ClaudeCodeProtocolError,
    ClaudeCodeProviderError,
    ClaudeCodeRequestLimitError,
)
from .process import ProcessResult

_MAX_LOGGED_OUTPUT_CHARS = 12_000


@dataclass(frozen=True)
class ParsedOutput:
    """One normalized Claude terminal response and its streamed evidence."""

    terminal: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    structured_output: Any
    structured_candidates: tuple[Any, ...]
    final_text: str | None
    usage: RuntimeUsage | None
    session_id: str | None
    model_id: str | None


@dataclass
class RequestCounter:
    """Track observable Claude model responses across repair attempts."""

    limit: int | None
    seen_message_ids: set[tuple[str | None, str]] = field(default_factory=set)
    anonymous_requests: int = 0

    @property
    def count(self) -> int:
        return len(self.seen_message_ids) + self.anonymous_requests

    def observe(self, event: Mapping[str, Any]) -> bool:
        if event.get("type") != "assistant":
            return False
        message = event.get("message")
        message_id = message.get("id") if isinstance(message, Mapping) else None
        if isinstance(message_id, str) and message_id:
            parent_tool_use_id = event.get("parent_tool_use_id")
            actor_id = (
                parent_tool_use_id
                if isinstance(parent_tool_use_id, str) and parent_tool_use_id
                else None
            )
            dedupe_key = (actor_id, message_id)
            if dedupe_key in self.seen_message_ids:
                return False
            self.seen_message_ids.add(dedupe_key)
        else:
            self.anonymous_requests += 1
        if self.limit is not None and self.count > self.limit:
            raise ClaudeCodeRequestLimitError(self.limit, observed=self.count)
        return True

    def reconcile_attempt(self, *, previous_count: int, reported_turns: int | None) -> None:
        """Account for responses absent from the selected output protocol."""

        if reported_turns is None:
            return
        observed_this_attempt = self.count - previous_count
        self.anonymous_requests += max(0, reported_turns - observed_this_attempt)
        if self.limit is not None and self.count > self.limit:
            raise ClaudeCodeRequestLimitError(self.limit, observed=self.count)


@dataclass
class StreamMonitor:
    """Emit bounded progress logs while enforcing the task request limit."""

    task_id: str
    attempt: int
    counter: RequestCounter
    event_handler: Callable[[Mapping[str, Any]], None] | None = None
    compact_events_path: Path | None = None
    seen_tool_use_ids: set[tuple[str | None, str]] = field(default_factory=set)
    subagent_types: dict[str, str] = field(default_factory=dict)
    _compact_event_offset: int = field(default=0, init=False)

    def observe_line(self, line: str) -> None:
        self._drain_compact_events()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return

        if self.counter.observe(event):
            limit = self.counter.limit if self.counter.limit is not None else "unbounded"
            actor, parent_tool_use_id = self._actor(event)
            logger.info(
                "Claude model request observed: task=%s attempt=%s requests=%s/%s "
                "actor=%s parent_tool_use_id=%s",
                self.task_id,
                self.attempt,
                self.counter.count,
                limit,
                actor,
                parent_tool_use_id,
            )

        actor, parent_tool_use_id = self._actor(event)
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                tool_use_id = block.get("id")
                tool_key = (
                    str(tool_use_id)
                    if tool_use_id
                    else json.dumps(block, sort_keys=True, default=str)
                )
                dedupe_key = (parent_tool_use_id, tool_key)
                if dedupe_key in self.seen_tool_use_ids:
                    continue
                self.seen_tool_use_ids.add(dedupe_key)
                tool_name = str(block.get("name") or "unknown")
                if tool_name in {"Agent", "Task"} and isinstance(tool_use_id, str):
                    tool_input = block.get("input")
                    agent_type = (
                        tool_input.get("subagent_type")
                        if isinstance(tool_input, Mapping)
                        else None
                    )
                    normalized_type = (
                        str(agent_type) if agent_type else "unknown"
                    )
                    self.subagent_types[tool_use_id] = normalized_type
                    logger.info(
                        "Claude subagent spawned: task=%s attempt=%s "
                        "subagent=%s parent_tool_use_id=%s",
                        self.task_id,
                        self.attempt,
                        normalized_type,
                        tool_use_id,
                    )
                logger.info(
                    "Claude tool call observed: task=%s attempt=%s tool=%s "
                    "actor=%s parent_tool_use_id=%s",
                    self.task_id,
                    self.attempt,
                    tool_name,
                    actor,
                    parent_tool_use_id,
                )
        self._observe_event(event)

    def finish(self) -> None:
        """Forward compact hook events that arrived with the final CLI output."""

        self._drain_compact_events()

    def _drain_compact_events(self) -> None:
        if self.event_handler is None or self.compact_events_path is None:
            return
        try:
            with self.compact_events_path.open("rb") as handle:
                handle.seek(self._compact_event_offset)
                pending = handle.read()
        except OSError:
            return
        if not pending:
            return

        consumed = 0
        for raw_line in pending.splitlines(keepends=True):
            if not raw_line.endswith((b"\n", b"\r")):
                break
            consumed += len(raw_line)
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            actor, parent_tool_use_id = self._actor(event)
            logger.info(
                "Claude context compacted: task=%s attempt=%s actor=%s "
                "parent_tool_use_id=%s trigger=%s",
                self.task_id,
                self.attempt,
                actor,
                parent_tool_use_id,
                event.get("trigger"),
            )
            self._forward_event(event, label="compact")
        self._compact_event_offset += consumed

    def _observe_event(self, event: Mapping[str, Any]) -> None:
        if event.get("type") == "result":
            actor, parent_tool_use_id = self._actor(event)
            output = event.get("structured_output")
            if output is None:
                output = event.get("result")
            if isinstance(output, str):
                rendered_output = redact(output)
            else:
                rendered_output = json.dumps(
                    sanitize_json(output),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            logger.info(
                "Claude terminal output: task=%s attempt=%s turns=%s reason=%s "
                "structured=%s output=%s actor=%s parent_tool_use_id=%s",
                self.task_id,
                self.attempt,
                event.get("num_turns"),
                event.get("terminal_reason"),
                event.get("structured_output") is not None,
                clip(rendered_output, _MAX_LOGGED_OUTPUT_CHARS),
                actor,
                parent_tool_use_id,
            )
        elif event.get("type") == "user":
            actor, parent_tool_use_id = self._actor(event)
            message = event.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list):
                for block in content:
                    if (
                        not isinstance(block, Mapping)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    tool_use_id = block.get("tool_use_id")
                    if (
                        isinstance(tool_use_id, str)
                        and tool_use_id in self.subagent_types
                    ):
                        actor = f"subagent:{self.subagent_types[tool_use_id]}"
                        parent_tool_use_id = tool_use_id
                    logger.info(
                        "Claude tool result observed: task=%s attempt=%s "
                        "tool_use_id=%s error=%s actor=%s parent_tool_use_id=%s",
                        self.task_id,
                        self.attempt,
                        tool_use_id,
                        bool(block.get("is_error")),
                        actor,
                        parent_tool_use_id,
                    )
        elif event.get("type") in {"task_notification", "task_result"}:
            actor, parent_tool_use_id = self._actor(event)
            logger.info(
                "Claude subagent lifecycle observed: task=%s attempt=%s actor=%s "
                "parent_tool_use_id=%s event=%s task_id=%s status=%s",
                self.task_id,
                self.attempt,
                actor,
                parent_tool_use_id,
                event.get("type"),
                event.get("task_id"),
                event.get("status"),
            )
        self._forward_event(event, label="stream")

    def _actor(self, event: Mapping[str, Any]) -> tuple[str, str | None]:
        parent_tool_use_id = event.get("parent_tool_use_id")
        if isinstance(parent_tool_use_id, str) and parent_tool_use_id:
            agent_type = self.subagent_types.get(parent_tool_use_id, "unknown")
            return f"subagent:{agent_type}", parent_tool_use_id
        agent_type = event.get("agent_type")
        if isinstance(agent_type, str) and agent_type:
            return f"subagent:{agent_type}", None
        if event.get("type") in {"task_notification", "task_result"}:
            task_id = event.get("task_id")
            if isinstance(task_id, str) and task_id in self.subagent_types:
                return f"subagent:{self.subagent_types[task_id]}", task_id
            return "subagent:unknown", None
        return "parent", None

    def _forward_event(self, event: Mapping[str, Any], *, label: str) -> None:
        if self.event_handler is not None:
            try:
                self.event_handler(event)
            except Exception:  # noqa: BLE001 - optional tracing must never stop Claude.
                logger.debug(
                    "Claude %s event handler failed: task=%s attempt=%s",
                    label,
                    self.task_id,
                    self.attempt,
                    exc_info=True,
                )


class ProtocolDecoder:
    """Normalize Claude JSON modes and classify protocol/provider failures."""

    def __init__(self, config: ClaudeCodeConfig) -> None:
        self._config = config

    def decode(
        self,
        process: ProcessResult,
        *,
        configured_model: str | None,
        turn_limit: int | None = None,
    ) -> ParsedOutput:
        stdout = process.stdout.decode("utf-8", errors="replace")
        stderr = process.stderr.decode("utf-8", errors="replace")
        events: list[Mapping[str, Any]] = []

        if self._config.output_format == "json":
            try:
                decoded = json.loads(stdout)
            except json.JSONDecodeError as exc:
                if process.returncode != 0:
                    self._raise_process_or_provider(process.returncode, stdout, stderr)
                raise ClaudeCodeProtocolError(
                    f"Claude Code did not emit valid JSON: {redact(clip(stdout or stderr, 1000))}"
                ) from exc
            if not isinstance(decoded, dict):
                raise ClaudeCodeProtocolError("Claude Code JSON output must be an object")
            events.append(decoded)
            terminal = decoded
        else:
            for line_number, line in enumerate(stdout.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError as exc:
                    if process.returncode != 0 and not events:
                        self._raise_process_or_provider(process.returncode, stdout, stderr)
                    raise ClaudeCodeProtocolError(
                        "invalid stream-json event on line "
                        f"{line_number}: {redact(clip(line, 500))}"
                    ) from exc
                if not isinstance(decoded, dict):
                    raise ClaudeCodeProtocolError(
                        f"stream-json event {line_number} must be an object"
                    )
                events.append(decoded)
            terminal = next(
                (event for event in reversed(events) if event.get("type") == "result"),
                None,
            )
            if terminal is None:
                if process.returncode != 0:
                    self._raise_process_or_provider(process.returncode, stdout, stderr)
                raise ClaudeCodeProtocolError(
                    "stream-json output did not contain a terminal result event"
                )

        permission_denials = terminal.get("permission_denials") or []
        if permission_denials:
            raise ClaudeCodePermissionError(
                f"Claude Code denied {len(permission_denials)} tool call(s) under dontAsk mode"
            )

        structured = terminal.get("structured_output")
        event_candidates = _structured_output_candidates(events)
        if (
            terminal.get("terminal_reason") == "budget_exhausted"
            or terminal.get("subtype") == "error_max_budget_usd"
        ):
            raise ClaudeCodeProviderError(
                "Claude Code reported an upstream budget exhaustion",
                category="budget",
            )
        if (
            terminal.get("terminal_reason") == "max_turns"
            or terminal.get("subtype") == "error_max_turns"
        ):
            observed = _optional_int(terminal.get("num_turns")) or turn_limit or 1
            raise ClaudeCodeRequestLimitError(turn_limit or observed, observed=observed)
        if (
            terminal.get("terminal_reason") == "structured_output_retry_exhausted"
            or terminal.get("subtype") == "error_max_structured_output_retries"
        ):
            raise ClaudeCodeProtocolError(
                "Claude Code exhausted its native structured-output retry limit"
            )
        if terminal.get("stop_reason") == "max_tokens":
            raise ClaudeCodeProtocolError(
                "Claude Code terminal response hit its output-token limit"
            )
        if terminal.get("is_error") or terminal.get("terminal_reason") == "api_error":
            self._raise_provider_error(terminal, stderr)
        if process.returncode != 0:
            self._raise_process_or_provider(
                process.returncode,
                stdout,
                stderr,
                terminal=terminal,
            )
        self.validate_initialization(events)

        final_value = terminal.get("result")
        final_text = final_value if isinstance(final_value, str) else None
        if structured is None and isinstance(final_value, (dict, list)):
            structured = final_value
        if structured is None and isinstance(final_value, str):
            structured = _parse_json_text(final_value)
        candidates = _dedupe_candidates(
            ([structured] if structured is not None else []) + list(event_candidates)
        )
        if structured is None and candidates:
            structured = candidates[-1]

        usage_value = terminal.get("usage")
        usage = None
        if isinstance(usage_value, dict):
            raw_model_usage = terminal.get("modelUsage")
            model_usage = (
                sanitize_json(raw_model_usage)
                if isinstance(raw_model_usage, Mapping)
                else {}
            )
            aggregate_input = _sum_model_usage_int(raw_model_usage, "inputTokens")
            aggregate_output = _sum_model_usage_int(raw_model_usage, "outputTokens")
            aggregate_cache_creation = _sum_model_usage_int(
                raw_model_usage,
                "cacheCreationInputTokens",
            )
            aggregate_cache_read = _sum_model_usage_int(
                raw_model_usage,
                "cacheReadInputTokens",
            )
            usage = RuntimeUsage(
                requests=_optional_int(terminal.get("num_turns")),
                turns=_optional_int(terminal.get("num_turns")),
                input_tokens=(
                    aggregate_input
                    if aggregate_input is not None
                    else _optional_int(usage_value.get("input_tokens"))
                ),
                output_tokens=(
                    aggregate_output
                    if aggregate_output is not None
                    else _optional_int(usage_value.get("output_tokens"))
                ),
                cache_creation_input_tokens=(
                    aggregate_cache_creation
                    if aggregate_cache_creation is not None
                    else _optional_int(usage_value.get("cache_creation_input_tokens"))
                ),
                cache_read_input_tokens=(
                    aggregate_cache_read
                    if aggregate_cache_read is not None
                    else _optional_int(usage_value.get("cache_read_input_tokens"))
                ),
                duration_ms=_optional_int(terminal.get("duration_ms")),
                model_usage=model_usage,
            )

        model_id = _model_from_usage(terminal.get("modelUsage")) or configured_model
        session_id = terminal.get("session_id")
        return ParsedOutput(
            terminal=terminal,
            events=tuple(events),
            structured_output=structured,
            structured_candidates=candidates,
            final_text=final_text,
            usage=usage,
            session_id=session_id if isinstance(session_id, str) else None,
            model_id=model_id,
        )

    @staticmethod
    def validate_initialization(
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        """Reject plugin and MCP initialization failures without later success."""

        successful_mcp_servers = _successful_mcp_servers(events)
        for event in events:
            if event.get("type") != "system" or event.get("subtype") != "init":
                continue
            plugin_errors = event.get("plugin_errors") or []
            if plugin_errors:
                raise ClaudeCodeConfigurationError(
                    f"Claude Code reported {len(plugin_errors)} plugin initialization error(s)"
                )
            mcp_servers = event.get("mcp_servers") or []
            failed = [
                server
                for server in mcp_servers
                if isinstance(server, dict)
                and str(server.get("status") or "").lower()
                not in {"", "connected", "pending", "starting", "initializing"}
                and str(server.get("name") or "") not in successful_mcp_servers
            ]
            if failed:
                names = ", ".join(
                    str(server.get("name", "unknown")) for server in failed
                )
                raise ClaudeCodeConfigurationError(
                    f"Claude Code failed to connect MCP server(s): {names}"
                )

    @staticmethod
    def _raise_provider_error(
        terminal: Mapping[str, Any],
        stderr: str,
    ) -> None:
        status = _optional_int(terminal.get("api_error_status"))
        result = terminal.get("result")
        message = result if isinstance(result, str) else stderr or "unknown provider failure"
        raise ClaudeCodeProviderError(
            redact(clip(message, 2000)),
            category=_provider_category(message, status),
            status_code=status,
        )

    @staticmethod
    def _raise_process_or_provider(
        returncode: int,
        stdout: str,
        stderr: str,
        *,
        terminal: Mapping[str, Any] | None = None,
    ) -> None:
        status = _optional_int(terminal.get("api_error_status")) if terminal else None
        terminal_result = terminal.get("result") if terminal else None
        message = terminal_result if isinstance(terminal_result, str) else stderr or stdout
        category = _provider_category(message, status)
        if category != "unknown" or status is not None:
            raise ClaudeCodeProviderError(
                redact(clip(message, 2000)),
                category=category,
                status_code=status,
            )
        safe_stderr = redact(clip(stderr, 2000))
        raise ClaudeCodeProcessError(
            f"Claude Code exited with status {returncode}: {safe_stderr or 'no stderr'}",
            returncode=returncode,
            stderr=safe_stderr,
        )


def validate_output(
    task: AgentTask[OutputT],
    parsed: ParsedOutput,
) -> tuple[OutputT | None, tuple[str, ...], str]:
    """Select and validate the strongest structured-output candidate."""

    candidates = parsed.structured_candidates or (
        (parsed.structured_output,) if parsed.structured_output is not None else ()
    )
    if not candidates:
        return None, ("structured_output is missing",), parsed.final_text or ""

    best: tuple[tuple[int, int, int, int], tuple[str, ...], str] | None = None
    for index, raw_candidate in enumerate(candidates):
        payload = _normalize_payload(task.output_type, raw_candidate)
        candidate_text = _candidate_text(payload, parsed.final_text)
        try:
            output = task.output_type.model_validate(payload)
        except ValidationError as exc:
            errors = tuple(
                _format_validation_error(item)
                for item in exc.errors(include_url=False)
            )
            score = (1, len(errors), -len(candidate_text), index)
        else:
            try:
                errors = task.validate_output(output)
            except Exception as exc:
                raise ClaudeCodeProtocolError(
                    f"task output validator raised: {redact(str(exc))}"
                ) from exc
            if not errors:
                return output, (), candidate_text
            score = (0, len(errors), -len(candidate_text), index)
        if best is None or score < best[0]:
            best = (score, tuple(errors), candidate_text)

    assert best is not None
    return None, best[1], best[2]


def aggregate_attempt_usage(
    attempts: Sequence[tuple[ParsedOutput, ProcessResult]],
    *,
    requests: int,
) -> RuntimeUsage:
    """Combine native usage from every schema-repair attempt."""

    usages = [parsed.usage for parsed, _ in attempts if parsed.usage is not None]
    model_usage: dict[str, Any] = {}
    for usage in usages:
        assert usage is not None
        _merge_numeric_usage(model_usage, usage.model_usage)
    return RuntimeUsage(
        requests=requests,
        turns=requests,
        input_tokens=sum_optional([usage.input_tokens for usage in usages]),
        output_tokens=sum_optional([usage.output_tokens for usage in usages]),
        cache_creation_input_tokens=sum_optional(
            [usage.cache_creation_input_tokens for usage in usages]
        ),
        cache_read_input_tokens=sum_optional(
            [usage.cache_read_input_tokens for usage in usages]
        ),
        duration_ms=sum(process.duration_ms for _, process in attempts),
        model_usage=model_usage,
    )


def validate_model_identity(
    attempts: Sequence[tuple[ParsedOutput, ProcessResult]],
) -> tuple[str, ...]:
    """Reject parent/child or repair attempts that report multiple models."""

    observed = {
        str(model_id)
        for parsed, _ in attempts
        if parsed.usage is not None
        for model_id in parsed.usage.model_usage
        if str(model_id).strip()
    }
    if len(observed) > 1:
        raise ClaudeCodeConfigurationError(
            "Claude parent/subagent model identity drifted within one task: "
            + ", ".join(sorted(observed))
        )
    return tuple(sorted(observed))


def subagent_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Extract a portable audit trail for native agent delegation."""

    recorded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                if block.get("name") not in {"Agent", "Task"}:
                    continue
                event_id = str(
                    block.get("id")
                    or json.dumps(block, sort_keys=True, default=str)
                )
                if event_id in seen:
                    continue
                seen.add(event_id)
                tool_input = block.get("input")
                parent_tool_use_id = event.get("parent_tool_use_id")
                recorded.append(
                    {
                        "event": "spawn",
                        "id": event_id,
                        "actor": (
                            "subagent"
                            if isinstance(parent_tool_use_id, str)
                            and parent_tool_use_id
                            else "parent"
                        ),
                        "parent_tool_use_id": (
                            parent_tool_use_id
                            if isinstance(parent_tool_use_id, str)
                            else None
                        ),
                        "agent_type": (
                            tool_input.get("subagent_type")
                            if isinstance(tool_input, Mapping)
                            else None
                        ),
                    }
                )
        if event.get("type") in {"task_notification", "task_result"}:
            recorded.append(
                {
                    "event": str(event.get("type")),
                    "actor": "subagent",
                    "parent_tool_use_id": event.get("parent_tool_use_id"),
                    "agent_type": event.get("agent_type"),
                    "task_id": event.get("task_id"),
                    "status": event.get("status"),
                }
            )
    return recorded


def sum_optional(values: Sequence[int | None]) -> int | None:
    """Sum optional integers when at least one value is present."""

    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _merge_numeric_usage(target: dict[str, Any], value: Mapping[str, Any]) -> None:
    additive = {
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
        "inputTokens",
        "outputTokens",
        "webSearchRequests",
    }
    for key, item in value.items():
        if isinstance(item, Mapping):
            nested = target.setdefault(str(key), {})
            if isinstance(nested, dict):
                _merge_numeric_usage(nested, item)
            continue
        if isinstance(item, bool):
            target[str(key)] = item
        elif isinstance(item, (int, float)):
            if key in additive:
                target[str(key)] = target.get(str(key), 0) + item
            elif key not in target:
                target[str(key)] = item
        elif key not in target:
            target[str(key)] = item


def _parse_json_text(value: str) -> Any | None:
    stripped = value.strip()
    candidates = [stripped]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json)?\s*(.*?)```",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _structured_output_candidates(
    events: Sequence[Mapping[str, Any]],
) -> tuple[Any, ...]:
    candidates: list[Any] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "tool_use"
                and block.get("name") == "StructuredOutput"
                and isinstance(block.get("input"), Mapping)
            ):
                candidates.append(dict(block["input"]))
    return tuple(candidates[-8:])


def _dedupe_candidates(candidates: Sequence[Any]) -> tuple[Any, ...]:
    deduped: list[Any] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = json.dumps(candidate, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            key = repr(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return tuple(deduped)


def _normalize_payload(output_type: type[BaseModel], payload: Any) -> Any:
    for _ in range(3):
        if not isinstance(payload, Mapping) or len(payload) != 1:
            break
        wrapper, nested = next(iter(payload.items()))
        if wrapper in output_type.model_fields:
            break
        if isinstance(nested, str):
            parsed = _parse_json_text(nested)
            if parsed is not None:
                nested = parsed
        if not isinstance(nested, (Mapping, list)):
            break
        payload = nested
    return payload


def _successful_mcp_servers(
    events: Sequence[Mapping[str, Any]],
) -> set[str]:
    tool_servers: dict[str, str] = {}
    successful: set[str] = set()
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                tool_use_id = block.get("id")
                name = block.get("name")
                if not isinstance(tool_use_id, str) or not isinstance(name, str):
                    continue
                parts = name.split("__", 2)
                if len(parts) == 3 and parts[0] == "mcp" and parts[1]:
                    tool_servers[tool_use_id] = parts[1]
            elif block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                server = (
                    tool_servers.get(tool_use_id)
                    if isinstance(tool_use_id, str)
                    else None
                )
                if server is None or block.get("is_error") is True:
                    continue
                result_content = block.get("content")
                if (
                    isinstance(result_content, str)
                    and "<tool_use_error>" in result_content
                ):
                    continue
                successful.add(server)
    return successful


def _format_validation_error(error: Mapping[str, Any]) -> str:
    location = ".".join(str(item) for item in error.get("loc", ())) or "output"
    return f"{location}: {error.get('msg', 'invalid value')}"


def _candidate_text(payload: Any, final_text: str | None) -> str:
    if payload is not None:
        with suppress(TypeError, ValueError):
            return json.dumps(payload, ensure_ascii=False, indent=2)
    return final_text or ""


def _provider_category(message: str, status_code: int | None) -> str:
    normalized = message.lower()
    if status_code in (401, 403) or any(
        marker in normalized
        for marker in (
            "not logged in",
            "authentication",
            "api key",
            "unauthorized",
            "forbidden",
        )
    ):
        return "authentication"
    if any(marker in normalized for marker in ("credit", "balance", "billing", "quota")):
        return "credits"
    if status_code == 429 or any(
        marker in normalized for marker in ("rate limit", "too many requests")
    ):
        return "rate_limit"
    if status_code == 404 or (
        "model" in normalized
        and any(marker in normalized for marker in ("not exist", "access", "unavailable"))
    ):
        return "model_unavailable"
    if "upstream" in normalized or (status_code is not None and status_code >= 500):
        return "upstream"
    if "api_error" in normalized or "provider" in normalized:
        return "provider"
    return "unknown"


def _model_from_usage(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    first = next(iter(value))
    return first if isinstance(first, str) else None


def _sum_model_usage_int(value: Any, key: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    numbers = [
        item[key]
        for item in value.values()
        if isinstance(item, Mapping)
        and isinstance(item.get(key), int)
        and not isinstance(item.get(key), bool)
    ]
    return sum(numbers) if numbers else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None
