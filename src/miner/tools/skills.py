"""Bounded read-only access to task-declared skill resources."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

MAX_SKILL_RESOURCE_FILES = 100
MAX_SKILL_RESOURCE_BYTES = 256 * 1024
MAX_SKILL_RESOURCE_LINES = 200


def _skill_root(skill_roots: Mapping[str, Path], skill_name: str) -> Path:
    try:
        root = Path(skill_roots[skill_name]).resolve()
    except KeyError as exc:
        available = ", ".join(sorted(skill_roots)) or "none"
        raise ValueError(f"unknown task skill {skill_name!r}; available skills: {available}") from exc
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise ValueError(f"skill root is unavailable: {skill_name!r}")
    return root


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
) -> dict[str, object]:
    """List regular non-symlink files under one task-declared skill root."""
    if max_files < 1 or max_files > MAX_SKILL_RESOURCE_FILES:
        raise ValueError(f"max_files must be between 1 and {MAX_SKILL_RESOURCE_FILES}")
    root = _skill_root(skill_roots, skill_name)
    resources: list[str] = []
    truncated = False
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
    return {
        "skill": skill_name,
        "resources": resources,
        "truncated": truncated,
    }


def read_skill_resource(
    skill_roots: Mapping[str, Path],
    skill_name: str,
    resource: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = MAX_SKILL_RESOURCE_LINES,
) -> dict[str, object]:
    """Read one bounded line range from a task-declared skill resource."""
    if start_line < 1 or max_lines < 1 or max_lines > MAX_SKILL_RESOURCE_LINES:
        raise ValueError(
            f"start_line must be positive and max_lines must be between 1 and {MAX_SKILL_RESOURCE_LINES}"
        )
    root, source = _skill_file(skill_roots, skill_name, resource)
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if start_line > len(lines):
        raise ValueError(
            f"start_line {start_line} exceeds {skill_name}/{resource} length ({len(lines)} lines)"
        )
    resolved_end = min(len(lines), end_line if end_line is not None else start_line + max_lines - 1)
    if resolved_end < start_line or resolved_end - start_line + 1 > max_lines:
        raise ValueError(f"requested line range exceeds the {max_lines}-line read limit")
    return {
        "skill": skill_name,
        "path": source.relative_to(root).as_posix(),
        "content": "".join(lines[start_line - 1 : resolved_end]),
        "start_line": start_line,
        "end_line": resolved_end,
        "total_lines": len(lines),
        "truncated": resolved_end < len(lines),
    }


__all__ = [
    "MAX_SKILL_RESOURCE_BYTES",
    "MAX_SKILL_RESOURCE_FILES",
    "MAX_SKILL_RESOURCE_LINES",
    "list_skill_resources",
    "read_skill_resource",
]
