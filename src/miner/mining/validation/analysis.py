"""Deterministic Issue Collection and Root Cause Analysis acceptance."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from git import Repo
from git.exc import GitError

from ...models.analysis import AstGrepLanguage, RootCauseAnalysis
from ...models.issue import IssueCollectionInfo

_CASE_RE = re.compile(r"^case(?P<case>\d+)(?:_var(?P<variant>\d+))?(?P<suffix>\.[A-Za-z0-9]+)$")
_LANGUAGE_SUFFIXES: dict[AstGrepLanguage, set[str]] = {
    AstGrepLanguage.C: {".c"},
    AstGrepLanguage.CPP: {".cc", ".cpp", ".cxx"},
    AstGrepLanguage.CSHARP: {".cs"},
    AstGrepLanguage.GO: {".go"},
    AstGrepLanguage.JAVA: {".java"},
    AstGrepLanguage.JAVASCRIPT: {".js", ".mjs", ".cjs"},
    AstGrepLanguage.JSX: {".jsx"},
    AstGrepLanguage.KOTLIN: {".kt", ".kts"},
    AstGrepLanguage.PHP: {".php"},
    AstGrepLanguage.PYTHON: {".py"},
    AstGrepLanguage.RUBY: {".rb"},
    AstGrepLanguage.RUST: {".rs"},
    AstGrepLanguage.SCALA: {".scala"},
    AstGrepLanguage.SWIFT: {".swift"},
    AstGrepLanguage.TSX: {".tsx"},
    AstGrepLanguage.TYPESCRIPT: {".ts"},
}


def root_cause_case_files(root_cause: RootCauseAnalysis) -> list[str]:
    return root_cause.extracted_case_files


def root_cause_source_spans(root_cause: RootCauseAnalysis) -> list[Any]:
    return list(root_cause.buggy_components)


def relative_case_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./").removeprefix("cases/")


def validate_issue_checkout(
    issue: IssueCollectionInfo,
    *,
    workspace_root: Path,
) -> list[str]:
    """Return concrete checkout errors without raising."""

    errors: list[str] = []
    repo_path = Path(issue.repo_path).resolve()
    workspace_root = workspace_root.resolve()
    try:
        repo_path.relative_to(workspace_root)
    except ValueError:
        errors.append(f"repo_path must stay inside {workspace_root}: {repo_path}")
        return errors
    if not repo_path.is_dir():
        return [f"repo_path does not exist: {repo_path}"]

    try:
        repo = Repo(repo_path)
        if repo.head.commit.hexsha != issue.buggy_commit:
            errors.append(f"buggy checkout mismatch: HEAD is {repo.head.commit.hexsha}, expected {issue.buggy_commit}")
        if issue.fixed_commit:
            try:
                fixed_sha = repo.commit("fixed").hexsha
            except GitError:
                errors.append("fixed_commit is declared but the fixed branch is missing")
            else:
                if fixed_sha != issue.fixed_commit:
                    errors.append(f"fixed branch is {fixed_sha}, expected {issue.fixed_commit}")
    except (GitError, OSError) as exc:
        errors.append(f"repo_path is not a readable git checkout: {exc}")
    return errors


def validate_root_cause_cases(
    analysis: RootCauseAnalysis,
    *,
    cases_dir: Path,
) -> list[str]:
    """Check that the RCA manifest exactly describes persistent case files."""

    errors: list[str] = []
    if not cases_dir.is_dir():
        return [f"cases directory does not exist: {cases_dir}"]

    declared = [relative_case_path(path) for path in root_cause_case_files(analysis)]
    expected = sorted(set(declared))
    actual_paths = sorted(
        (
            path
            for path in cases_dir.rglob("*")
            if path.is_file() or path.is_symlink()
        ),
        key=lambda path: path.as_posix(),
    )
    for path in actual_paths:
        relative = path.relative_to(cases_dir).as_posix()
        if relative not in expected:
            path.unlink(missing_ok=True)
    for path in sorted(
        (path for path in cases_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if path != cases_dir:
            shutil.rmtree(path, ignore_errors=True)

    actual = sorted(
        path.relative_to(cases_dir).as_posix()
        for path in cases_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    )
    if not actual:
        errors.append("cases directory is empty")
    if len(declared) != len(expected):
        errors.append("extracted_case_files contains duplicate paths")
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        if missing:
            errors.append(f"declared case files are missing: {', '.join(missing)}")
        if unexpected:
            errors.append(f"undeclared case files exist: {', '.join(unexpected)}")

    allowed_suffixes = _LANGUAGE_SUFFIXES[analysis.language]
    originals: set[tuple[str, str]] = set()
    variants: list[tuple[str, str, str]] = []
    for relative in expected:
        if "/" in relative:
            errors.append(f"case files must be directly under cases/: {relative}")
            continue
        match = _CASE_RE.fullmatch(relative)
        if not match:
            errors.append(f"invalid case filename: {relative}")
            continue
        suffix = match.group("suffix").lower()
        if suffix not in allowed_suffixes:
            errors.append(f"case extension {suffix} does not match language {analysis.language.value}: {relative}")
        case_no = match.group("case")
        variant_no = match.group("variant")
        if variant_no is None:
            originals.add((case_no, suffix))
        else:
            variants.append((case_no, suffix, relative))
        path = cases_dir / relative
        if path.is_symlink():
            errors.append(f"case file must not be a symbolic link: {relative}")
        elif path.is_file() and not path.read_text(encoding="utf-8", errors="replace").strip():
            errors.append(f"case file is empty: {relative}")

    for case_no, suffix, relative in variants:
        if (case_no, suffix) not in originals:
            errors.append(f"variant has no matching original case: {relative}")
    return errors


def validate_root_cause_analysis(
    analysis: RootCauseAnalysis,
    *,
    source_root: Path,
    cases_dir: Path,
) -> list[str]:
    """Validate persistent cases and concrete source evidence as one RCA contract."""

    errors = validate_root_cause_cases(analysis, cases_dir=cases_dir)
    root = source_root.resolve()
    if not root.is_dir():
        return [*errors, f"source root does not exist: {root}"]

    seen_components: set[tuple[str, int, int]] = set()
    for component in analysis.buggy_components:
        relative = Path(component.file)
        if relative.is_absolute():
            errors.append(f"buggy component path must be source-root-relative: {component.file}")
            continue
        source_path = (root / relative).resolve()
        try:
            canonical = source_path.relative_to(root).as_posix()
        except ValueError:
            errors.append(f"buggy component escapes the source root: {component.file}")
            continue
        location = (canonical, component.start_line, component.end_line)
        if location in seen_components:
            errors.append(
                f"duplicate buggy component location: {canonical}:{component.start_line}-{component.end_line}"
            )
        seen_components.add(location)
        if not source_path.is_file():
            errors.append(f"buggy component file does not exist: {canonical}")
            continue
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if component.end_line > len(lines):
            errors.append(
                f"buggy component line range exceeds {canonical} ({len(lines)} lines): "
                f"{component.start_line}-{component.end_line}"
            )
    return errors
