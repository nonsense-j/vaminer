"""Single-pass Claude stream-json decoder and normalized diagnostics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ...agent.contracts import RuntimeEventType, RuntimeLogEvent, RuntimeUsage
from ...utils.log import RuntimeLog, safe_log_value
from .errors import (
    ClaudeCodeConfigurationError,
    ClaudeCodeProcessError,
    ClaudeCodeProtocolError,
)
from .process import ProcessResult, clip, redact

_EVENT_CHARS = 1_000
_EVENT_LINES = 15


@dataclass(frozen=True, slots=True)
class DecodedClaudeRun:
    output: BaseModel | None
    usage: RuntimeUsage
    events: tuple[RuntimeLogEvent, ...]
    validation_errors: tuple[str, ...] = ()
    candidate: Any = None


def _bounded(value: Any) -> str:
    safe = safe_log_value(value)
    if isinstance(safe, str):
        text = safe
    else:
        try:
            text = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(safe)
    text = redact(text)
    lines = text.splitlines()
    if len(lines) > _EVENT_LINES:
        text = "\n".join(lines[:_EVENT_LINES]) + "\n... [truncated]"
    return clip(text, _EVENT_CHARS)


def _emit(
    events: list[RuntimeLogEvent],
    kind: RuntimeEventType,
    content: Any,
    *,
    agent_name: str,
    runtime_log: RuntimeLog,
    detail: str | None = None,
) -> None:
    safe = _bounded(content)
    if kind == "output":
        events.append(RuntimeLogEvent(type=kind, content=safe))
    else:
        events.append(
            runtime_log.event(
                agent_name,
                kind,
                safe,
                detail=detail,
            )
        )


def _content_events(
    event: dict[str, Any],
    events: list[RuntimeLogEvent],
    *,
    agent_name: str,
    runtime_log: RuntimeLog,
) -> None:
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, dict) else event.get("content")
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "thinking":
            _emit(
                events,
                "thinking",
                block.get("thinking") or block.get("text") or "",
                agent_name=agent_name,
                runtime_log=runtime_log,
            )
        elif block_type == "text":
            _emit(
                events,
                "message",
                block.get("text") or "",
                agent_name=agent_name,
                runtime_log=runtime_log,
            )
        elif block_type == "tool_use":
            tool_name = str(block.get("name") or "unknown")
            tool_id = str(block.get("id") or "unknown")
            _emit(
                events,
                "tool.call",
                block.get("input") or {},
                agent_name=agent_name,
                runtime_log=runtime_log,
                detail=f"{tool_name}:{tool_id}",
            )
        elif block_type == "tool_result":
            _emit(
                events,
                "tool.result",
                block.get("content"),
                agent_name=agent_name,
                runtime_log=runtime_log,
                detail=str(block.get("tool_use_id") or "unknown"),
            )


def _result_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("structured_output")
    if payload is not None:
        return payload if isinstance(payload, dict) else None
    payload = event.get("result")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _validation_errors(exc: ValidationError) -> tuple[str, ...]:
    errors: list[str] = []
    for item in exc.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "output"
        errors.append(f"{location}: {item.get('msg', 'invalid value')}")
    return tuple(errors)


def _native_output_errors(event: dict[str, Any]) -> tuple[str, ...]:
    raw = event.get("errors")
    if raw is None:
        return ("Claude Code exhausted its native structured-output retry limit",)
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ClaudeCodeProtocolError("Claude structured-output errors must be an array of strings")
    errors = tuple(redact(item.strip()) for item in raw if item.strip())
    return errors or ("Claude Code exhausted its native structured-output retry limit",)


class ClaudeStreamDecoder:
    """Incrementally render JSONL events, then validate the terminal result."""

    def __init__(
        self,
        *,
        output_type: type[BaseModel],
        agent_name: str = "Claude Code",
        runtime_log: RuntimeLog | None = None,
        expected_mcp_server: str | None = None,
        expected_mcp_tools: tuple[str, ...] = (),
        session_mode: Literal["fresh", "resumed"] = "fresh",
    ) -> None:
        self.output_type = output_type
        self.agent_name = agent_name
        self.runtime_log = runtime_log or RuntimeLog()
        self.expected_mcp_server = expected_mcp_server
        self.expected_mcp_tools = expected_mcp_tools
        self.session_mode = session_mode
        self.events: list[RuntimeLogEvent] = []
        self.result_event: dict[str, Any] | None = None
        self.parsed_count = 0
        self.line_number = 0
        self.error: ClaudeCodeProtocolError | None = None

    def _validate_init(self, event: dict[str, Any]) -> None:
        if self.expected_mcp_server is None:
            return
        raw_servers = event.get("mcp_servers")
        servers = raw_servers if isinstance(raw_servers, list) else []
        status = next(
            (
                item.get("status")
                for item in servers
                if isinstance(item, dict) and item.get("name") == self.expected_mcp_server
            ),
            None,
        )
        if status != "connected":
            state = status if isinstance(status, str) and status else "missing"
            raise ClaudeCodeConfigurationError(
                f"Claude Code MCP server {self.expected_mcp_server!r} is {state}; expected connected"
            )

        raw_tools = event.get("tools")
        available = {item for item in raw_tools if isinstance(item, str)} if isinstance(raw_tools, list) else set()
        missing = sorted(set(self.expected_mcp_tools) - available)
        if missing:
            raise ClaudeCodeConfigurationError(
                "Claude Code did not expose required MCP tools: " + ", ".join(missing)
            )

    def feed_line(self, raw: str) -> None:
        """Decode and render one line as soon as the subprocess emits it."""
        self.line_number += 1
        if self.error is not None or not raw.strip():
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self.error = ClaudeCodeProtocolError(
                f"Claude stream line {self.line_number} is not JSON: {_bounded(raw)}"
            )
            return
        if not isinstance(event, dict):
            return
        self.parsed_count += 1
        event_type = str(event.get("type") or "")
        if event_type in {"assistant", "user"}:
            _content_events(
                event,
                self.events,
                agent_name=self.agent_name,
                runtime_log=self.runtime_log,
            )
        elif event_type in {"compact", "compaction"} or event.get("subtype") in {"compact", "compaction"}:
            _emit(
                self.events,
                "compaction",
                event.get("summary") or event,
                agent_name=self.agent_name,
                runtime_log=self.runtime_log,
            )
        elif event_type == "result":
            self.result_event = event
            if event.get("is_error") or event.get("subtype") not in {None, "success"}:
                _emit(
                    self.events,
                    "error",
                    event.get("result") or event,
                    agent_name=self.agent_name,
                    runtime_log=self.runtime_log,
                )
            else:
                _emit(
                    self.events,
                    "output",
                    _result_payload(event) or event.get("result") or "completed",
                    agent_name=self.agent_name,
                    runtime_log=self.runtime_log,
                )
        elif event_type == "system" and event.get("subtype") == "init":
            self._validate_init(event)
            _emit(
                self.events,
                "message",
                {
                    "model": event.get("model"),
                    "session": self.session_mode,
                    "mcp": self.expected_mcp_server,
                },
                agent_name=self.agent_name,
                runtime_log=self.runtime_log,
            )
        elif event_type == "error":
            _emit(
                self.events,
                "error",
                event,
                agent_name=self.agent_name,
                runtime_log=self.runtime_log,
            )

    def finish(self, process: ProcessResult) -> DecodedClaudeRun:
        """Validate the buffered terminal state after the subprocess exits."""
        if process.returncode != 0:
            detail = redact(clip(process.stderr or process.stdout, 2_000))
            raise ClaudeCodeProcessError(
                f"Claude Code exited with {process.returncode}: {detail}",
                returncode=process.returncode,
                stderr=detail,
            )
        if self.error is not None:
            raise self.error
        if self.parsed_count == 0:
            raise ClaudeCodeProtocolError(
                "Claude Code emitted no stream-json events: " + redact(clip(process.stderr, 1_000))
            )
        if self.result_event is None:
            raise ClaudeCodeProtocolError("Claude Code stream ended without a terminal result event")

        native_output_failure = (
            self.result_event.get("terminal_reason") == "structured_output_retry_exhausted"
            or self.result_event.get("subtype") == "error_max_structured_output_retries"
        )
        if not native_output_failure and (
            self.result_event.get("is_error")
            or self.result_event.get("subtype") not in {None, "success"}
        ):
            raise ClaudeCodeProtocolError(
                "Claude Code returned an error result: "
                + _bounded(self.result_event.get("result") or self.result_event)
            )
        usage_data = self.result_event.get("usage", {})
        if not isinstance(usage_data, dict):
            usage_data = {}
        turns = self.result_event.get("num_turns")
        usage = RuntimeUsage(
            requests=turns if isinstance(turns, int) else None,
            turns=turns if isinstance(turns, int) else None,
            input_tokens=(
                usage_data.get("input_tokens") if isinstance(usage_data.get("input_tokens"), int) else None
            ),
            output_tokens=(
                usage_data.get("output_tokens") if isinstance(usage_data.get("output_tokens"), int) else None
            ),
            cache_creation_input_tokens=(
                usage_data.get("cache_creation_input_tokens")
                if isinstance(usage_data.get("cache_creation_input_tokens"), int)
                else None
            ),
            cache_read_input_tokens=(
                usage_data.get("cache_read_input_tokens")
                if isinstance(usage_data.get("cache_read_input_tokens"), int)
                else None
            ),
            duration_ms=process.duration_ms,
        )

        candidate = self.result_event.get("structured_output")
        if candidate is None:
            candidate = self.result_event.get("result")
        if native_output_failure:
            return DecodedClaudeRun(
                output=None,
                usage=usage,
                events=tuple(self.events),
                validation_errors=_native_output_errors(self.result_event),
                candidate=candidate,
            )

        structured = _result_payload(self.result_event)
        if structured is None:
            message = (
                "Claude terminal result is not valid JSON"
                if isinstance(candidate, str) and candidate.strip()
                else "Claude terminal result did not contain a structured output object"
            )
            return DecodedClaudeRun(
                output=None,
                usage=usage,
                events=tuple(self.events),
                validation_errors=(message,),
                candidate=candidate,
            )
        try:
            output = self.output_type.model_validate(structured)
        except ValidationError as exc:
            return DecodedClaudeRun(
                output=None,
                usage=usage,
                events=tuple(self.events),
                validation_errors=_validation_errors(exc),
                candidate=structured,
            )
        return DecodedClaudeRun(
            output=output,
            usage=usage,
            events=tuple(self.events),
            candidate=structured,
        )


def decode_claude_stream(
    process: ProcessResult,
    *,
    output_type: type[BaseModel],
    agent_name: str = "Claude Code",
    runtime_log: RuntimeLog | None = None,
    expected_mcp_server: str | None = None,
    expected_mcp_tools: tuple[str, ...] = (),
    session_mode: Literal["fresh", "resumed"] = "fresh",
) -> DecodedClaudeRun:
    """Decode a completed process through the incremental decoder."""
    decoder = ClaudeStreamDecoder(
        output_type=output_type,
        agent_name=agent_name,
        runtime_log=runtime_log,
        expected_mcp_server=expected_mcp_server,
        expected_mcp_tools=expected_mcp_tools,
        session_mode=session_mode,
    )
    for raw in process.stdout.splitlines():
        decoder.feed_line(raw)
    return decoder.finish(process)


__all__ = ["ClaudeStreamDecoder", "DecodedClaudeRun", "decode_claude_stream"]
