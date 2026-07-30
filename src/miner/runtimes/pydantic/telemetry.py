"""Optional tracing integration owned by the Pydantic AI adapter."""

from __future__ import annotations

from pydantic_ai import Agent

from ...utils.telemetry import configure_tracing

_INSTRUMENTED = False


def instrument_tracing() -> None:
    """Instrument Pydantic agents when the shared trace client is available."""

    global _INSTRUMENTED
    if _INSTRUMENTED or configure_tracing() is None:
        return
    Agent.instrument_all()
    _INSTRUMENTED = True
