"""Bounded Claude subprocess execution and cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .artifacts import redact
from .config import ClaudeCodeConfig
from .errors import (
    ClaudeCodeConfigurationError,
    ClaudeCodeOutputLimitError,
    ClaudeCodeProtocolError,
    ClaudeCodeTimeoutError,
)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int


class ProcessRunner:
    """Execute and terminate one isolated Claude process."""

    def __init__(self, config: ClaudeCodeConfig) -> None:
        self._config = config

    async def run(
        self,
        argv: Sequence[str],
        *,
        prompt: str,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
        stdout_path: Path | None,
        stderr_path: Path | None,
        stdout_line_handler: Callable[[str], None] | None,
    ) -> ProcessResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd.resolve()),
                env=dict(environment),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ClaudeCodeConfigurationError(f"failed to start Claude Code: {redact(str(exc))}") from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            await self._terminate(process, None)
            raise ClaudeCodeProtocolError("Claude Code subprocess pipes were not created")

        stdin_task = asyncio.create_task(self._write_stdin(process.stdin, prompt.encode("utf-8")))
        stdout_task = asyncio.create_task(
            self._read_limited(
                process.stdout,
                self._config.max_stdout_bytes,
                "stdout",
                sink_path=stdout_path,
                line_handler=stdout_line_handler,
            )
        )
        stderr_task = asyncio.create_task(
            self._read_limited(
                process.stderr,
                self._config.max_stderr_bytes,
                "stderr",
                sink_path=stderr_path,
            )
        )
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdin_task, stdout_task, stderr_task, wait_task)
        combined = asyncio.gather(*tasks)
        try:
            await asyncio.wait_for(asyncio.shield(combined), timeout_seconds)
        except TimeoutError as exc:
            await self._terminate(process, wait_task)
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(asyncio.CancelledError, Exception):
                await combined
            raise ClaudeCodeTimeoutError(timeout_seconds) from exc
        except BaseException:
            await self._terminate(process, wait_task)
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(asyncio.CancelledError, Exception):
                await combined
            raise

        duration_ms = round((time.monotonic() - started) * 1000)
        return ProcessResult(
            returncode=wait_task.result(),
            stdout=stdout_task.result(),
            stderr=stderr_task.result(),
            duration_ms=duration_ms,
        )

    @staticmethod
    async def _write_stdin(stream: asyncio.StreamWriter, data: bytes) -> None:
        try:
            stream.write(data)
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            stream.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await stream.wait_closed()

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader,
        limit_bytes: int,
        stream_name: str,
        *,
        sink_path: Path | None = None,
        line_handler: Callable[[str], None] | None = None,
    ) -> bytes:
        result = bytearray()
        pending_sink = bytearray()
        sink = None

        def persist_complete_lines(*, final: bool = False) -> None:
            nonlocal sink
            while True:
                newline = pending_sink.find(b"\n")
                if newline < 0:
                    break
                raw_line = bytes(pending_sink[: newline + 1])
                del pending_sink[: newline + 1]
                text_line = raw_line.decode("utf-8", errors="replace")
                if sink_path is not None:
                    if sink is None:
                        sink = sink_path.open("ab", buffering=0)
                    sink.write(redact(text_line).encode("utf-8"))
                if line_handler is not None:
                    line_handler(text_line.rstrip("\r\n"))
            if final and pending_sink:
                text_line = bytes(pending_sink).decode("utf-8", errors="replace")
                pending_sink.clear()
                if sink_path is not None:
                    if sink is None:
                        sink = sink_path.open("ab", buffering=0)
                    sink.write(redact(text_line).encode("utf-8"))
                if line_handler is not None:
                    line_handler(text_line)

        try:
            while chunk := await stream.read(64 * 1024):
                remaining = limit_bytes - len(result)
                accepted = chunk[: max(remaining, 0)]
                result.extend(accepted)
                pending_sink.extend(accepted)
                persist_complete_lines()
                if len(chunk) > remaining:
                    persist_complete_lines(final=True)
                    raise ClaudeCodeOutputLimitError(stream_name, limit_bytes)
            persist_complete_lines(final=True)
        finally:
            if sink is not None:
                sink.close()
        return bytes(result)

    async def _terminate(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int] | None,
    ) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return

        waiter = wait_task or asyncio.create_task(process.wait())
        try:
            await asyncio.wait_for(asyncio.shield(waiter), self._config.terminate_grace_seconds)
            return
        except TimeoutError:
            pass

        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), 5.0)
