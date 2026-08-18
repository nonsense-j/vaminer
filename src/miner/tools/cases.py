"""Restricted case-artifact operations shared by agent runtimes."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

CASE_ARTIFACT_RE = re.compile(r"^case\d+(?:_var\d+)?\.[A-Za-z0-9]+$")
MAX_CASE_ARTIFACT_BYTES = 128 * 1024
MAX_CASE_READ_BYTES = 512 * 1024
MAX_CASE_READ_LINES = 200


def _case_artifact_path(cases_dir: Path, path: str, *, create_root: bool = False) -> Path:
    """Resolve one bare case filename and reject nested or symlinked targets."""
    if not path or Path(path).name != path or CASE_ARTIFACT_RE.fullmatch(path) is None:
        raise ValueError(
            "case artifact path must be a bare filename matching caseN.ext or caseN_varM.ext"
        )

    root = Path(cases_dir)
    if create_root:
        root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"cases directory is not an existing directory: {root}")

    target = root / path
    if target.is_symlink():
        raise ValueError(f"case artifact must not be a symbolic link: {path}")
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:  # pragma: no cover - filename validation already blocks traversal.
        raise ValueError(f"case artifact must stay inside the cases directory: {path}") from exc
    return resolved


def list_case_artifacts(cases_dir: Path) -> list[str]:
    """List valid top-level case artifacts in deterministic order."""
    root = Path(cases_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"cases directory is not an existing directory: {root}")
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_file() and not path.is_symlink() and CASE_ARTIFACT_RE.fullmatch(path.name)
    )


def read_case_artifact(
    cases_dir: Path,
    path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = MAX_CASE_READ_LINES,
) -> dict[str, Any]:
    """Read a bounded line range from one case artifact.

    A start position past EOF returns empty content together with the artifact
    length and a recovery message.
    """
    target = _case_artifact_path(cases_dir, path)
    if not target.is_file():
        raise ValueError(f"case artifact does not exist: {path}")
    size = target.stat().st_size
    if size > MAX_CASE_READ_BYTES:
        raise ValueError(f"case artifact exceeds the {MAX_CASE_READ_BYTES}-byte read limit: {path}")
    if start_line < 1:
        raise ValueError("start_line must be positive")
    if max_lines < 1:
        raise ValueError("max_lines must be positive")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    total_lines = len(lines)
    if total_lines == 0:
        result: dict[str, Any] = {
            "path": path,
            "content": "",
            "start_line": start_line,
            "end_line": 0,
            "total_lines": 0,
            "truncated": False,
        }
        if start_line != 1 or end_line not in {None, 0, 1}:
            result["message"] = (
                f"requested range starts past EOF; {path} is empty; no content was returned"
            )
        return result
    if start_line > total_lines:
        return {
            "path": path,
            "content": "",
            "start_line": start_line,
            "end_line": total_lines,
            "total_lines": total_lines,
            "truncated": False,
            "message": (
                f"start_line {start_line} is past EOF; {path} has {total_lines} lines; "
                "no content was returned"
            ),
        }

    resolved_end = min(total_lines, end_line if end_line is not None else start_line + max_lines - 1)
    if resolved_end < start_line:
        raise ValueError("end_line must be greater than or equal to start_line")
    if resolved_end - start_line + 1 > max_lines:
        raise ValueError(f"requested line range exceeds the {max_lines}-line read limit")
    return {
        "path": path,
        "content": "".join(lines[start_line - 1 : resolved_end]),
        "start_line": start_line,
        "end_line": resolved_end,
        "total_lines": total_lines,
        "truncated": resolved_end < total_lines,
    }


def write_case_artifact(cases_dir: Path, path: str, content: str) -> dict[str, Any]:
    """Atomically write one bounded, non-empty case artifact."""
    target = _case_artifact_path(cases_dir, path, create_root=True)
    encoded = content.encode("utf-8")
    if not content.strip():
        raise ValueError("case artifact content must be non-empty")
    if len(encoded) > MAX_CASE_ARTIFACT_BYTES:
        raise ValueError(f"case artifact exceeds the {MAX_CASE_ARTIFACT_BYTES}-byte write limit")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=".case-artifact-",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary_path = Path(temporary.name)
        temporary_path.replace(target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return {"path": path, "bytes_written": len(encoded)}


__all__ = [
    "CASE_ARTIFACT_RE",
    "MAX_CASE_ARTIFACT_BYTES",
    "MAX_CASE_READ_BYTES",
    "MAX_CASE_READ_LINES",
    "list_case_artifacts",
    "read_case_artifact",
    "write_case_artifact",
]
