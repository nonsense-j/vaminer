"""Repo manager tool — clone with buggy/fixed branches."""

import re
import shutil
import subprocess
from urllib.parse import urlparse

from git import GitCommandError, Repo
from pydantic_ai import RunContext

from ..configs import GITHUB_MIRROR_ENABLED
from ..core.context import MinerContext
from ..utils.models import RepoCheckout

GITHUB_URL = "https://github.com/"
GHFAST_GITHUB_URL = "https://ghfast.top/https://github.com/"
SAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _parse_repo_url(url: str) -> tuple[str, str]:
    if "://" not in url and ":" in url:
        host, path = url.split(":", 1)
        host = host.rsplit("@", 1)[-1]
    else:
        parsed = urlparse(url)
        host = parsed.hostname or "unknown"
        path = parsed.path

    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return _safe_path_part(host, "unknown"), "repo"

    repo_name = _safe_path_part(parts[-1].removesuffix(".git"), "repo")
    if host.lower() == "github.com" and len(parts) >= 2:
        namespace = parts[-2]
    else:
        namespace = "__".join([host, *parts[:-1]])

    return _safe_path_part(namespace, "unknown"), repo_name


def _safe_path_part(value: str, fallback: str) -> str:
    safe_value = SAFE_PATH_CHARS_RE.sub("_", value).strip("._-")
    return safe_value or fallback


def _is_github_https_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == "github.com"


def _disable_github_mirror(repo: Repo) -> None:
    try:
        repo.git.config("--unset-all", f"url.{GHFAST_GITHUB_URL}.insteadOf")
    except GitCommandError:
        pass


def clone_repo(
    context: RunContext[MinerContext],
    repo_url: str,
    buggy_sha: str,
    fixed_sha: str | None = None,
) -> RepoCheckout:
    """Clone a repository with only the buggy and fixed commits and create branches for them. Check out the buggy branch by default.

    Args:
        repo_url: Repository URL (e.g., https://github.com/owner/repo)
        buggy_sha: SHA of the buggy commit
        fixed_sha: SHA of the fixed commit (optional)
    """
    owner, repo_name = _parse_repo_url(repo_url)
    repo_path = context.deps.workspace_root / "src" / owner / repo_name

    if repo_path.exists():
        shutil.rmtree(repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)

    use_github_mirror = _is_github_https_url(repo_url) and GITHUB_MIRROR_ENABLED
    repo = Repo.init(repo_path)
    with repo.config_writer() as config:
        config.set_value("core", "symlinks", "false")
        if use_github_mirror:
            config.set_value(f'url "{GHFAST_GITHUB_URL}"', "insteadOf", GITHUB_URL)
    repo.create_remote("origin", repo_url)

    def fetch(ref: str) -> None:
        nonlocal use_github_mirror
        try:
            repo.git.fetch("origin", ref, depth=1)
        except GitCommandError:
            if not use_github_mirror:
                raise
            _disable_github_mirror(repo)
            use_github_mirror = False
            repo.git.fetch("origin", ref, depth=1)

    fetch(buggy_sha)
    repo.git.branch("buggy", "FETCH_HEAD")

    fixed_branch = None
    if fixed_sha:
        fetch(fixed_sha)
        repo.git.branch("fixed", "FETCH_HEAD")
        fixed_branch = "fixed"

    repo.git.checkout("buggy")
    repo_path_str = repo_path.absolute().as_posix()
    return RepoCheckout(repo_path=repo_path_str, buggy_branch="buggy", fixed_branch=fixed_branch)


def read_fixed_diff(
    context: RunContext[MinerContext],
    path: str | None = None,
) -> str:
    """Read the buggy-to-fixed Git diff, optionally limited to one repository-relative path."""
    if context.deps.repo_path is None:
        raise RuntimeError("repo_path is missing from agent dependencies")
    repo_path = context.deps.repo_path.resolve()
    command = ["git", "diff", "--no-ext-diff", "buggy", "fixed", "--"]
    if path:
        candidate = (repo_path / path).resolve()
        try:
            relative = candidate.relative_to(repo_path)
        except ValueError as exc:
            raise ValueError(f"diff path must stay inside the repository: {path}") from exc
        command.append(relative.as_posix())

    completed = subprocess.run(
        command,
        cwd=repo_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git diff exited with {completed.returncode}"
        raise RuntimeError(detail)
    return completed.stdout or f"No differences found between buggy and fixed{' for ' + path if path else '' }."
