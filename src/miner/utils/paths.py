"""Portable filesystem and filename helpers."""

from __future__ import annotations

import os
from pathlib import Path

_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def absolute_path(path: Path) -> Path:
    """Return an absolute path that remains usable for long Windows paths."""

    absolute = Path(os.path.normpath(os.fspath(Path(path).expanduser().absolute())))
    if os.name != "nt":
        return absolute

    value = os.fspath(absolute)
    if value.startswith("\\\\?\\"):
        return absolute
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def resolve_path(path: Path, *, strict: bool = False) -> Path:
    """Resolve a path without silently losing Windows extended-length support."""

    candidate = absolute_path(path)
    try:
        return candidate.resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"unable to resolve path: {path}") from exc


def is_windows_reserved_name(value: str) -> bool:
    """Return whether one filename is reserved by the Windows device namespace."""

    stem = value.rstrip(" .").partition(".")[0]
    return stem.casefold() in _WINDOWS_RESERVED_NAMES


__all__ = ["absolute_path", "is_windows_reserved_name", "resolve_path"]
