"""Failure modes exposed by the Claude runtime adapter."""

from __future__ import annotations

from collections.abc import Sequence


class ClaudeCodeError(RuntimeError):
    """Base class for Claude adapter failures."""


class ClaudeCodeConfigurationError(ClaudeCodeError):
    """Raised when executable or runtime-owned inputs are invalid."""


class ClaudeCodeProcessError(ClaudeCodeError):
    """Raised when Claude exits without a usable protocol result."""

    def __init__(self, message: str, *, returncode: int, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class ClaudeCodeTimeoutError(ClaudeCodeError):
    """Raised after terminating a timed-out Claude process group."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"Claude Code timed out after {timeout_seconds:g} seconds")
        self.timeout_seconds = timeout_seconds


class ClaudeCodeOutputLimitError(ClaudeCodeError):
    """Raised after terminating Claude for exceeding a captured stream limit."""

    def __init__(self, stream_name: str, limit_bytes: int) -> None:
        super().__init__(f"Claude Code {stream_name} exceeded the {limit_bytes}-byte limit")
        self.stream_name = stream_name
        self.limit_bytes = limit_bytes


class ClaudeCodeRequestLimitError(ClaudeCodeError):
    """Raised after the observable model-request limit is exceeded."""

    def __init__(self, limit: int, *, observed: int) -> None:
        super().__init__(
            f"Claude Code exceeded the per-task model request limit of {limit}; "
            f"observed request {observed}"
        )
        self.limit = limit
        self.observed = observed


class ClaudeCodeProtocolError(ClaudeCodeError):
    """Raised when stdout does not implement the expected Claude result protocol."""


class ClaudeCodeProviderError(ClaudeCodeError):
    """Raised for authentication, model, credit, rate-limit, or upstream failures."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
    ) -> None:
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"Claude Code provider {category} error{suffix}: {message}")
        self.category = category
        self.status_code = status_code


class ClaudeCodeValidationError(ClaudeCodeError):
    """Raised when all bounded structured-output repair attempts fail."""

    def __init__(self, errors: Sequence[str], *, attempts: int) -> None:
        detail = "\n- ".join(errors)
        super().__init__(f"Claude Code output remained invalid after {attempts} attempt(s):\n- {detail}")
        self.errors = tuple(errors)
        self.attempts = attempts
