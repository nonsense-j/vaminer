"""Repository operations shared by SDK tools and external runtimes."""

from __future__ import annotations

import re
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from git import GitCommandError, Repo

from ..models.issue import RepoCheckout
from ..utils.config import GITHUB_MIRROR_ENABLED

GITHUB_URL = "https://github.com/"
GHFAST_GITHUB_URL = "https://ghfast.top/https://github.com/"
SAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_REPO_ERROR_CHARS = 2_000
MAX_REPO_DIFF_BYTES = 512 * 1024
MAX_REPO_DIFF_STAT_FILES = 100


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
    with suppress(GitCommandError):
        repo.git.config("--unset-all", f"url.{GHFAST_GITHUB_URL}.insteadOf")


def clone_repository(
    workspace_root: Path,
    repo_url: str,
    buggy_sha: str,
    fixed_sha: str | None = None,
    *,
    github_mirror_enabled: bool = GITHUB_MIRROR_ENABLED,
) -> RepoCheckout:
    """Clone selected revisions below ``workspace_root`` and check out ``buggy``.

    Args:
        workspace_root: Task-owned workspace that will contain the ``src`` tree.
        repo_url: Repository URL (e.g., https://github.com/owner/repo)
        buggy_sha: SHA of the buggy commit
        fixed_sha: SHA of the fixed commit (optional)
        github_mirror_enabled: Try the configured GitHub mirror before direct GitHub.
    """
    owner, repo_name = _parse_repo_url(repo_url)
    workspace_root = Path(workspace_root).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    repo_path = workspace_root / "src" / owner / repo_name

    if repo_path.exists():
        shutil.rmtree(repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)

    use_github_mirror = _is_github_https_url(repo_url) and github_mirror_enabled
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
    return RepoCheckout(
        repo_path=repo_path.absolute().as_posix(),
        buggy_branch="buggy",
        fixed_branch=fixed_branch,
    )


def _bounded_process_error(label: str, stderr: str, returncode: int) -> RuntimeError:
    detail = stderr.strip()[:MAX_REPO_ERROR_CHARS]
    return RuntimeError(detail or f"{label} exited with {returncode}")


def read_patch_diff_from_repo(
    repo_path: Path,
    path: str | None = None,
    *,
    timeout_seconds: float = 30,
) -> str:
    """Read the ``buggy`` to ``fixed`` diff from one verified checkout.

    This tool is already rooted at the repository checkout. ``path`` may be a
    file or directory relative to that root; do not include the workspace
    checkout prefix. When omitted or resolved to the root, return a compact
    diffstat. Otherwise return the full patch for that scope. Oversized output
    is rejected with a request to narrow the path.
    """
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not an existing directory: {repo_path}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    relative = Path(path) if path else None
    if relative is not None and relative.is_absolute():
        raise ValueError("diff path must be relative")
    if relative is not None:
        candidate = (repo_path / relative).resolve()
        try:
            relative = candidate.relative_to(repo_path)
        except ValueError as exc:
            raise ValueError(f"diff path must stay inside the repository: {path}") from exc
        if relative == Path("."):
            relative = None

    command = ["git", "diff", "--no-ext-diff"]
    if relative is None:
        command.extend(("--stat", f"--stat-count={MAX_REPO_DIFF_STAT_FILES}"))
    command.extend(("buggy", "fixed", "--"))
    if relative is not None:
        command.append(relative.as_posix())

    try:
        completed = subprocess.run(
            command,
            cwd=repo_path,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("repository diff requires git on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"repository diff timed out after {timeout_seconds:g} seconds") from exc
    if completed.returncode != 0:
        raise _bounded_process_error("git diff", completed.stderr, completed.returncode)
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_REPO_DIFF_BYTES:
        raise ValueError(
            f"repository diff output exceeds the {MAX_REPO_DIFF_BYTES}-byte limit; use a narrower path"
        )
    scope = f" for {relative.as_posix()}" if relative is not None else ""
    return completed.stdout or f"No differences found between buggy and fixed{scope}."


__all__ = [
    "MAX_REPO_DIFF_BYTES",
    "MAX_REPO_DIFF_STAT_FILES",
    "clone_repository",
    "read_patch_diff_from_repo",
]
