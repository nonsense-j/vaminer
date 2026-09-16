"""Pydantic AI bindings for runtime-neutral Miner tools."""

from __future__ import annotations

from pydantic_ai import RunContext

from ...models.issue import RepoCheckout
from ...models.tool import BuggyCommitSha, FixedCommitSha, RepositoryUrl
from ...tools.repo import clone_repository
from .context import MinerContext


def clone_repo(
    context: RunContext[MinerContext],
    repo_url: RepositoryUrl,
    buggy_sha: BuggyCommitSha,
    fixed_sha: FixedCommitSha = None,
) -> RepoCheckout:
    """Clone selected repository revisions into the task workspace."""

    return clone_repository(
        context.deps.workspace_root,
        repo_url,
        buggy_sha,
        fixed_sha,
    )
