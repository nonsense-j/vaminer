"""Progress callbacks used by the interactive preflight CLI."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

ProgressCallback = Callable[[str], None]


def notify(progress: ProgressCallback | None, message: str) -> None:
    if progress is None:
        return
    try:
        progress(message)
    except Exception:  # noqa: BLE001 - diagnostics must never affect the check.
        return


def start_heartbeat(
    progress: ProgressCallback | None,
    *,
    message: str,
    timeout_seconds: float,
    interval_seconds: float = 10.0,
) -> asyncio.Task[None] | None:
    if progress is None:
        return None

    async def pulse() -> None:
        started = time.monotonic()
        while True:
            await asyncio.sleep(interval_seconds)
            elapsed = round(time.monotonic() - started)
            notify(progress, f"{message}; still waiting ({elapsed}s elapsed, timeout {timeout_seconds:g}s)")

    return asyncio.create_task(pulse())


async def stop_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


__all__ = ["ProgressCallback", "notify", "start_heartbeat", "stop_heartbeat"]
