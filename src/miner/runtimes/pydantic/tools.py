"""Pydantic AI bindings for runtime-neutral Miner tools."""

from __future__ import annotations

from pydantic_ai import RunContext

from ...models.issue import RepoCheckout
from ...tools.repo import clone_repository, read_patch_diff_from_repo
from .context import MinerContext


def clone_repo(
    context: RunContext[MinerContext],
    repo_url: str,
    buggy_sha: str,
    fixed_sha: str | None = None,
) -> RepoCheckout:
    """Clone selected revisions into the active task workspace."""

    return clone_repository(
        context.deps.workspace_root,
        repo_url,
        buggy_sha,
        fixed_sha,
    )


def read_patch_diff(
    context: RunContext[MinerContext],
    path: str | None = None,
) -> str:
    """Read the buggy-to-fixed diff, optionally limited to one repository path."""

    if context.deps.repo_path is None:
        raise RuntimeError("repo_path is missing from agent dependencies")
    return read_patch_diff_from_repo(context.deps.repo_path, path)
