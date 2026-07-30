"""Tests for runtime-neutral repository operations."""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from src.miner.tools.repo import (
    clone_repository,
    read_patch_diff_from_repo,
    read_repository_file,
    search_repository_files,
)
from tests.support.repos import prepare_history


def test_clone_and_diff_preserve_requested_history(tmp_path: Path):
    upstream = tmp_path / "upstream"
    buggy_sha, fixed_sha = prepare_history(upstream)
    checkout = clone_repository(
        tmp_path / "workspace",
        upstream.as_posix(),
        buggy_sha,
        fixed_sha,
        github_mirror_enabled=False,
    )
    checkout_path = Path(checkout.repo_path)
    cloned = Repo(checkout_path)

    assert cloned.head.commit.hexsha == buggy_sha
    assert cloned.commit("fixed").hexsha == fixed_sha
    assert "-int value = 1;" in read_patch_diff_from_repo(
        checkout_path,
        "bug.c",
    )
    assert "+int value = 2;" in read_patch_diff_from_repo(
        checkout_path,
        "bug.c",
    )
    with pytest.raises(ValueError, match="must stay inside"):
        read_patch_diff_from_repo(checkout_path, "../outside.c")


def test_navigation_rejects_escape_and_caps_results(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.c").write_text(
        "int guard = 1;\nvoid f(void) { guard++; }\nint other = guard;\n",
        encoding="utf-8",
    )

    assert (
        read_repository_file(
            repo,
            "src.c",
            start_line=2,
            end_line=2,
        )["content"]
        == "void f(void) { guard++; }\n"
    )
    result = search_repository_files(repo, r"guard", max_results=2)
    assert result["matches"]
    assert all(match["file"] == "src.c" for match in result["matches"])
    with pytest.raises(ValueError, match="inside"):
        read_repository_file(repo, "../src.c")
    with pytest.raises(ValueError, match="inside"):
        search_repository_files(repo, "guard", path="../")
