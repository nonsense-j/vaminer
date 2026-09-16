"""Bounded navigation over one analyzed Src Root."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from .errors import ToolExecutionError, ToolInputError, validate_text_argument
from .text import format_file_read, truncation_footer

MAX_SRC_READ_BYTES = 512 * 1024
MAX_SRC_READ_LINES = 200
MAX_SRC_SEARCH_RESULTS = 100
MAX_SRC_SEARCH_BYTES = 512 * 1024
SRC_SEARCH_CONTEXT_LINES = 1
MAX_SRC_LIST_RESULTS = 500
MAX_SRC_ERROR_CHARS = 2_000


def _src_scope_path(
    src_root: Path,
    path: str | None,
    *,
    allow_file: bool = False,
) -> tuple[Path, Path]:
    """Resolve a Src-Root-relative scope without following symlinks."""

    root = Path(src_root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"src_root is not an existing directory: {root}")
    if path is None or path in {"", "."}:
        return root, root
    validate_text_argument(path, "path")
    relative = Path(path)
    if relative.is_absolute():
        raise ToolInputError("src path must be relative to the bound Src Root")
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current /= part
        if current.is_symlink():
            raise ToolInputError(f"src path must not traverse symbolic links: {path}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ToolInputError(f"src path must stay inside the bound Src Root: {path}") from exc
    if not target.exists():
        relative_parts = relative.parts
        if len(relative_parts) <= len(root.parts) and root.parts[-len(relative_parts) :] == relative_parts:
            raise ToolInputError(
                f"src path repeats the bound Src Root: {path}; "
                f"bound root: {root.as_posix()}; use '.' or omit path for the root"
            )
        nearest = target.parent
        while nearest != root and not nearest.is_dir():
            nearest = nearest.parent
        nearest_relative = nearest.relative_to(root).as_posix()
        raise ToolInputError(
            f"src path does not exist relative to bound root {root.as_posix()}: {path}; "
            f"nearest existing directory: {nearest_relative}"
        )
    if not target.is_dir() and not (allow_file and target.is_file()):
        expected = "a file or directory" if allow_file else "a directory"
        raise ToolInputError(f"src path is not {expected}: {path}")
    return root, target


def _bounded_process_error(label: str, stderr: str, returncode: int) -> ToolExecutionError | ToolInputError:
    detail = stderr.strip()[:MAX_SRC_ERROR_CHARS]
    if "regex parse error:" in stderr or "error parsing glob" in stderr:
        return ToolInputError(detail)
    return ToolExecutionError(detail or f"{label} exited with {returncode}")


def _bounded_complete_output(value: str) -> tuple[str, bool]:
    """Return only complete lines within the Src tool output budget."""

    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_SRC_SEARCH_BYTES:
        return value, False
    prefix = encoded[:MAX_SRC_SEARCH_BYTES].decode("utf-8", errors="ignore")
    if not prefix.endswith("\n"):
        prefix = prefix.rsplit("\n", 1)[0] if "\n" in prefix else ""
    return prefix, True


def list_src_files(
    src_root: Path,
    *,
    path: str | None = None,
    glob: str | None = None,
    max_results: int = MAX_SRC_LIST_RESULTS,
) -> str:
    """List Src-Root-relative files under one directory.

    Args:
        src_root: Bound source root.
        path: Optional directory relative to ``src_root``; omit it for the root.
        glob: Optional ripgrep glob expression.
        max_results: Maximum number of paths to return.

    The tool is already rooted at the analyzed Src Root. ``path`` must be an
    existing directory relative to that root; do not include its workspace
    prefix. Omit it to list from the root. ``glob`` uses ripgrep glob syntax.
    Results are sorted and returned as one path per line. Broad listings that
    exceed the output budget return the collected prefix and a message asking
    for a narrower scope.
    """

    if max_results < 1 or max_results > MAX_SRC_LIST_RESULTS:
        raise ToolInputError(f"max_results must be between 1 and {MAX_SRC_LIST_RESULTS}")
    if glob is not None and (not glob.strip() or len(glob) > 500):
        raise ToolInputError("glob must be between 1 and 500 characters")
    if glob is not None:
        validate_text_argument(glob, "glob")
    root, target = _src_scope_path(src_root, path)
    command = [
        "rg",
        "--files",
        "--hidden",
        "--glob",
        "!.git/**",
        "--color",
        "never",
    ]
    if glob is not None:
        command.append(f"--glob={glob}")
    command.append(str(target))
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("src file listing requires rg on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        detail = _stream_text(exc.stderr) or "src file listing timed out after 20 seconds"
        raise ToolExecutionError(detail) from exc
    if completed.returncode not in (0, 1):
        raise _bounded_process_error("rg --files", completed.stderr, completed.returncode)
    bounded_stdout, output_truncated = _bounded_complete_output(completed.stdout)

    files: list[str] = []
    for raw in bounded_stdout.splitlines():
        candidate = Path(raw).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        files.append(relative)
    files.sort()
    rendered = files[:max_results] or ["(no source files)"]
    if output_truncated:
        rendered.append(
            truncation_footer(
                f"src file listing exceeded the {MAX_SRC_SEARCH_BYTES}-byte output limit; "
                "results collected before the limit were returned; narrow path or glob for complete results"
            )
        )
    elif len(files) > max_results:
        rendered.append(truncation_footer())
    return "\n".join(rendered)


def read_src_file(
    src_root: Path,
    path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = MAX_SRC_READ_LINES,
    full_file: bool = False,
) -> str:
    """Read a bounded line range or one complete Src-Root-relative file.

    Args:
        src_root: Bound source root.
        path: Source file path relative to ``src_root``.
        start_line: One-based first line to return.
        end_line: Optional inclusive last line to return.
        max_lines: Maximum number of lines in a bounded read.
        full_file: Return the complete file instead of a bounded line range.

    The tool is already rooted at the analyzed Src Root, so ``path`` must be
    relative to that root and must not include its workspace prefix. Line
    numbers are one-based and ``end_line`` is inclusive. Reads are capped at
    the configured line limit; when the result header says ``more available``,
    continue from one line after its displayed end line. Set ``full_file`` to
    read the whole file without line bounds; the byte limit still applies. A
    start position past EOF returns the file length and a recovery message.
    Oversized files are rejected.
    """

    if not path:
        raise ToolInputError("src file path must be non-empty")
    _, target = _src_scope_path(src_root, path, allow_file=True)
    if not target.is_file():
        raise ToolInputError(f"src path is not a regular file: {path}")
    if start_line < 1 or max_lines < 1:
        raise ToolInputError("start_line and max_lines must be positive")
    if full_file and (start_line != 1 or end_line is not None):
        raise ToolInputError("full_file cannot be combined with start_line or end_line bounds")
    if target.stat().st_size > MAX_SRC_READ_BYTES:
        raise ToolInputError(f"src file exceeds the {MAX_SRC_READ_BYTES}-byte read limit: {path}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if not lines and start_line == 1:
        return format_file_read(
            path=Path(path).as_posix(),
            content="",
            start_line=1,
            end_line=0,
            total_lines=0,
            truncated=False,
        )
    if start_line > len(lines):
        return format_file_read(
            path=Path(path).as_posix(),
            content="",
            start_line=start_line,
            end_line=len(lines),
            total_lines=len(lines),
            truncated=False,
            message=(
                f"start_line {start_line} is past EOF; {path} has {len(lines)} lines; "
                "no content was returned"
            ),
        )
    effective_max_lines = MAX_SRC_READ_BYTES if full_file else max_lines
    requested_end = end_line if end_line is not None else start_line + effective_max_lines - 1
    if requested_end < start_line:
        raise ToolInputError("end_line must be greater than or equal to start_line")
    resolved_end = min(len(lines), requested_end, start_line + effective_max_lines - 1)
    return format_file_read(
        path=Path(path).as_posix(),
        content="".join(lines[start_line - 1 : resolved_end]),
        start_line=start_line,
        end_line=resolved_end,
        total_lines=len(lines),
        truncated=resolved_end < len(lines),
    )


def search_src_files(
    src_root: Path,
    pattern: str,
    *,
    path: str | None = None,
    mode: str = "literal",
    glob: str | None = None,
    max_results: int = MAX_SRC_SEARCH_RESULTS,
) -> str:
    """Search one Src-Root-relative file or directory.

    Args:
        src_root: Bound source root.
        pattern: Single-line literal text or regular expression to search.
        path: Optional file or directory relative to ``src_root``; omit it for the root.
        mode: ``literal`` for exact text or ``regex`` for a regular expression.
        glob: Optional ripgrep glob expression.
        max_results: Maximum number of matching lines to return.

    The tool is already rooted at the analyzed Src Root. ``path`` may be an
    existing file or directory relative to that root; do not include its
    workspace prefix. Omit it to search from the root. Literal mode preserves
    ``pattern`` exactly. Every match always includes one line of code context
    before and after when those lines exist; the context size is intentionally
    fixed and is not a tool argument. Broad searches that exceed the output
    budget return a truncated result and a message asking for a narrower scope.
    Matched lines use ``path:line:text`` and context lines use
    ``path-line-text``.
    """

    if not isinstance(pattern, str):
        raise ToolInputError("search pattern must be a string")
    if not pattern.strip() or len(pattern) > 500:
        raise ToolInputError("search pattern must be between 1 and 500 characters")
    validate_text_argument(pattern, "pattern")
    if "\n" in pattern:
        raise ToolInputError("search pattern must be a single line; multiline search is not supported")
    if mode not in {"literal", "regex"}:
        raise ToolInputError("search mode must be 'literal' or 'regex'")
    if glob is not None and (not glob.strip() or len(glob) > 500):
        raise ToolInputError("glob must be between 1 and 500 characters")
    if glob is not None:
        validate_text_argument(glob, "glob")
    if max_results < 1 or max_results > MAX_SRC_SEARCH_RESULTS:
        raise ToolInputError(f"max_results must be between 1 and {MAX_SRC_SEARCH_RESULTS}")
    root, target = _src_scope_path(src_root, path, allow_file=True)

    command = [
        "rg",
        "--json",
        "--hidden",
        "--glob",
        "!.git/**",
        "--context",
        str(SRC_SEARCH_CONTEXT_LINES),
        "--color",
        "never",
    ]
    if mode == "literal":
        command.append("--fixed-strings")
    if glob is not None:
        command.append(f"--glob={glob}")
    command.extend((f"--regexp={pattern}", str(target)))
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("src search requires rg on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        detail = _stream_text(exc.stderr) or "src search timed out after 20 seconds"
        raise ToolExecutionError(detail) from exc
    if completed.returncode not in (0, 1):
        raise _bounded_process_error("rg search", completed.stderr, completed.returncode)
    bounded_stdout, output_truncated = _bounded_complete_output(completed.stdout)

    matches: list[tuple[str, int, str]] = []
    source_lines: dict[tuple[str, int], str] = {}
    for line in bounded_stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"rg returned invalid JSON: {exc}") from exc
        if not isinstance(event, dict):
            raise ToolExecutionError("rg returned an unexpected JSON shape")
        event_type = event.get("type")
        if event_type not in {"match", "context"}:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("rg returned a malformed match")
        line_number = data.get("line_number")
        if type(line_number) is not int or line_number < 1:
            raise ToolExecutionError("rg returned a malformed line number")
        absolute = _rg_text(data.get("path"), errors="surrogateescape")
        text = _rg_text(data.get("lines"), errors="replace")
        try:
            relative = Path(absolute).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        normalized_text = text.rstrip("\r\n")
        source_lines[(relative, line_number)] = normalized_text
        if event_type == "match":
            matches.append((relative, line_number, normalized_text))
    matches.sort()
    selected_matches = matches[:max_results]
    selected_lines = {(file, line_number) for file, line_number, _ in selected_matches}
    snippets: dict[tuple[str, int], str] = {}
    for file, line_number, _ in selected_matches:
        for context_line in range(
            max(1, line_number - SRC_SEARCH_CONTEXT_LINES),
            line_number + SRC_SEARCH_CONTEXT_LINES + 1,
        ):
            key = (file, context_line)
            if key in source_lines:
                snippets[key] = source_lines[key]

    rendered: list[str] = []
    previous_file: str | None = None
    previous_line = 0
    for (file, line_number), text in sorted(snippets.items()):
        if previous_file is not None and (file != previous_file or line_number > previous_line + 1):
            rendered.append("--")
        separator = ":" if (file, line_number) in selected_lines else "-"
        rendered.append(f"{file}{separator}{line_number}{separator}{text}")
        previous_file = file
        previous_line = line_number
    if not rendered:
        rendered.append("(no matches)")
    if output_truncated:
        rendered.append(
            truncation_footer(
                f"src search output exceeded the {MAX_SRC_SEARCH_BYTES}-byte limit; "
                "matches collected before the limit were returned; "
                "narrow path, glob, or pattern for complete results"
            )
        )
    elif len(matches) > max_results:
        rendered.append(truncation_footer())
    return "\n".join(rendered)


def _rg_text(value: object, *, errors: str) -> str:
    """ripgrep uses base64 for paths or lines that are not UTF-8."""
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("bytes"), str):
            try:
                return base64.b64decode(value["bytes"], validate=True).decode("utf-8", errors=errors)
            except ValueError as exc:
                raise ToolExecutionError("rg returned invalid base64 text") from exc
    raise ToolExecutionError("rg returned malformed text")


def _stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", errors="replace")


__all__ = [
    "MAX_SRC_ERROR_CHARS",
    "MAX_SRC_LIST_RESULTS",
    "MAX_SRC_READ_BYTES",
    "MAX_SRC_READ_LINES",
    "MAX_SRC_SEARCH_BYTES",
    "MAX_SRC_SEARCH_RESULTS",
    "SRC_SEARCH_CONTEXT_LINES",
    "list_src_files",
    "read_src_file",
    "search_src_files",
]
