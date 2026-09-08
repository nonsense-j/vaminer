"""Shared fixed text formats for file-oriented tools."""

from __future__ import annotations


def format_file_read(
    *,
    path: str,
    content: str,
    start_line: int,
    end_line: int,
    total_lines: int,
    truncated: bool,
    message: str | None = None,
) -> str:
    if total_lines == 0:
        span = "empty"
    elif start_line > end_line:
        span = f"requested line {start_line}; EOF at {total_lines}"
    else:
        span = f"lines {start_line}-{end_line} of {total_lines}"
    availability = " | more available" if truncated else ""
    parts = [f"==> {path} | {span}{availability} <=="]
    if content:
        parts.append(content.rstrip("\n"))
    if message:
        parts.append(f"-- {message}")
    return "\n".join(parts)


def truncation_footer(message: str | None = None) -> str:
    return f"-- truncated: {message}" if message else "-- truncated"


__all__ = ["format_file_read", "truncation_footer"]
