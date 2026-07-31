"""Optional pipeline tracing independent of runtime selection."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langfuse import Langfuse, get_client
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace

_TRACING_CONFIGURED = False
_TRACING_ATTEMPTED = False
_TRACING_CLIENT: Langfuse | None = None
_TRACE_CARRIER_ENV = {
    "traceparent": "VAMINER_OTEL_TRACEPARENT",
    "tracestate": "VAMINER_OTEL_TRACESTATE",
    "baggage": "VAMINER_OTEL_BAGGAGE",
}


@dataclass(frozen=True)
class PipelineTrace:
    """One workflow trace identity, with an optional Langfuse observation."""

    trace_id: str
    observation: Any | None = None

    def update(self, **values: Any) -> None:
        if self.observation is not None:
            self.observation.update(**values)


def configure_tracing() -> Langfuse | None:
    """Configure the runtime-neutral Langfuse client when credentials work."""

    global _TRACING_ATTEMPTED, _TRACING_CLIENT, _TRACING_CONFIGURED
    if _TRACING_CONFIGURED:
        return _TRACING_CLIENT
    if _TRACING_ATTEMPTED:
        return None
    _TRACING_ATTEMPTED = True
    try:
        langfuse = get_client()
        if not langfuse.auth_check():
            return None
        _TRACING_CLIENT = langfuse
        _TRACING_CONFIGURED = True
        return langfuse
    except Exception:  # noqa: BLE001 - optional tracing must not block mining.
        return None


def propagated_trace_environment() -> dict[str, str]:
    """Serialize the active W3C trace context for an owned subprocess."""

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {
        env_name: value
        for key, env_name in _TRACE_CARRIER_ENV.items()
        if (value := carrier.get(key))
    }


@contextmanager
def use_propagated_trace_environment(
    environment: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """Attach a subprocess-propagated W3C context for the current process."""

    values = os.environ if environment is None else environment
    carrier = {
        key: value
        for key, env_name in _TRACE_CARRIER_ENV.items()
        if (value := values.get(env_name))
    }
    if "traceparent" not in carrier:
        yield
        return
    token = otel_context.attach(propagate.extract(carrier))
    try:
        yield
    finally:
        otel_context.detach(token)


@contextmanager
def trace_tool_observation(
    *,
    name: str,
    input: Any,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Create one current TOOL observation under an active owned trace."""

    if not trace.get_current_span().get_span_context().is_valid:
        yield None
        return
    langfuse = configure_tracing()
    if langfuse is None:
        yield None
        return
    try:
        manager = langfuse.start_as_current_observation(
            name=name,
            as_type="tool",
            input=input,
            metadata=dict(metadata or {}),
        )
        observation = manager.__enter__()
    except Exception:  # noqa: BLE001 - optional tracing must not block work.
        yield None
        return

    try:
        yield observation
    except BaseException as error:
        try:
            manager.__exit__(type(error), error, error.__traceback__)
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def trace_pipeline(
    *,
    issue_input: str,
    vas_id: str,
    runtime_ids: Sequence[str],
) -> Iterator[PipelineTrace]:
    """Create one active observation for the complete mining workflow."""

    langfuse = configure_tracing()
    if langfuse is None:
        yield PipelineTrace(trace_id=uuid.uuid4().hex)
        return

    routed_runtimes = tuple(dict.fromkeys(runtime_ids))
    runtime_label = "+".join(routed_runtimes) or "unknown-runtime"
    with langfuse.start_as_current_observation(
        name=f"{vas_id} Miner Workflow @{runtime_label}",
        as_type="chain",
        input={"issue_input": issue_input},
        metadata={"vas_id": vas_id, "runtimes": routed_runtimes},
    ) as pipeline_span:
        yield PipelineTrace(
            trace_id=pipeline_span.trace_id,
            observation=pipeline_span,
        )


def flush_tracing() -> None:
    """Flush pending telemetry for the short-lived CLI process."""

    if _TRACING_CLIENT is None:
        return
    try:
        _TRACING_CLIENT.flush()
    except Exception:  # noqa: BLE001 - optional tracing must not block shutdown.
        return
