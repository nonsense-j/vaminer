"""Portable filesystem-name helpers."""

from __future__ import annotations

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


def is_windows_reserved_name(value: str) -> bool:
    """Return whether one filename is reserved by the Windows device namespace."""

    stem = value.rstrip(" .").partition(".")[0]
    return stem.casefold() in _WINDOWS_RESERVED_NAMES


__all__ = ["is_windows_reserved_name"]
