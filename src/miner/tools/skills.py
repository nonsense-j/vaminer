"""Bounded read-only access to task-declared skill resources."""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import portalocker

from ..models.anchors import AstGrepExperience, AstGrepExperienceMode
from .errors import ToolInputError, validate_text_argument
from .text import format_file_read, truncation_footer

MAX_SKILL_RESOURCE_FILES = 100
MAX_SKILL_RESOURCE_BYTES = 256 * 1024
MAX_SKILL_RESOURCE_LINES = 200
AST_GREP_EXPERIENCES_RESOURCE = "references/experiences.md"
_EXPERIENCE_LINE = re.compile(
    r"^- \[(?P<lesson_id>[A-Za-z][A-Za-z0-9]*-[1-9][0-9]*)\] (?P<lesson>.+)$"
)
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
        raise ToolInputError(f"unknown task skill {skill_name!r}; available skills: {available}") from exc
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise RuntimeError(f"skill root is unavailable: {skill_name!r}")
    return root


@contextmanager
def _skill_resource_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    """Coordinate skill reads and evolution writes across processes."""

    with (root / "SKILL.md").open("rb") as handle:
        operation = (
            portalocker.LockFlags.EXCLUSIVE
            if exclusive
            else portalocker.LockFlags.SHARED
        ) | portalocker.LockFlags.NON_BLOCKING
        while True:
            try:
                portalocker.lock(handle, operation)
                break
            except portalocker.exceptions.AlreadyLocked:
                time.sleep(0.05)
        try:
            yield
        finally:
            portalocker.unlock(handle)


def _skill_file(skill_roots: Mapping[str, Path], skill_name: str, resource: str) -> tuple[Path, Path]:
    root = _skill_root(skill_roots, skill_name)
    validate_text_argument(resource, "resource")
    relative = Path(resource)
    if not resource.strip() or relative.is_absolute():
        raise ToolInputError("skill resource path must be non-empty and relative")
    candidate = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ToolInputError(f"skill resource must not traverse symbolic links: {resource}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolInputError(f"skill resource must stay inside {skill_name!r}: {resource}") from exc
    if not resolved.is_file():
        raise ToolInputError(f"skill resource does not exist: {skill_name}/{resource}")
    if resolved.stat().st_size > MAX_SKILL_RESOURCE_BYTES:
        raise ToolInputError(
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
    """List regular non-symlink files under one task-declared skill root.

    Args:
        skill_roots: Mapping of task skill names to their bound directories.
        skill_name: Declared skill whose resources should be listed.
        max_files: Maximum number of resource paths to return.
    """
    if max_files < 1 or max_files > MAX_SKILL_RESOURCE_FILES:
        raise ToolInputError(f"max_files must be between 1 and {MAX_SKILL_RESOURCE_FILES}")
    root = _skill_root(skill_roots, skill_name)
    resources: list[str] = []
    truncated = False
    with _skill_resource_lock(root, exclusive=False):
        for candidate in sorted(
            root.rglob("*"),
            key=lambda path: path.relative_to(root).as_posix(),
        ):
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
    """Read one bounded line range from a task-declared skill resource.

    Args:
        skill_roots: Mapping of task skill names to their bound directories.
        skill_name: Declared skill containing the resource.
        resource: Relative resource path within the skill root.
        start_line: One-based first line to return.
        end_line: Optional inclusive last line to return.
        max_lines: Maximum number of lines to return.
    """
    if start_line < 1 or max_lines < 1 or max_lines > MAX_SKILL_RESOURCE_LINES:
        raise ToolInputError(
            f"start_line must be positive and max_lines must be between 1 and {MAX_SKILL_RESOURCE_LINES}"
        )
    root = _skill_root(skill_roots, skill_name)
    with _skill_resource_lock(root, exclusive=False):
        _, source = _skill_file(skill_roots, skill_name, resource)
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if start_line > len(lines):
        raise ToolInputError(
            f"start_line {start_line} exceeds {skill_name}/{resource} length ({len(lines)} lines)"
        )
    resolved_end = min(len(lines), end_line if end_line is not None else start_line + max_lines - 1)
    if resolved_end < start_line or resolved_end - start_line + 1 > max_lines:
        raise ToolInputError(f"requested line range exceeds the {max_lines}-line read limit")
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
                mode=AstGrepExperienceMode.ADD,
                lesson_id=match.group("lesson_id"),
                lesson=match.group("lesson"),
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


def _next_lesson_id(scope: str, experiences: Sequence[AstGrepExperience]) -> str:
    used = {
        experience.lesson_id.casefold()
        for experience in experiences
        if experience.scope.casefold() == scope.casefold()
    }
    number = 1
    while f"{scope}-{number}".casefold() in used:
        number += 1
    return f"{scope}-{number}"


def _render_ast_grep_experiences(experiences: Sequence[AstGrepExperience]) -> str:
    grouped: dict[str, list[AstGrepExperience]] = {}
    scopes: list[str] = []
    for experience in experiences:
        scope = experience.scope
        if scope not in grouped:
            grouped[scope] = []
            scopes.append(scope)
        grouped[scope].append(experience)

    ordered_scopes = (["ALL"] if "ALL" in grouped else []) + [
        scope for scope in scopes if scope != "ALL"
    ]
    lines = [_EXPERIENCE_HEADER.rstrip()]
    for scope in ordered_scopes:
        lines.extend(("", f"## {'Language-Agnostic Lessons' if scope == 'ALL' else f'{scope} Query Lessons'}", ""))
        lines.extend(f"- [{item.lesson_id}] {item.lesson}" for item in grouped[scope])
    return "\n".join(lines) + "\n"


def record_ast_grep_experiences(
    skill_root: str | Path,
    experiences: Sequence[AstGrepExperience],
) -> int:
    """Read, apply, and atomically persist ID-addressed query-writing lessons."""

    incoming = [AstGrepExperience.model_validate(item.model_dump()) for item in experiences]
    if not incoming:
        return 0
    root = _skill_root({"ast-grep": Path(skill_root)}, "ast-grep")
    path = root / AST_GREP_EXPERIENCES_RESOURCE
    with _skill_resource_lock(root, exclusive=True):
        current = _read_ast_grep_experiences(path)
        merged = list(current)
        changed = False
        updates = 0
        for item in incoming:
            index = next(
                (index for index, current_item in enumerate(merged) if current_item.identity == item.identity),
                None,
            )
            if item.mode is AstGrepExperienceMode.ADD:
                if index is not None:
                    if merged[index].lesson == item.lesson:
                        continue
                    item = item.model_copy(update={"lesson_id": _next_lesson_id(item.scope, merged)})
                merged.append(item)
                changed = True
                updates += 1
                continue
            if index is None:
                raise ValueError(f"cannot replace unknown ast-grep experience {item.lesson_id}")
            if merged[index].lesson == item.lesson:
                continue
            merged[index] = item
            changed = True
            updates += 1
        if not changed:
            return 0
        _atomic_write_text(path, _render_ast_grep_experiences(merged))
        return updates


__all__ = [
    "AST_GREP_EXPERIENCES_RESOURCE",
    "MAX_SKILL_RESOURCE_BYTES",
    "MAX_SKILL_RESOURCE_FILES",
    "MAX_SKILL_RESOURCE_LINES",
    "list_skill_resources",
    "read_skill_resource",
    "record_ast_grep_experiences",
]
