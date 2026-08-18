"""Pydantic AI bindings for runtime-neutral Miner tools."""

from __future__ import annotations

from pydantic_ai import RunContext

from ...models.issue import RepoCheckout
from ...tools.repo import clone_repository
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
