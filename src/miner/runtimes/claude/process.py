"""Bounded Claude CLI subprocess execution."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    ClaudeCodeOutputLimitError,
    ClaudeCodeProcessError,
    ClaudeCodeTimeoutError,
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
)


def redact(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "<redacted>", result)
    return result


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + " ... [truncated]"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int


class ProcessRunner:
    def __init__(
        self,
        *,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        terminate_grace_seconds: float,
    ) -> None:
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.terminate_grace_seconds = terminate_grace_seconds

    async def _read(
        self,
        stream: asyncio.StreamReader,
        limit: int,
        label: str,
        line_handler: Callable[[str], None] | None = None,
    ) -> bytes:
        chunks: list[bytes] = []
        pending = bytearray()
        size = 0
        while chunk := await stream.read(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise ClaudeCodeOutputLimitError(label, limit)
            chunks.append(chunk)
            if line_handler is not None:
                pending.extend(chunk)
                while (newline := pending.find(b"\n")) >= 0:
                    raw = bytes(pending[:newline])
                    del pending[: newline + 1]
                    line_handler(raw.decode("utf-8", errors="replace").rstrip("\r"))
        if line_handler is not None and pending:
            line_handler(bytes(pending).decode("utf-8", errors="replace").rstrip("\r"))
        return b"".join(chunks)

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        prompt: str,
        timeout_seconds: float,
        stdout_line_handler: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise ClaudeCodeProcessError(
                f"failed to start Claude Code: {redact(str(exc))}",
                returncode=-1,
            ) from exc
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        async def collect() -> tuple[bytes, bytes, int]:
            stdout_task = asyncio.create_task(
                self._read(
                    process.stdout,
                    self.max_stdout_bytes,
                    "stdout",
                    stdout_line_handler,
                )
            )
            stderr_task = asyncio.create_task(self._read(process.stderr, self.max_stderr_bytes, "stderr"))
            try:
                stdout, stderr, returncode = await asyncio.gather(stdout_task, stderr_task, process.wait())
                return stdout, stderr, returncode
            except BaseException:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.terminate_grace_seconds)
                except (TimeoutError, ProcessLookupError):
                    process.kill()
                    await process.wait()
                raise

        try:
            stdout, stderr, returncode = await asyncio.wait_for(collect(), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise ClaudeCodeTimeoutError(timeout_seconds) from exc
        return ProcessResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=returncode,
            duration_ms=round((time.monotonic() - started) * 1000),
        )


__all__ = ["ProcessResult", "ProcessRunner", "clip", "redact"]
