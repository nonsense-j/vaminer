"""Repository operations shared by SDK tools and external runtimes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from git import GitCommandError, Repo
from pydantic_ai import RunContext

from ..configs import GITHUB_MIRROR_ENABLED
from ..core.context import MinerContext
from ..utils.models import RepoCheckout

GITHUB_URL = "https://github.com/"
GHFAST_GITHUB_URL = "https://ghfast.top/https://github.com/"
SAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_REPO_READ_BYTES = 512 * 1024
MAX_REPO_READ_LINES = 200
MAX_REPO_SEARCH_RESULTS = 100
MAX_REPO_SEARCH_BYTES = 512 * 1024


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


def read_fixed_diff_from_repo(
    repo_path: Path,
    path: str | None = None,
    *,
    timeout_seconds: float = 30,
) -> str:
    """Read the ``buggy`` to ``fixed`` diff from one verified checkout."""
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not an existing directory: {repo_path}")
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
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git diff exited with {completed.returncode}"
        raise RuntimeError(detail)
    return completed.stdout or f"No differences found between buggy and fixed{' for ' + path if path else '' }."


def _repository_file_path(repo_path: Path, path: str) -> Path:
    """Resolve one repository-relative regular file without escaping checkout."""
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_path is not an existing directory: {root}")
    if not path or Path(path).is_absolute():
        raise ValueError("repository file path must be relative")
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"repository file must stay inside the checkout: {path}") from exc
    original = root / path
    if original.is_symlink():
        raise ValueError(f"repository file must not be a symbolic link: {path}")
    if not target.is_file():
        raise ValueError(f"repository file does not exist: {path}")
    return target


def read_repository_file(
    repo_path: Path,
    path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = MAX_REPO_READ_LINES,
) -> dict[str, object]:
    """Read a bounded line range from a repository-relative source file."""
    target = _repository_file_path(repo_path, path)
    if start_line < 1 or max_lines < 1:
        raise ValueError("start_line and max_lines must be positive")
    if target.stat().st_size > MAX_REPO_READ_BYTES:
        raise ValueError(f"repository file exceeds the {MAX_REPO_READ_BYTES}-byte read limit: {path}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if start_line > len(lines):
        raise ValueError(f"start_line {start_line} exceeds {path} length ({len(lines)} lines)")
    resolved_end = min(len(lines), end_line if end_line is not None else start_line + max_lines - 1)
    if resolved_end < start_line or resolved_end - start_line + 1 > max_lines:
        raise ValueError(f"requested line range exceeds the {max_lines}-line read limit")
    return {
        "path": Path(path).as_posix(),
        "content": "".join(lines[start_line - 1 : resolved_end]),
        "start_line": start_line,
        "end_line": resolved_end,
        "total_lines": len(lines),
        "truncated": resolved_end < len(lines),
    }


def search_repository_files(
    repo_path: Path,
    pattern: str,
    *,
    path: str | None = None,
    max_results: int = MAX_REPO_SEARCH_RESULTS,
) -> dict[str, object]:
    """Run a bounded regex search over repository source files."""
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_path is not an existing directory: {root}")
    pattern = pattern.strip()
    if not pattern or len(pattern) > 500:
        raise ValueError("search pattern must be between 1 and 500 characters")
    if max_results < 1 or max_results > MAX_REPO_SEARCH_RESULTS:
        raise ValueError(f"max_results must be between 1 and {MAX_REPO_SEARCH_RESULTS}")
    target = root
    if path:
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"search path must stay inside the checkout: {path}") from exc
        if not candidate.is_dir():
            raise ValueError(f"search path is not a directory: {path}")
        target = candidate

    command = [
        "rg",
        "--json",
        "--hidden",
        "--glob",
        "!.git/**",
        "--color",
        "never",
        pattern,
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("repository search requires rg") from exc
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_REPO_SEARCH_BYTES:
        raise ValueError(f"repository search output exceeds the {MAX_REPO_SEARCH_BYTES}-byte limit")
    if completed.returncode not in (0, 1):
        raise RuntimeError(completed.stderr.strip() or f"rg exited with {completed.returncode}")

    matches: list[dict[str, object]] = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path_data = data.get("path")
        line_number = data.get("line_number")
        lines_data = data.get("lines")
        if not isinstance(path_data, dict) or not isinstance(line_number, int) or not isinstance(lines_data, dict):
            continue
        absolute = path_data.get("text")
        text = lines_data.get("text")
        if not isinstance(absolute, str) or not isinstance(text, str):
            continue
        try:
            relative = Path(absolute).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        matches.append({"file": relative, "line": line_number, "text": text.rstrip("\n")})
        if len(matches) >= max_results:
            break
    return {
        "pattern": pattern,
        "path": Path(path).as_posix() if path else ".",
        "matches": matches,
        "truncated": len(matches) >= max_results,
    }


def clone_repo(
    context: RunContext[MinerContext],
    repo_url: str,
    buggy_sha: str,
    fixed_sha: str | None = None,
) -> RepoCheckout:
    """Clone the selected revisions into the active task workspace.

    Args:
        repo_url: Repository URL (e.g., https://github.com/owner/repo).
        buggy_sha: SHA of the buggy commit.
        fixed_sha: SHA of the fixed commit, when available.
    """
    return clone_repository(
        context.deps.workspace_root,
        repo_url,
        buggy_sha,
        fixed_sha,
    )


def read_fixed_diff(
    context: RunContext[MinerContext],
    path: str | None = None,
) -> str:
    """Read the buggy-to-fixed diff, optionally limited to one repository-relative path.

    Args:
        path: Optional repository-relative path used to limit the diff.
    """
    if context.deps.repo_path is None:
        raise RuntimeError("repo_path is missing from agent dependencies")
    return read_fixed_diff_from_repo(context.deps.repo_path, path)


__all__ = [
    "MAX_REPO_READ_BYTES",
    "MAX_REPO_READ_LINES",
    "MAX_REPO_SEARCH_BYTES",
    "MAX_REPO_SEARCH_RESULTS",
    "clone_repo",
    "clone_repository",
    "read_fixed_diff",
    "read_fixed_diff_from_repo",
    "read_repository_file",
    "search_repository_files",
]
