"""Shared console and run-file logging for the Miner."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from enum import Enum
from io import StringIO
from itertools import islice
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from ..agent.contracts import RuntimeEventType, RuntimeLogEvent
LOG_LEVEL = logging.INFO
LOG_FORMAT = "[%(levelname)s] %(filename)s:%(funcName)s -> %(message)s"
_RAW_RECORD_ATTRIBUTE = "_miner_raw"
_FILE_ONLY_RECORD_ATTRIBUTE = "_miner_file_only"
_SENSITIVE_KEY = re.compile(
    r"(?:api[-_]?key|authorization|auth[-_]?token|access[-_]?token|refresh[-_]?token|"
    r"github[-_]?token|openai[-_]?token|password|passwd|secret|cookie)",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;\"'}\]]+|"
    r"\b(?:sk|ghp|github_pat)[-_][A-Za-z0-9_-]{8,}\b"
)
_MAX_COLLECTION_ITEMS = 50
_MAX_VALUE_DEPTH = 6
_EVENT_STYLE = {
    "thinking": ("🧠 Thinking", "yellow"),
    "message": ("💬 Message", "green"),
    "tool.call": ("🔧 Tool Call", "magenta"),
    "tool.result": ("🔧 Tool Output", "magenta1"),
    "compaction": ("🗜 Compaction", "blue"),
    "output": ("📦 Output", "cyan"),
    "error": ("❌ Error", "red"),
}
class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, _FILE_ONLY_RECORD_ATTRIBUTE, False)


class _ConsoleHandler(logging.StreamHandler):
    """Marker type preventing duplicate console handlers."""


class _RunFormatter(logging.Formatter):
    """Keep Rich panels intact while formatting ordinary miner records."""

    def __init__(self) -> None:
        super().__init__(LOG_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, _RAW_RECORD_ATTRIBUTE, False):
            return record.getMessage()
        return super().format(record)


logger = logging.getLogger("MINER")
logger.setLevel(LOG_LEVEL)
logger.propagate = False

if not any(isinstance(handler, _ConsoleHandler) for handler in logger.handlers):
    console_handler = _ConsoleHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(_RunFormatter())
    console_handler.addFilter(_ConsoleFilter())
    logger.addHandler(console_handler)


def _log_renderable(rendered: str) -> None:
    """Write an already-rendered Rich panel to the active run file only."""
    logger.info(
        rendered.rstrip("\n"),
        extra={
            _RAW_RECORD_ATTRIBUTE: True,
            _FILE_ONLY_RECORD_ATTRIBUTE: True,
        },
    )


def _redact_text(value: str) -> str:
    return _SECRET.sub(lambda match: (match.group(1) or "") + "<redacted>", value)


def safe_log_value(value: Any, *, depth: int = 0) -> Any:
    """Return bounded, JSON-compatible diagnostic data with secrets removed."""
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
        return safe_log_value(value.value, depth=depth + 1)
    if isinstance(value, BaseModel):
        try:
            dumped = value.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - diagnostics fall back to Python-mode data.
            dumped = value.model_dump()
        return safe_log_value(dumped, depth=depth + 1)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": _redact_text(str(value))}
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in islice(value.items(), _MAX_COLLECTION_ITEMS):
            key_text = str(key)
            sanitized[key_text] = (
                "<redacted>" if _SENSITIVE_KEY.search(key_text) else safe_log_value(item, depth=depth + 1)
            )
        if len(value) > _MAX_COLLECTION_ITEMS:
            sanitized["<truncated>"] = f"{len(value) - _MAX_COLLECTION_ITEMS} more items"
        return sanitized
    if isinstance(value, Sequence):
        sanitized_items = [
            safe_log_value(item, depth=depth + 1) for item in islice(value, _MAX_COLLECTION_ITEMS)
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            sanitized_items.append(f"<{len(value) - _MAX_COLLECTION_ITEMS} more items>")
        return sanitized_items
    if is_dataclass(value) and not isinstance(value, type):
        return safe_log_value(
            {field.name: getattr(value, field.name) for field in fields(value)},
            depth=depth + 1,
        )
    return _redact_text(str(value))


def _format_value(value: Any, *, body_limit: int, lines_limit: int) -> Text:
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if len(value) <= body_limit * 10 else None
        except (json.JSONDecodeError, TypeError):
            rendered = value
        else:
            rendered = value if parsed is None else json.dumps(safe_log_value(parsed), ensure_ascii=False, indent=2)
    else:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    lines = rendered.splitlines()
    truncated = len(rendered) > body_limit or len(lines) > lines_limit
    if len(lines) > lines_limit:
        rendered = "\n".join(lines[:lines_limit])
    if len(rendered) > body_limit:
        rendered = rendered[:body_limit]
    if truncated:
        rendered += "\n... [truncated]"
    return Text(rendered)


class RuntimeLog:
    """Uniform Rich stdout and plain-text run-file diagnostics for Runtime Adapters."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        emit_console: bool = True,
        ansi_transport: bool = False,
        width: int = 120,
        body_limit: int = 1_000,
        lines_limit: int = 15,
    ) -> None:
        self.console = (console or Console(width=width)) if emit_console else None
        self.ansi_transport = ansi_transport
        self.width = width
        self.body_limit = body_limit
        self.lines_limit = lines_limit

    def _emit(
        self,
        title: str,
        value: Any,
        *,
        border_style: str,
        panel_box: box.Box = box.ROUNDED,
    ) -> Any:
        """Best-effort diagnostics must never affect Runtime execution."""
        try:
            safe = safe_log_value(value)
            panel = Panel(
                _format_value(
                    safe,
                    body_limit=self.body_limit,
                    lines_limit=self.lines_limit,
                ),
                title=title,
                border_style=border_style,
                box=panel_box,
                padding=(0, 1),
            )
            if self.console is not None:
                self.console.print(panel)
            buffer = StringIO()
            if self.ansi_transport:
                Console(
                    file=buffer,
                    width=self.width,
                    color_system="truecolor",
                    force_terminal=True,
                    no_color=False,
                ).print(panel)
            else:
                Console(file=buffer, width=self.width, color_system=None, force_terminal=False).print(panel)
            _log_renderable(buffer.getvalue())
        except Exception:
            logger.debug("Failed to emit Runtime diagnostics", exc_info=True)
            return "<diagnostic unavailable>"
        return safe

    def relay(self, rendered: str) -> None:
        """Relay an ANSI Rich transport line to this console and the plain run log."""
        try:
            decoded = Text.from_ansi(rendered.rstrip("\n"))
            if self.console is not None:
                self.console.print(decoded)
            buffer = StringIO()
            Console(file=buffer, width=self.width, color_system=None, force_terminal=False).print(decoded)
            _log_renderable(buffer.getvalue())
        except Exception:
            logger.debug("Failed to relay Runtime diagnostics", exc_info=True)

    def started(self, agent_name: str, content: Any) -> None:
        self._emit(
            f"[bold]🤖 {escape(agent_name)} Started[/bold]",
            content,
            border_style="cyan",
            panel_box=box.DOUBLE,
        )

    def event(
        self,
        agent_name: str,
        event_type: RuntimeEventType,
        content: Any,
        *,
        detail: str | None = None,
    ) -> RuntimeLogEvent:
        label, border_style = _EVENT_STYLE[event_type]
        suffix = f" ({escape(detail)})" if detail else ""
        safe = self._emit(
            f"🤖 {escape(agent_name)} — {label}{suffix}",
            content,
            border_style=border_style,
        )
        return RuntimeLogEvent(type=event_type, content=safe)

    def finished(self, agent_name: str, content: Any) -> None:
        self._emit(
            f"[bold]🤖 {escape(agent_name)} Finished[/bold]",
            content,
            border_style="cyan",
            panel_box=box.DOUBLE,
        )

    def failed(self, agent_name: str, error: BaseException) -> None:
        self._emit(
            f"[bold]🤖 {escape(agent_name)} Failed[/bold]",
            error,
            border_style="red",
            panel_box=box.DOUBLE,
        )


def active_run_log_path() -> Path | None:
    """Return the run file currently attached to the shared Miner logger."""

    for handler in reversed(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            return Path(handler.baseFilename).resolve()
    return None


@contextmanager
def mirror_run_log_file(path: Path | None) -> Iterator[None]:
    """Append subprocess diagnostics to an existing parent-owned run log."""

    resolved = path.expanduser().resolve() if path is not None else None
    if resolved is None or not resolved.is_file():
        yield
        return
    file_handler = logging.FileHandler(
        resolved,
        mode="a",
        encoding="utf-8",
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(_RunFormatter())
    logger.addHandler(file_handler)
    try:
        yield
    finally:
        logger.removeHandler(file_handler)
        file_handler.close()


@contextmanager
def run_log_file(
    log_root: Path,
    vas_id: str,
    *,
    input_id: str,
    trace_id: str,
    runtime: str,
) -> Iterator[Path]:
    """Attach one append-only log file for the duration of a VAS run."""
    run_dir = log_root / vas_id / input_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{trace_id}__{runtime}"
    log_path = run_dir / f"{stem}.log"
    sequence = 1
    while log_path.exists():
        log_path = run_dir / f"{stem}-{sequence}.log"
        sequence += 1
    log_path.touch(exist_ok=False)

    file_handler = logging.FileHandler(
        log_path,
        mode="a",
        encoding="utf-8",
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(_RunFormatter())
    logger.addHandler(file_handler)
    try:
        yield log_path
    finally:
        logger.removeHandler(file_handler)
        file_handler.close()
