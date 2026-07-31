"""Rich command-line observability for the Pydantic AI adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from io import StringIO
from itertools import islice
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Hooks, ValidatedToolArgs
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import ToolDefinition
from rich import box
from rich.console import Console, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from ...utils.log import log_renderable, logger
from .context import MinerContext

_SENSITIVE_KEY = re.compile(
    r"(?:api[-_]?key|authorization|auth[-_]?token|access[-_]?token|refresh[-_]?token|"
    r"github[-_]?token|openai[-_]?token|password|passwd|secret|cookie)",
    re.IGNORECASE,
)
_BEARER_SECRET = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}")
_OPENAI_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_MAX_COLLECTION_ITEMS = 50
_MAX_VALUE_DEPTH = 6


def _agent_name(ctx: RunContext[Any]) -> str:
    if ctx.agent is None:
        return "Unknown Agent"
    return ctx.agent.name or "Unnamed Agent"


def _redact_text(value: str) -> str:
    value = _BEARER_SECRET.sub(r"\1<redacted>", value)
    return _OPENAI_SECRET.sub("<redacted>", value)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Convert diagnostics into bounded, JSON-compatible, redacted values."""
    if depth >= _MAX_VALUE_DEPTH:
        return "<max depth reached>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _safe_value(value.value, depth=depth + 1)
    if isinstance(value, BaseModel):
        try:
            dumped = value.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - diagnostics fall back to Python-mode data.
            dumped = value.model_dump()
        return _safe_value(dumped, depth=depth + 1)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": _redact_text(str(value)),
        }
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in islice(value.items(), _MAX_COLLECTION_ITEMS):
            key_text = str(key)
            sanitized[key_text] = (
                "<redacted>" if _SENSITIVE_KEY.search(key_text) else _safe_value(item, depth=depth + 1)
            )
        if len(value) > _MAX_COLLECTION_ITEMS:
            sanitized["<truncated>"] = f"{len(value) - _MAX_COLLECTION_ITEMS} more items"
        return sanitized
    if isinstance(value, Sequence):
        sanitized_items = [_safe_value(item, depth=depth + 1) for item in islice(value, _MAX_COLLECTION_ITEMS)]
        if len(value) > _MAX_COLLECTION_ITEMS:
            sanitized_items.append(f"<{len(value) - _MAX_COLLECTION_ITEMS} more items>")
        return sanitized_items
    if is_dataclass(value) and not isinstance(value, type):
        return _safe_value(
            {field.name: getattr(value, field.name) for field in fields(value)},
            depth=depth + 1,
        )
    return _redact_text(str(value))


def _truncate(text: str, *, body_limit: int, lines_limit: int) -> str:
    lines = text.splitlines()
    truncated = len(text) > body_limit or len(lines) > lines_limit
    if len(lines) > lines_limit:
        text = "\n".join(lines[:lines_limit])
    if len(text) > body_limit:
        text = text[:body_limit]
    if truncated:
        text += "\n... [truncated]"
    return text


def _format_value(value: Any, *, body_limit: int, lines_limit: int) -> Text:
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if len(value) <= body_limit * 10 else None
        except (json.JSONDecodeError, TypeError):
            rendered = _redact_text(value)
        else:
            rendered = (
                _redact_text(value)
                if parsed is None
                else json.dumps(
                    _safe_value(parsed),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
    else:
        rendered = json.dumps(
            _safe_value(value),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    return Text(_truncate(rendered, body_limit=body_limit, lines_limit=lines_limit))


class _CliLogSink:
    """Print Rich renderables and send plain text through the shared run logger."""

    def __init__(
        self,
        *,
        console: Console | None,
        width: int,
    ) -> None:
        self.console = console
        self.width = width

    def emit(self, renderable: RenderableType) -> None:
        """Best-effort diagnostics must never affect agent execution."""
        try:
            if self.console is not None:
                self.console.print(renderable)
            buffer = StringIO()
            file_console = Console(
                file=buffer,
                width=self.width,
                color_system=None,
                force_terminal=False,
            )
            file_console.print(renderable)
            log_renderable(buffer.getvalue())
        except Exception:
            logger.debug("Failed to emit agent CLI diagnostics", exc_info=True)


def make_cli_hooks(
    *,
    console: Console | None = None,
    emit_console: bool = True,
    width: int = 120,
    body_limit: int = 1_000,
    lines_limit: int = 15,
) -> Hooks[MinerContext]:
    """Build eager, observe-only hooks for console and active run-file diagnostics."""
    sink = _CliLogSink(
        console=(console or Console(width=width)) if emit_console else None,
        width=width,
    )
    hooks = Hooks[MinerContext](defer_loading=False)

    def panel(
        title: str,
        value: Any,
        *,
        border_style: str,
        panel_box: box.Box = box.ROUNDED,
    ) -> Panel:
        return Panel(
            _format_value(
                value,
                body_limit=body_limit,
                lines_limit=lines_limit,
            ),
            title=title,
            border_style=border_style,
            box=panel_box,
            padding=(0, 1),
        )

    @hooks.on.before_run
    async def before_run(ctx: RunContext[MinerContext]) -> None:
        agent_name = _agent_name(ctx)
        sink.emit(
            panel(
                f"[bold]🤖 {escape(agent_name)} Started",
                {"run_id": ctx.run_id, "prompt": ctx.prompt},
                border_style="cyan",
                panel_box=box.DOUBLE,
            ),
        )

    @hooks.on.after_model_request
    async def after_model_request(
        ctx: RunContext[MinerContext],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del request_context
        agent_name = _agent_name(ctx)
        for part in response.parts:
            if isinstance(part, ThinkingPart) and part.content:
                sink.emit(
                    panel(
                        f"🤖 {escape(agent_name)} — 🧠 Thinking",
                        part.content,
                        border_style="yellow",
                    ),
                )
            elif isinstance(part, TextPart):
                sink.emit(
                    panel(
                        f"🤖 {escape(agent_name)} — 💬 Message",
                        part.content or "(empty)",
                        border_style="green",
                    ),
                )
        return response

    @hooks.on.model_request_error
    async def model_request_error(
        ctx: RunContext[MinerContext],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        del request_context
        agent_name = _agent_name(ctx)
        sink.emit(
            panel(
                f"🤖 {escape(agent_name)} — Model Request Failed",
                error,
                border_style="red",
            ),
        )
        raise error

    @hooks.on.before_tool_execute
    async def before_tool_execute(
        ctx: RunContext[MinerContext],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        del tool_def
        agent_name = _agent_name(ctx)
        tool_id = f"{call.tool_name}:{call.tool_call_id}"
        sink.emit(
            panel(
                f"🤖 {escape(agent_name)} — 🔧 Tool Call ({escape(tool_id)})",
                args,
                border_style="magenta",
            ),
        )
        return args

    @hooks.on.after_tool_execute
    async def after_tool_execute(
        ctx: RunContext[MinerContext],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        result: Any,
    ) -> Any:
        del tool_def, args
        agent_name = _agent_name(ctx)
        tool_id = f"{call.tool_name}:{call.tool_call_id}"
        sink.emit(
            panel(
                f"🤖 {escape(agent_name)} — 🔧 Tool Output ({escape(tool_id)})",
                result,
                border_style="magenta1",
            ),
        )
        return result

    @hooks.on.tool_execute_error
    async def tool_execute_error(
        ctx: RunContext[MinerContext],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        error: Exception,
    ) -> Any:
        del tool_def, args
        agent_name = _agent_name(ctx)
        tool_id = f"{call.tool_name}:{call.tool_call_id}"
        sink.emit(
            panel(
                f"🤖 {escape(agent_name)} — Tool Failed ({escape(tool_id)})",
                error,
                border_style="red",
            ),
        )
        raise error

    @hooks.on.after_run
    async def after_run(
        ctx: RunContext[MinerContext],
        *,
        result: AgentRunResult[Any],
    ) -> AgentRunResult[Any]:
        agent_name = _agent_name(ctx)
        sink.emit(
            panel(
                f"[bold]🤖 {escape(agent_name)} Finished",
                {
                    "run_id": result.run_id,
                    "output": result.output,
                    "usage": result.usage,
                },
                border_style="cyan",
                panel_box=box.DOUBLE,
            ),
        )
        return result

    @hooks.on.run_error
    async def run_error(
        ctx: RunContext[MinerContext],
        *,
        error: BaseException,
    ) -> AgentRunResult[Any]:
        agent_name = _agent_name(ctx)
        sink.emit(
            panel(
                f"[bold]🤖 {escape(agent_name)} Failed",
                error,
                border_style="red",
                panel_box=box.DOUBLE,
            ),
        )
        raise error

    return hooks
