"""Bounded read-only access to task-declared skill resources."""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..models.anchors import AstGrepExperience, AstGrepExperienceOutcome
from .text import format_file_read, truncation_footer

MAX_SKILL_RESOURCE_FILES = 100
MAX_SKILL_RESOURCE_BYTES = 256 * 1024
MAX_SKILL_RESOURCE_LINES = 200
MAX_AST_GREP_EXPERIENCES = 24
AST_GREP_EXPERIENCES_RESOURCE = "references/experiences.md"
_EXPERIENCE_LINE = re.compile(r"^- \*\*(success|pitfall)\*\*: (.+)$")
_EXPERIENCE_HEADER = """# AST-Grep Query-Writing Experiences

Read these compact, non-redundant query-writing lessons before constructing a
query. Treat them as heuristics and validate them against the current language
and ast-grep version. Add a lesson only when existing guidance cannot be
materially extended instead.
"""


def _skill_root(skill_roots: Mapping[str, Path], skill_name: str) -> Path:
    try:
        root = Path(skill_roots[skill_name]).resolve()
    except KeyError as exc:
        available = ", ".join(sorted(skill_roots)) or "none"
        raise ValueError(f"unknown task skill {skill_name!r}; available skills: {available}") from exc
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise ValueError(f"skill root is unavailable: {skill_name!r}")
    return root


@contextmanager
def _skill_resource_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    """Coordinate skill reads and evolution writes across processes."""

    with (root / "SKILL.md").open("rb") as handle:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _skill_file(skill_roots: Mapping[str, Path], skill_name: str, resource: str) -> tuple[Path, Path]:
    root = _skill_root(skill_roots, skill_name)
    relative = Path(resource)
    if not resource.strip() or relative.is_absolute():
        raise ValueError("skill resource path must be non-empty and relative")
    candidate = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"skill resource must not traverse symbolic links: {resource}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"skill resource must stay inside {skill_name!r}: {resource}") from exc
    if not resolved.is_file():
        raise ValueError(f"skill resource does not exist: {skill_name}/{resource}")
    if resolved.stat().st_size > MAX_SKILL_RESOURCE_BYTES:
        raise ValueError(
            f"skill resource exceeds the {MAX_SKILL_RESOURCE_BYTES}-byte read limit: "
            f"{skill_name}/{resource}"
        )
    return root, resolved


def list_skill_resources(
    skill_roots: Mapping[str, Path],
    skill_name: str,
    *,
    max_files: int = MAX_SKILL_RESOURCE_FILES,
) -> str:
    """List regular non-symlink files under one task-declared skill root."""
    if max_files < 1 or max_files > MAX_SKILL_RESOURCE_FILES:
        raise ValueError(f"max_files must be between 1 and {MAX_SKILL_RESOURCE_FILES}")
    root = _skill_root(skill_roots, skill_name)
    resources: list[str] = []
    truncated = False
    with _skill_resource_lock(root, exclusive=False):
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if "__pycache__" in relative.parts:
                continue
            if len(resources) >= max_files:
                truncated = True
                break
            resources.append(relative.as_posix())
    rendered = resources or ["(no skill resources)"]
    if truncated:
        rendered.append(truncation_footer())
    return "\n".join(rendered)


def read_skill_resource(
    skill_roots: Mapping[str, Path],
    skill_name: str,
    resource: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = MAX_SKILL_RESOURCE_LINES,
) -> str:
    """Read one bounded line range from a task-declared skill resource."""
    if start_line < 1 or max_lines < 1 or max_lines > MAX_SKILL_RESOURCE_LINES:
        raise ValueError(
            f"start_line must be positive and max_lines must be between 1 and {MAX_SKILL_RESOURCE_LINES}"
        )
    root = _skill_root(skill_roots, skill_name)
    with _skill_resource_lock(root, exclusive=False):
        _, source = _skill_file(skill_roots, skill_name, resource)
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if start_line > len(lines):
        raise ValueError(
            f"start_line {start_line} exceeds {skill_name}/{resource} length ({len(lines)} lines)"
        )
    resolved_end = min(len(lines), end_line if end_line is not None else start_line + max_lines - 1)
    if resolved_end < start_line or resolved_end - start_line + 1 > max_lines:
        raise ValueError(f"requested line range exceeds the {max_lines}-line read limit")
    return format_file_read(
        path=f"{skill_name}/{source.relative_to(root).as_posix()}",
        content="".join(lines[start_line - 1 : resolved_end]),
        start_line=start_line,
        end_line=resolved_end,
        total_lines=len(lines),
        truncated=resolved_end < len(lines),
    )


def _read_ast_grep_experiences(path: Path) -> list[AstGrepExperience]:
    if not path.exists():
        return []
    experiences: list[AstGrepExperience] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("- "):
            continue
        match = _EXPERIENCE_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"malformed ast-grep experience at line {line_number}")
        experiences.append(
            AstGrepExperience(
                outcome=AstGrepExperienceOutcome(match.group(1)),
                lesson=match.group(2),
            )
        )
    return experiences


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _merge_ast_grep_experience(
    experiences: list[AstGrepExperience],
    candidate: AstGrepExperience,
) -> bool:
    """Add or extend one lesson without retaining a redundant shorter form."""

    for index, current in enumerate(experiences):
        if candidate.identity == current.identity:
            return False
        padded_candidate = f" {candidate.identity} "
        padded_current = f" {current.identity} "
        if padded_current in padded_candidate:
            if candidate.outcome != current.outcome:
                return False
            experiences[index] = candidate
            return True
        if padded_candidate in padded_current:
            return False
    experiences.append(candidate)
    return True


def record_ast_grep_experiences(
    skill_root: str | Path,
    experiences: Sequence[AstGrepExperience],
) -> int:
    """Read, consolidate, and atomically persist bounded query-writing lessons."""

    incoming = [AstGrepExperience.model_validate(item) for item in experiences]
    if not incoming:
        return 0
    root = _skill_root({"ast-grep": Path(skill_root)}, "ast-grep")
    path = root / AST_GREP_EXPERIENCES_RESOURCE
    with _skill_resource_lock(root, exclusive=True):
        current = _read_ast_grep_experiences(path)
        merged: list[AstGrepExperience] = []
        for item in current:
            _merge_ast_grep_experience(merged, item)
        changed = merged != current
        updates = 0
        for item in incoming:
            if _merge_ast_grep_experience(merged, item):
                changed = True
                updates += 1
        if len(merged) > MAX_AST_GREP_EXPERIENCES:
            merged = merged[-MAX_AST_GREP_EXPERIENCES:]
            changed = True
        if not changed:
            return 0
        body = "\n".join(
            f"- **{item.outcome.value}**: {item.lesson}"
            for item in merged
        )
        _atomic_write_text(path, f"{_EXPERIENCE_HEADER}\n{body}\n")
        return updates


__all__ = [
    "MAX_SKILL_RESOURCE_BYTES",
    "MAX_SKILL_RESOURCE_FILES",
    "MAX_SKILL_RESOURCE_LINES",
    "AST_GREP_EXPERIENCES_RESOURCE",
    "MAX_AST_GREP_EXPERIENCES",
    "list_skill_resources",
    "record_ast_grep_experiences",
    "read_skill_resource",
]
