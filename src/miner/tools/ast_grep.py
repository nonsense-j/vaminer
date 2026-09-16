"""Runtime-neutral ast-grep query and pattern-debug operations."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from .errors import ToolExecutionError, ToolInputError, validate_text_argument

QueryType = Literal["pattern", "rule"]
OutputMode = Literal["count", "sample", "full"]
DebugQuery = Literal["pattern", "ast", "cst", "sexp"]

_AST_GREP_INSTALL_HINT = (
    "Uninstall the .cmd shim, then reinstall the native ast-grep .exe with "
    "Scoop (`scoop uninstall ast-grep`, then `scoop install ast-grep`) or "
    "Cargo (`cargo uninstall ast-grep`, then `cargo install ast-grep --locked`)."
)
_ERROR_NODE_WARNING = "warning: pattern contains an error node"


class AstGrepUnavailableError(RuntimeError):
    """The ast-grep CLI cannot be used by the host."""


def _resolve_executable(executable: str | None = None) -> str:
    candidates = (executable,) if executable else ("ast-grep", "sg")
    resolved = next((path for name in candidates if (path := shutil.which(name))), None)
    if resolved is None:
        label = f"configured ast-grep executable {executable!r}" if executable else "ast-grep"
        raise AstGrepUnavailableError(f"{label} was not found on PATH")
    if resolved.casefold().endswith(".cmd"):
        raise AstGrepUnavailableError(
            f"ast-grep resolved to a .cmd shim instead of a native .exe binary: {resolved}. "
            f"{_AST_GREP_INSTALL_HINT}"
        )
    return resolved


def _validate_root(target_dir: str | Path) -> Path:
    root = Path(target_dir).resolve()
    if not root.is_dir():
        raise ToolInputError(f"target directory does not exist: {root}")
    return root


def _validate_language(language: str) -> None:
    if not isinstance(language, str) or not language.strip():
        raise ToolInputError("language must be a non-empty string")
    validate_text_argument(language, "language")


def _validate_timeout(timeout_seconds: int) -> None:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds < 1:
        raise ToolInputError("timeout_seconds must be a positive integer")


def _stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _run(command: list[str], *, root: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        diagnostic = _stream_text(exc.stderr) or _stream_text(exc.stdout)
        raise ToolExecutionError(diagnostic or f"ast-grep timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise ToolExecutionError(str(exc)) from exc

    if completed.stdout is None or completed.stderr is None:
        raise ToolExecutionError(f"ast-grep did not return captured output (exit code {completed.returncode})")
    if not isinstance(completed.stdout, (str, bytes)) or not isinstance(completed.stderr, (str, bytes)):
        raise ToolExecutionError(f"ast-grep returned invalid captured output (exit code {completed.returncode})")
    return completed


def _completed_streams(completed: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    return _stream_text(completed.stdout), _stream_text(completed.stderr)


def _raise_for_execution_error(completed: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    stdout, stderr = _completed_streams(completed)
    malformed_pattern = any(
        line.strip().casefold().startswith(_ERROR_NODE_WARNING)
        for line in stderr.splitlines()
    )
    if completed.returncode not in {0, 1} or malformed_pattern:
        raise ToolExecutionError(stderr or stdout or f"ast-grep exited with {completed.returncode}")
    return stdout, stderr


def _diagnostic(message: str, *, stdout: str, stderr: str) -> str:
    parts = [message]
    if stderr:
        parts.extend(("", "ast-grep stderr (verbatim):", stderr))
    if stdout:
        parts.extend(("", "ast-grep stdout (verbatim):", stdout))
    return "\n".join(parts)


def _has_top_level_key(query: str, key: str) -> bool:
    return any(line.startswith(f"{key}:") for line in query.splitlines())


def _make_rule(query: str, language: str) -> str:
    prefix = []
    if not _has_top_level_key(query, "id"):
        prefix.append("id: agent-query")
    if not _has_top_level_key(query, "language"):
        prefix.append(f"language: {language}")
    if _has_top_level_key(query, "rule"):
        return "\n".join([*prefix, query]) if prefix else query
    indented = "\n".join(f"  {line}" if line else "" for line in query.splitlines())
    return "\n".join([*prefix, "rule:", indented])


def _parse_matches(stdout: str, stderr: str) -> list[dict[str, Any]]:
    if not stdout.strip():
        return []
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(
            _diagnostic(f"ast-grep returned invalid JSON: {exc}", stdout=stdout, stderr=stderr)
        ) from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ToolExecutionError(
            _diagnostic("ast-grep returned an unexpected JSON shape", stdout=stdout, stderr=stderr)
        )
    return value


def _relative_file(root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ToolExecutionError("ast-grep returned a match without a file")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ToolExecutionError(f"ast-grep returned a file outside the target directory: {value}") from exc


def _coordinate(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ToolExecutionError("ast-grep returned a malformed match coordinate")
    return value + 1


def _normalize_site(root: Path, raw: dict[str, Any], *, include_metavariables: bool) -> dict[str, Any]:
    range_info = raw.get("range") if isinstance(raw.get("range"), dict) else {}
    start = range_info.get("start") if isinstance(range_info.get("start"), dict) else {}
    end = range_info.get("end") if isinstance(range_info.get("end"), dict) else {}
    text = raw.get("text", raw.get("lines"))
    if not isinstance(text, str):
        raise ToolExecutionError("ast-grep returned malformed match text")
    site: dict[str, Any] = {
        "file": _relative_file(root, raw.get("file")),
        "text": text,
        "start": {
            "line": _coordinate(start.get("line")),
            "column": _coordinate(start.get("column")),
        },
        "end": {
            "line": _coordinate(end.get("line")),
            "column": _coordinate(end.get("column")),
        },
    }
    if include_metavariables:
        meta_variables = raw.get("metaVariables") or {}
        if not isinstance(meta_variables, dict) or any(
            not isinstance(captures, dict) for captures in meta_variables.values()
        ):
            raise ToolExecutionError("ast-grep returned malformed metavariable captures")
        for captures in meta_variables.values():
            for capture in captures.values():
                values = capture if isinstance(capture, list) else [capture]
                if any(
                    not isinstance(value, str)
                    and not (isinstance(value, dict) and isinstance(value.get("text"), str))
                    for value in values
                ):
                    raise ToolExecutionError("ast-grep returned malformed metavariable captures")
        site["meta_variables"] = meta_variables
    return site


def _render_matches(root: Path, stdout: str, stderr: str, *, output: OutputMode, sample_size: int) -> str:
    try:
        matches = [
            _normalize_site(root, raw, include_metavariables=output == "full")
            for raw in _parse_matches(stdout, stderr)
        ]
    except ToolExecutionError as exc:
        if stdout in str(exc) or stderr in str(exc):
            raise
        raise ToolExecutionError(_diagnostic(str(exc), stdout=stdout, stderr=stderr)) from exc
    matches.sort(
        key=lambda site: (
            site["file"],
            site["start"]["line"],
            site["start"]["column"],
            site["end"]["line"],
            site["end"]["column"],
            site["text"],
        )
    )

    rendered = [
        f"matches: {len(matches)}",
        f"matched files: {len({site['file'] for site in matches})}",
    ]
    visible_matches = matches[:sample_size] if output == "sample" else matches if output == "full" else []
    for site in visible_matches:
        start = site["start"]
        end = site["end"]
        rendered.extend(
            (
                "--",
                f"==> {site['file']}:{start['line']}:{start['column']}-{end['line']}:{end['column']} <==",
                str(site["text"]).rstrip("\r\n"),
            )
        )
        for group, captures in site.get("meta_variables", {}).items():
            for name, capture in captures.items():
                values = capture if isinstance(capture, list) else [capture]
                for value in values:
                    text = value["text"] if isinstance(value, dict) else str(value)
                    rendered.append(f"capture {group}.{name}: {text}")
    if output == "sample" and len(matches) > sample_size:
        rendered.append("-- truncated")
    report = "\n".join(rendered)
    return f"{report}\n\nast-grep stderr (verbatim):\n{stderr}" if stderr else report


def run_query(
    target_dir: str | Path,
    *,
    language: str,
    query_type: QueryType,
    query: str,
    output: OutputMode = "sample",
    sample_size: int = 20,
    timeout_seconds: int = 60,
    executable: str | None = None,
) -> str:
    """Run one raw pattern or YAML rule and return normalized matches."""

    root = _validate_root(target_dir)
    _validate_language(language)
    if not isinstance(query_type, str) or query_type not in {"pattern", "rule"}:
        raise ToolInputError(f"unsupported query type: {query_type!r}")
    if not isinstance(query, str) or not query.strip():
        raise ToolInputError("query must be a non-empty string")
    validate_text_argument(query, "query")
    if not isinstance(output, str) or output not in {"count", "sample", "full"}:
        raise ToolInputError(f"unsupported output mode: {output!r}")
    if not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size < 1:
        raise ToolInputError("sample_size must be a positive integer")
    _validate_timeout(timeout_seconds)
    binary = _resolve_executable(executable)

    if query_type == "pattern":
        completed = _run(
            [binary, "run", f"--pattern={query}", f"--lang={language}", "--json=compact", "."],
            root=root,
            timeout_seconds=timeout_seconds,
        )
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="vaminer-ast-grep-") as temp_dir:
                rule_file = Path(temp_dir) / "rule.yml"
                rule_file.write_text(_make_rule(query, language), encoding="utf-8")
                completed = _run(
                    [binary, "scan", "--rule", str(rule_file), "--json=compact", "."],
                    root=root,
                    timeout_seconds=timeout_seconds,
                )
        except OSError as exc:
            raise ToolExecutionError(str(exc)) from exc

    stdout, stderr = _raise_for_execution_error(completed)
    return _render_matches(root, stdout, stderr, output=output, sample_size=sample_size)


def debug_pattern(
    target_dir: str | Path,
    *,
    language: str,
    pattern: str,
    debug_query: DebugQuery = "pattern",
    timeout_seconds: int = 60,
    executable: str | None = None,
) -> str:
    """Return ast-grep's native debug tree for one raw pattern."""

    root = _validate_root(target_dir)
    _validate_language(language)
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolInputError("pattern must be a non-empty string")
    validate_text_argument(pattern, "pattern")
    if not isinstance(debug_query, str) or debug_query not in {"pattern", "ast", "cst", "sexp"}:
        raise ToolInputError(f"unsupported debug-query format: {debug_query!r}")
    _validate_timeout(timeout_seconds)
    binary = _resolve_executable(executable)
    completed = _run(
        [
            binary,
            "run",
            f"--pattern={pattern}",
            f"--lang={language}",
            "--json=compact",
            f"--debug-query={debug_query}",
            ".",
        ],
        root=root,
        timeout_seconds=timeout_seconds,
    )
    stdout, stderr = _raise_for_execution_error(completed)
    return stderr or stdout


__all__ = ["debug_pattern", "run_query"]
