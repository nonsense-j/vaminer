"""Langfuse authentication, propagation, and ingestion checks."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from ..utils.telemetry import configure_tracing
from .models import CheckResult
from .progress import ProgressCallback, notify, start_heartbeat, stop_heartbeat


def tracing_requested(environment: dict[str, str] | None = None) -> bool:
    values = os.environ if environment is None else environment
    enabled = values.get("LANGFUSE_TRACING_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    return enabled and bool(values.get("LANGFUSE_PUBLIC_KEY") or values.get("LANGFUSE_SECRET_KEY"))


def check_langfuse(environment: dict[str, str] | None = None) -> tuple[CheckResult, Any | None]:
    values = os.environ if environment is None else environment
    if values.get("LANGFUSE_TRACING_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return CheckResult.skipped("langfuse.auth", "Tracing is explicitly disabled"), None

    public_key = values.get("LANGFUSE_PUBLIC_KEY")
    secret_key = values.get("LANGFUSE_SECRET_KEY")
    if not public_key and not secret_key:
        return CheckResult.skipped("langfuse.auth", "Optional Langfuse tracing is not configured"), None
    if not public_key or not secret_key:
        missing = "LANGFUSE_PUBLIC_KEY" if not public_key else "LANGFUSE_SECRET_KEY"
        return (
            CheckResult.failed(
                "langfuse.auth",
                "Langfuse configuration is incomplete",
                detail=f"missing {missing}",
            ),
            None,
        )

    started = time.monotonic()
    client = configure_tracing()
    if client is None:
        return CheckResult.failed(
            "langfuse.auth",
            "Langfuse credentials or endpoint authentication failed",
            detail="Check LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL, and proxy settings",
        ), None
    return (
        CheckResult.passed(
            "langfuse.auth",
            "Langfuse authentication succeeded",
            duration_ms=round((time.monotonic() - started) * 1000),
        ),
        client,
    )


async def check_trace_ingestion(
    client: Any | None,
    *,
    trace_id: str | None,
    minimum_observations: int,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
) -> CheckResult:
    if client is None or trace_id is None:
        return CheckResult.skipped("langfuse.trace", "No active Langfuse client; trace probe was not required")

    started = time.monotonic()
    try:
        await asyncio.to_thread(client.flush)
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed("langfuse.trace", "Langfuse flush failed", detail=f"{type(exc).__name__}: {exc}")
    notify(progress, f"Langfuse flush completed; polling trace {trace_id} ...")

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    last_error: Exception | None = None
    heartbeat = start_heartbeat(
        progress,
        message=f"Langfuse trace {trace_id} is not visible yet",
        timeout_seconds=timeout_seconds,
        interval_seconds=10.0,
    )
    try:
        while time.monotonic() < deadline:
            try:
                trace = await asyncio.to_thread(
                    client.api.trace.get,
                    trace_id,
                    fields="core,observations",
                )
                observations = list(trace.observations or ())
                if len(observations) >= minimum_observations:
                    names = sorted({item.name or item.type for item in observations})
                    return CheckResult.passed(
                        "langfuse.trace",
                        f"Langfuse returned {len(observations)} probe observations for trace {trace_id}",
                        detail=", ".join(names),
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
            except Exception as exc:  # noqa: BLE001 - ingestion can be eventually consistent.
                last_error = exc
            await asyncio.sleep(1)
    finally:
        await stop_heartbeat(heartbeat)

    detail = f"trace_id={trace_id}; expected at least {minimum_observations} observations"
    if last_error is not None:
        detail += f"; last query error: {type(last_error).__name__}: {last_error}"
    return CheckResult.failed(
        "langfuse.trace",
        "The live trace was not observable in Langfuse before the timeout",
        detail=detail,
    )


__all__ = ["check_langfuse", "check_trace_ingestion", "tracing_requested"]
