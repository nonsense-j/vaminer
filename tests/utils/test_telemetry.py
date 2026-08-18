from contextlib import contextmanager

from src.miner.utils import telemetry
from src.miner.utils.telemetry import bounded_trace_value, trace_value


def test_pytest_disables_live_langfuse_tracing(monkeypatch):
    monkeypatch.setattr(telemetry, "_TRACING_ATTEMPTED", False)
    monkeypatch.setattr(telemetry, "_TRACING_CLIENT", None)
    monkeypatch.setattr(telemetry, "_TRACING_CONFIGURED", False)

    def unexpected_client():
        raise AssertionError("Langfuse client must not be created during tests")

    monkeypatch.setattr(telemetry, "get_client", unexpected_client)

    assert telemetry.configure_tracing() is None


def test_trace_payloads_are_redacted_and_bounded():
    safe = bounded_trace_value(
        {
            "api_key": "top-secret-key",
            "message": "Authorization: Bearer top-secret-token",
        }
    )
    rendered = str(safe)
    assert "top-secret-key" not in rendered
    assert "top-secret-token" not in rendered
    assert "<redacted>" in rendered

    bounded = bounded_trace_value({"content": "x" * 20_000})
    assert isinstance(bounded, str)
    assert len(bounded) <= 16_020
    assert bounded.endswith("[truncated]")


def test_trace_value_preserves_complete_payloads():
    content = "x" * 20_000

    full = trace_value({"content": content})

    assert full == {"content": content}


def test_pipeline_uses_original_input_and_binds_exact_trace_name(monkeypatch):
    class Observation:
        trace_id = "0123456789abcdef0123456789abcdef"

        def __init__(self):
            self.updates = []

        def update(self, **values):
            self.updates.append(values)

    class Client:
        def __init__(self):
            self.started = None
            self.observation = Observation()

        @contextmanager
        def start_as_current_observation(self, **values):
            self.started = values
            yield self.observation

    propagated = []

    @contextmanager
    def record_attributes(**values):
        propagated.append(values)
        yield

    client = Client()
    monkeypatch.setattr(telemetry, "configure_tracing", lambda: client)
    monkeypatch.setattr(telemetry, "propagate_attributes", record_attributes)

    mining_input = {"reference": "CVE-2099-0001"}
    with telemetry.trace_pipeline(mining_input=mining_input, runtime_id="pydanic-sdk") as pipeline:
        with pipeline.bind("VAS-0001"):
            pipeline.update(output={"vas_id": "VAS-0001"})

    assert client.started["name"] == "VAMiner @pydanic-sdk"
    assert client.started["input"] == mining_input
    assert client.observation.updates[0] == {
        "name": "VAS-0001 Miner @pydanic-sdk",
        "metadata": {"vas_id": "VAS-0001", "runtime": "pydanic-sdk"},
    }
    assert client.observation.updates[1] == {"output": {"vas_id": "VAS-0001"}}
    assert propagated == [
        {
            "trace_name": "VAS-0001 Miner @pydanic-sdk",
            "metadata": {"vas_id": "VAS-0001", "runtime": "pydanic-sdk"},
            "tags": ["vaminer", "pydanic-sdk"],
        }
    ]


def test_claude_trace_environment_uses_current_w3c_carrier(monkeypatch):
    def inject(carrier):
        carrier["traceparent"] = "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01"

    monkeypatch.setattr(telemetry.propagate, "inject", inject)

    assert telemetry.claude_trace_environment() == {
        "CC_LANGFUSE_TRACEPARENT": "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01"
    }
