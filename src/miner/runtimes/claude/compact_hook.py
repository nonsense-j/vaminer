"""Persist one Claude PostCompact hook event for the parent runtime."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_MAX_HOOK_INPUT_BYTES = 2 * 1024 * 1024
_MAX_SUMMARY_CHARS = 100_000


def _compact_event(value: Any) -> dict[str, Any]:
    event = value if isinstance(value, dict) else {}
    summary = event.get("compact_summary")
    if not isinstance(summary, str):
        summary = ""
    truncated = len(summary) > _MAX_SUMMARY_CHARS
    return {
        "type": "hook",
        "hook_event_name": "PostCompact",
        "trigger": event.get("trigger"),
        "compact_summary": summary[:_MAX_SUMMARY_CHARS],
        "summary_truncated": truncated,
        "session_id": event.get("session_id"),
        "parent_tool_use_id": event.get("parent_tool_use_id"),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
    }


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        return 2

    raw = sys.stdin.buffer.read(_MAX_HOOK_INPUT_BYTES + 1)
    if len(raw) > _MAX_HOOK_INPUT_BYTES:
        return 2
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 2

    path = Path(args[0])
    payload = (
        json.dumps(
            _compact_event(decoded),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    with os.fdopen(descriptor, "ab", buffering=0) as handle:
        handle.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
