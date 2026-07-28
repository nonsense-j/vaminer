"""End-to-end tests for reusable cache, logging, and tracing behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
import os
from pathlib import Path

from git import Actor, Repo
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel
from rich.console import Console

from src.miner import configs
from src.miner.core.context import MinerContext
from src.miner.utils.cache import AgentCache, load_collection_cache
from src.miner.utils.hooks import make_cli_hooks
from src.miner.utils.logger import run_log_file
from src.miner.utils.models import IssueCollectionInfo


def test_standard_proxy_configures_local_web_search(monkeypatch):
    monkeypatch.delenv("DDGS_PROXY", raising=False)

    configs._configure_ddgs_proxy(
        http_proxy="http://127.0.0.1:8080",
        https_proxy="http://127.0.0.1:8443",
    )

    assert os.environ["DDGS_PROXY"] == "http://127.0.0.1:8443"

    monkeypatch.delenv("DDGS_PROXY")
    configs._configure_ddgs_proxy(
        http_proxy="http://127.0.0.1:8080",
        https_proxy=None,
    )

    assert os.environ["DDGS_PROXY"] == "http://127.0.0.1:8080"

    monkeypatch.setenv("DDGS_PROXY", "socks5://127.0.0.1:1080")
    configs._configure_ddgs_proxy(
        http_proxy="http://127.0.0.1:8080",
        https_proxy=None,
    )

    assert os.environ["DDGS_PROXY"] == "socks5://127.0.0.1:1080"


def test_typed_cache_round_trips_through_checkout_validation(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "file.c").write_text("int value;\n", encoding="utf-8")
    repo = Repo.init(repo_path)
    actor = Actor("VAS Test", "vas-test@example.com")
    repo.index.add(["file.c"])
    commit = repo.index.commit("buggy", author=actor, committer=actor)

    cache = AgentCache("Issue Collector", tmp_path / "cache")
    expected = IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="Summary",
        issue_details="Details",
        repo_url="https://github.com/example/project",
        repo_path=repo_path.as_posix(),
        buggy_commit=commit.hexsha,
        fixed_commit=None,
    )
    cache.set(expected)

    assert load_collection_cache(cache, workspace_root=tmp_path) == expected


async def test_agent_hooks_write_second_precision_redacted_run_logs(tmp_path: Path):
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

    timestamp = datetime(2026, 7, 28, 17, 2, 13, tzinfo=UTC)
    with run_log_file(tmp_path / "logs", "VAS-TEST", timestamp=timestamp) as first_log:
        result = await agent.run("test prompt", deps=MinerContext(workspace_root=tmp_path))
    with run_log_file(tmp_path / "logs", "VAS-TEST", timestamp=timestamp) as second_log:
        pass

    assert "pong" in result.output
    assert first_log.name == "miner-20260728-170213.log"
    assert second_log.name == "miner-20260728-170213-1.log"
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
    assert {"Miner Workflow", "invoke_agent Parent", "invoke_agent Child"} <= {span.name for span in spans}
