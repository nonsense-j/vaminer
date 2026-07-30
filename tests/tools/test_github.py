"""Tests for GitHub commit-search operations."""

from __future__ import annotations

import pytest

from src.miner.tools import github


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


def _commit_payload(sha: str, timestamp: str) -> dict:
    return {
        "sha": sha,
        "parents": [{"sha": f"{sha}-parent"}],
        "commit": {
            "committer": {"date": timestamp},
            "message": f"commit {sha}",
        },
    }


def test_commit_search_follows_prefix_and_time_contracts(
    monkeypatch: pytest.MonkeyPatch,
):
    matching_url = (
        "https://api.github.com/repos/curl/curl/git/matching-refs/tags/curl-7_64"
    )
    older_url = "https://api.github.com/repos/curl/curl/commits/curl-7_64_0"
    newer_url = "https://api.github.com/repos/curl/curl/commits/curl-7_64_1"
    tag_client = FakeClient(
        {
            matching_url: [
                {
                    "ref": "refs/tags/curl-7_64_1",
                    "object": {"type": "tag", "sha": "tag-object"},
                },
                {
                    "ref": "refs/tags/curl-7_64_0",
                    "object": {"type": "commit", "sha": "lightweight"},
                },
            ],
            older_url: _commit_payload("older", "2019-02-01T12:00:00Z"),
            newer_url: _commit_payload("newer", "2019-03-01T12:00:00Z"),
        }
    )
    monkeypatch.setattr(github.httpx, "Client", lambda *, timeout: tag_client)

    assert [
        commit.cur_sha
        for commit in github.search_commit_by_tag(
            "curl",
            "curl",
            "curl-7_64",
        )
    ] == ["older", "newer"]
    assert [url for url, _ in tag_client.calls] == [
        matching_url,
        newer_url,
        older_url,
    ]
    assert "must be non-empty" in github.search_commit_by_tag(
        "curl",
        "curl",
        " ",
    )

    commits_url = "https://api.github.com/repos/curl/curl/commits"
    time_clients = iter(
        [
            FakeClient(
                {
                    commits_url: [
                        _commit_payload("newer", "2019-03-01T12:00:00Z"),
                        _commit_payload("older", "2019-02-01T12:00:00Z"),
                    ]
                }
            ),
            FakeClient(
                {
                    commits_url: [
                        _commit_payload(
                            f"sha-{index}",
                            "2019-02-01T12:00:00Z",
                        )
                        for index in range(100)
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(
        github.httpx,
        "Client",
        lambda *, timeout: next(time_clients),
    )

    commits = github.search_commit_by_time(
        "curl",
        "curl",
        "2019-02-01T00:00:00Z",
        "2019-03-02T00:00:00Z",
    )
    assert [commit.cur_sha for commit in commits] == ["older", "newer"]
    assert "narrow the time range" in github.search_commit_by_time(
        "curl",
        "curl",
        "2019-01-01T00:00:00Z",
        "2019-04-01T00:00:00Z",
    )
