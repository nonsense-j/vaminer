"""Public-behavior tests for Miner research and structural-search tools."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile

from src.miner.core.agents import make_issue_collector
from src.miner.core.context import MinerContext
from src.miner.tools import github
from src.miner.tools.ast_grep import _load_runner, _resolve_target


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return self

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self):
        return None

    def get(self, url: str, *, params=None, headers=None):
        del headers
        self.calls.append((url, params))
        return FakeResponse(self.responses[url])


def commit_payload(sha: str, timestamp: str) -> dict:
    return {
        "sha": sha,
        "parents": [{"sha": f"{sha}-parent"}],
        "commit": {
            "committer": {"date": timestamp},
            "message": f"commit {sha}",
        },
    }


def test_commit_search_tools_follow_prefix_and_time_contracts(monkeypatch: pytest.MonkeyPatch):
    matching_url = "https://api.github.com/repos/curl/curl/git/matching-refs/tags/curl-7_64"
    older_url = "https://api.github.com/repos/curl/curl/commits/curl-7_64_0"
    newer_url = "https://api.github.com/repos/curl/curl/commits/curl-7_64_1"
    tag_client = FakeClient(
        {
            matching_url: [
                {"ref": "refs/tags/curl-7_64_1", "object": {"type": "tag", "sha": "tag-object"}},
                {"ref": "refs/tags/curl-7_64_0", "object": {"type": "commit", "sha": "lightweight"}},
            ],
            older_url: commit_payload("older", "2019-02-01T12:00:00Z"),
            newer_url: commit_payload("newer", "2019-03-01T12:00:00Z"),
        }
    )
    monkeypatch.setattr(github.httpx, "Client", lambda *, timeout: tag_client)

    tags = github.search_commit_by_tag("curl", "curl", "curl-7_64")

    assert [commit.cur_sha for commit in tags] == ["older", "newer"]
    assert [url for url, _ in tag_client.calls] == [matching_url, newer_url, older_url]
    assert "must be non-empty" in github.search_commit_by_tag("curl", "curl", " ")

    commits_url = "https://api.github.com/repos/curl/curl/commits"
    time_clients = iter(
        [
            FakeClient(
                {
                    commits_url: [
                        commit_payload("newer", "2019-03-01T12:00:00Z"),
                        commit_payload("older", "2019-02-01T12:00:00Z"),
                    ]
                }
            ),
            FakeClient({commits_url: [commit_payload(f"sha-{index}", "2019-02-01T12:00:00Z") for index in range(100)]}),
        ]
    )
    monkeypatch.setattr(github.httpx, "Client", lambda *, timeout: next(time_clients))

    commits = github.search_commit_by_time(
        "curl",
        "curl",
        "2019-02-01T00:00:00Z",
        "2019-03-02T00:00:00Z",
    )
    saturated = github.search_commit_by_time(
        "curl",
        "curl",
        "2019-01-01T00:00:00Z",
        "2019-04-01T00:00:00Z",
    )

    assert [commit.cur_sha for commit in commits] == ["older", "newer"]
    assert "narrow the time range" in saturated


async def test_commit_history_capability_hides_tools_until_loaded(tmp_path: Path):
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
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "commit-history-search"})])
        assert {"search_commit_by_tag", "search_commit_by_time"} <= tool_names
        raise ProbeComplete

    model = FunctionModel(
        model_function,
        profile=ModelProfile(
            supports_json_schema_output=True,
            supports_json_object_output=True,
            supported_native_tools=frozenset(),
        ),
    )
    agent = make_issue_collector(model=model)

    with pytest.raises(ProbeComplete):
        await agent.run(
            "Use commit history only because direct evidence failed.",
            deps=MinerContext(workspace_root=tmp_path),
        )


async def test_issue_collector_web_capabilities_are_deferred(tmp_path: Path):
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
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "web-search"})])
        if requests == 2:
            assert "duckduckgo_search" in tool_names
            assert "web_fetch" not in tool_names
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "web-fetch"})])
        assert {"duckduckgo_search", "web_fetch"} <= tool_names
        raise ProbeComplete

    model = FunctionModel(
        model_function,
        profile=ModelProfile(
            supports_json_schema_output=True,
            supports_json_object_output=True,
            supported_native_tools=frozenset(),
        ),
    )
    agent = make_issue_collector(model=model)

    with pytest.raises(ProbeComplete):
        await agent.run(
            "Research a concrete evidence gap using optional web capabilities.",
            deps=MinerContext(workspace_root=tmp_path),
        )


def test_ast_grep_runner_reports_normalized_directory_results(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    target = tmp_path / "arbitrary"
    target.mkdir()
    (target / "a.c").write_text("void a(void) {\n  danger(1);\n}\n", encoding="utf-8")
    (target / "b.c").write_text("void b(void) {\n  danger(2);\n}\n", encoding="utf-8")
    runner = _load_runner()

    count = runner.run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="count",
    )
    sample = runner.run_ast_grep(
        target,
        language="c",
        query_type="rule",
        query="rule:\n  pattern: danger($ARG);",
        output="sample",
        sample_size=1,
    )
    full = runner.run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="full",
    )

    assert count == {
        "target_dir": target.as_posix(),
        "output": "count",
        "match_count": 2,
        "matched_file_count": 2,
    }
    assert sample["truncated"] is True
    assert sample["matches"][0]["file"] == "a.c"
    assert sample["matches"][0]["start"] == {"line": 2, "column": 3}
    assert [site["file"] for site in full["matches"]] == ["a.c", "b.c"]
    assert all("meta_variables" in site for site in full["matches"])

    assert _resolve_target(tmp_path, "arbitrary") == target
    with pytest.raises(Exception, match="must stay inside"):
        _resolve_target(tmp_path, str(tmp_path.parent))
