"""Expected tool failures shared by runtime adapters.

Only explicitly classified failures become model feedback. A plain ValueError,
RuntimeError, OSError, or an invalid tool result remains a host failure.
"""

from __future__ import annotations

import errno

import httpx


class ToolInputError(ValueError):
    """The caller can correct an argument or choose a permitted operation."""


class ToolUnavailableError(RuntimeError):
    """An external evidence source is unavailable; the agent can use a fallback."""


def validate_text_argument(value: str, name: str) -> None:
    """Reject strings that filesystem/process APIs cannot represent."""
    if "\x00" in value:
        raise ToolInputError(f"{name} must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolInputError(f"{name} must contain valid Unicode characters") from exc


def tool_error_feedback(error: Exception) -> str | None:
    """Return feedback for known failures, or None for bugs/host failures."""

    if isinstance(error, ToolInputError):
        return f"Tool input error: {error}\nCorrect the arguments before calling again."
    if isinstance(error, OSError) and error.errno in {errno.ENAMETOOLONG, errno.E2BIG}:
        return "Tool input error: path or arguments exceed the operating system limit. Use a shorter value."
    if isinstance(error, httpx.LocalProtocolError):
        return None
    if isinstance(error, (ToolUnavailableError, httpx.HTTPError)):
        return f"Evidence source unavailable: {error}\nTry another source or retry within the remaining turn budget."
    return None
