"""Tests for deferred Pydantic AI capabilities."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile

from src.miner.mining.tasks import make_issue_collection_task
from src.miner.runtimes.pydantic.context import MinerContext
from src.miner.runtimes.pydantic.runtime import PydanticAIRuntime


def _model(model_function) -> FunctionModel:
    return FunctionModel(
        model_function,
        profile=ModelProfile(
            supports_json_schema_output=True,
            supports_json_object_output=True,
            supported_native_tools=frozenset(),
        ),
    )


async def test_commit_history_tools_are_hidden_until_loaded(tmp_path: Path):
    requests = 0

    class ProbeComplete(RuntimeError):
        pass

    async def model_function(_messages, info):
        nonlocal requests
        requests += 1
        tool_names = {tool.name for tool in info.function_tools}
        if requests == 1:
            assert "search_commit_by_tag" not in tool_names
            assert "search_commit_by_time" not in tool_names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "load_capability",
                        {"id": "commit-history-search"},
                    )
                ]
            )
        assert {"search_commit_by_tag", "search_commit_by_time"} <= tool_names
        raise ProbeComplete

    model = _model(model_function)
    task = make_issue_collection_task(
        "CVE-2099-0001",
        workspace_root=tmp_path,
    )
    agent = PydanticAIRuntime(model=model).build_agent(task, model=model)

    with pytest.raises(ProbeComplete):
        await agent.run(
            "Use commit history only because direct evidence failed.",
            deps=MinerContext(workspace_root=tmp_path),
        )


async def test_web_tools_are_deferred_independently(tmp_path: Path):
    requests = 0

    class ProbeComplete(RuntimeError):
        pass

    async def model_function(_messages, info):
        nonlocal requests
        requests += 1
        tool_names = {tool.name for tool in info.function_tools}
        if requests == 1:
            assert "duckduckgo_search" not in tool_names
            assert "web_fetch" not in tool_names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "load_capability",
                        {"id": "web-search"},
                    )
                ]
            )
        if requests == 2:
            assert "duckduckgo_search" in tool_names
            assert "web_fetch" not in tool_names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "load_capability",
                        {"id": "web-fetch"},
                    )
                ]
            )
        assert {"duckduckgo_search", "web_fetch"} <= tool_names
        raise ProbeComplete

    model = _model(model_function)
    task = make_issue_collection_task(
        "CVE-2099-0001",
        workspace_root=tmp_path,
    )
    agent = PydanticAIRuntime(model=model).build_agent(task, model=model)

    with pytest.raises(ProbeComplete):
        await agent.run(
            "Research a concrete evidence gap using optional web capabilities.",
            deps=MinerContext(workspace_root=tmp_path),
        )
