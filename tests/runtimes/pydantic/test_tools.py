"""Tests for thin Pydantic AI tool adapters."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from git import Repo

from src.miner.runtimes.pydantic.context import MinerContext
from src.miner.runtimes.pydantic.tools import clone_repo, read_patch_diff
from src.miner.tools.repo import read_patch_diff_from_repo
from tests.support.repos import prepare_history


def test_repo_adapters_preserve_neutral_operation_results(tmp_path: Path):
    upstream = tmp_path / "upstream"
    buggy_sha, fixed_sha = prepare_history(upstream)
    context = SimpleNamespace(deps=MinerContext(workspace_root=tmp_path / "workspace"))

    checkout = clone_repo(
        context,
        upstream.as_posix(),
        buggy_sha,
        fixed_sha,
    )
    checkout_path = Path(checkout.repo_path)
    assert Repo(checkout_path).head.commit.hexsha == buggy_sha

    diff_context = SimpleNamespace(
        deps=MinerContext(
            workspace_root=tmp_path / "workspace",
            repo_path=checkout_path,
        )
    )
    assert read_patch_diff(diff_context) == read_patch_diff_from_repo(checkout_path)
