"""Native Pydantic AI OpenTelemetry instrumentation for Langfuse."""

from __future__ import annotations

from pydantic_ai import Agent

from ...utils.telemetry import configure_tracing

_INSTRUMENTED = False


def instrument_tracing() -> None:
    """Enable Pydantic AI's native spans once a Langfuse client is active."""

    global _INSTRUMENTED
    if _INSTRUMENTED or configure_tracing() is None:
        return
    Agent.instrument_all()
    _INSTRUMENTED = True


__all__ = ["instrument_tracing"]
