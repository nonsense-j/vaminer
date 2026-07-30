"""Tests for typed mining-result persistence."""

from __future__ import annotations

from pathlib import Path

from git import Actor, Repo

from src.miner.models import IssueCollectionInfo
from src.miner.utils.cache import AgentCache, load_collection_cache


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
