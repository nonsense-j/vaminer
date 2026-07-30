"""Tests for Pydantic runtime logging and trace propagation."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel
from rich.console import Console

from src.miner.utils.log import run_log_file
from src.miner.runtimes.pydantic.context import MinerContext
from src.miner.runtimes.pydantic.hooks import make_cli_hooks


async def test_agent_hooks_write_second_precision_redacted_run_logs(
    tmp_path: Path,
):
    output = StringIO()
    hooks = make_cli_hooks(
        console=Console(
            file=output,
            width=120,
            color_system=None,
            force_terminal=False,
        )
    )
    agent = Agent(
        TestModel(),
        name="Probe Agent",
        deps_type=MinerContext,
        capabilities=[hooks],
    )

    @agent.tool_plain
    def ping(api_key: str = "sk-super-secret-value") -> str:
        return "pong"

    with run_log_file(
        tmp_path / "logs",
        "VAS-TEST",
        input_id="CVE-TEST",
        trace_id="trace-1",
        runtime="pydantic-ai",
    ) as first_log:
        result = await agent.run(
            "test prompt",
            deps=MinerContext(workspace_root=tmp_path),
        )
    with run_log_file(
        tmp_path / "logs",
        "VAS-TEST",
        input_id="CVE-TEST",
        trace_id="trace-1",
        runtime="pydantic-ai",
    ) as second_log:
        pass

    assert "pong" in result.output
    assert first_log == (
        tmp_path
        / "logs"
        / "VAS-TEST"
        / "CVE-TEST"
        / "trace-1__pydantic-ai.log"
    )
    assert second_log.name == "trace-1__pydantic-ai-1.log"
    rendered = output.getvalue() + first_log.read_text(encoding="utf-8")
    assert "Probe Agent Started" in rendered
    assert "Probe Agent Finished" in rendered
    assert "<redacted>" in rendered
    assert "sk-super-secret-value" not in rendered


async def test_nested_agents_share_the_active_trace():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = InstrumentationSettings(tracer_provider=provider)
    child = Agent(
        TestModel(call_tools=[]),
        name="Child",
        capabilities=[Instrumentation(settings)],
    )
    parent = Agent(
        TestModel(call_tools=["delegate"]),
        name="Parent",
        capabilities=[Instrumentation(settings)],
    )

    @parent.tool_plain
    async def delegate() -> str:
        return (await child.run("child work")).output

    tracer = provider.get_tracer("test.pipeline")
    with tracer.start_as_current_span("Miner Workflow"):
        await parent.run("parent work")

    spans = exporter.get_finished_spans()
    assert len({span.context.trace_id for span in spans}) == 1
    assert {
        "Miner Workflow",
        "invoke_agent Parent",
        "invoke_agent Child",
    } <= {span.name for span in spans}
