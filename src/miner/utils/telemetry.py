"""Optional pipeline tracing independent of runtime selection."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from langfuse import Langfuse, get_client, propagate_attributes
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
_TRACE_PAYLOAD_LIMIT = 16_000
_SENSITIVE_KEY = re.compile(r"api[-_]?key|authorization|token|password|secret|cookie", re.IGNORECASE)
_SECRET = re.compile(r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+")


def _tracing_disabled() -> bool:
    return os.getenv("LANGFUSE_TRACING_ENABLED", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def trace_value(value: Any) -> Any:
    """Return complete JSON-compatible telemetry data with secrets removed."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY.search(str(key)) else trace_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [trace_value(item) for item in value]
    if isinstance(value, str):
        return _SECRET.sub(lambda match: match.group(1) + "<redacted>", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def bounded_trace_value(value: Any) -> Any:
    safe = trace_value(value)
    try:
        rendered = json.dumps(safe, ensure_ascii=False, default=str)
    except TypeError:
        rendered = str(safe)
    if len(rendered) <= _TRACE_PAYLOAD_LIMIT:
        return safe
    return rendered[:_TRACE_PAYLOAD_LIMIT].rstrip() + " ... [truncated]"


@dataclass(frozen=True)
class PipelineTrace:
    """One workflow trace identity, with an optional Langfuse observation."""

    trace_id: str
    runtime_id: str
    observation: Any | None = None

    def update(self, **values: Any) -> None:
        if self.observation is not None:
            self.observation.update(
                **{key: bounded_trace_value(value) for key, value in values.items()}
            )

    @contextmanager
    def bind(self, vas_id: str) -> Iterator[None]:
        """Apply the stable VAS identity before any traced Agent work begins."""

        if self.observation is None:
            yield
            return
        trace_name = f"{vas_id} Miner @{self.runtime_id}"
        metadata = {"vas_id": vas_id, "runtime": self.runtime_id}
        self.observation.update(name=trace_name, metadata=metadata)
        with propagate_attributes(
            trace_name=trace_name,
            metadata=metadata,
            tags=["vaminer", self.runtime_id],
        ):
            yield


def configure_tracing() -> Langfuse | None:
    """Configure the runtime-neutral Langfuse client when credentials work."""

    global _TRACING_ATTEMPTED, _TRACING_CLIENT, _TRACING_CONFIGURED
    if _tracing_disabled():
        _TRACING_ATTEMPTED = True
        return None
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


def claude_trace_environment() -> dict[str, str]:
    """Return the trace carrier understood by the official Claude Langfuse plugin."""

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    traceparent = carrier.get("traceparent")
    return {"CC_LANGFUSE_TRACEPARENT": traceparent} if traceparent else {}


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
            input=bounded_trace_value(input),
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
        except Exception:  # noqa: BLE001, S110
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001, S110
            pass


@contextmanager
def trace_agent_observation(
    *,
    name: str,
    input: Any,
    metadata: Mapping[str, Any],
    truncate: bool = True,
) -> Iterator[Any | None]:
    """Create one Agent observation below the active Workflow or synthesis Tool."""

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
            as_type="agent",
            input=bounded_trace_value(input) if truncate else trace_value(input),
            metadata=dict(metadata),
        )
        observation = manager.__enter__()
    except Exception:  # noqa: BLE001
        yield None
        return
    try:
        yield observation
    except BaseException as error:
        try:
            observation.update(status_message=str(error), level="ERROR")
            manager.__exit__(type(error), error, error.__traceback__)
        except Exception:  # noqa: BLE001, S110
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001, S110
            pass


@contextmanager
def trace_pipeline(
    *,
    mining_input: Any,
    runtime_id: str,
) -> Iterator[PipelineTrace]:
    """Create one root observation from the original input to the final VAS."""

    langfuse = configure_tracing()
    if langfuse is None:
        yield PipelineTrace(trace_id=uuid.uuid4().hex, runtime_id=runtime_id)
        return

    with langfuse.start_as_current_observation(
        name=f"VAMiner @{runtime_id}",
        as_type="chain",
        input=bounded_trace_value(mining_input),
        metadata={"runtime": runtime_id},
    ) as pipeline_span:
        yield PipelineTrace(
            trace_id=pipeline_span.trace_id,
            runtime_id=runtime_id,
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
