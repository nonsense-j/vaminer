#!/usr/bin/env python3
"""Bounded ast-grep JSON runner for typed agent tools and the skill CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

QueryType = Literal["pattern", "rule"]
OutputMode = Literal["count", "sample", "full"]
_ALLOWED_ROOTS_ENV = "VAMINER_AST_GREP_ALLOWED_ROOTS"


class AstGrepRunnerError(RuntimeError):
    """Raised when ast-grep cannot produce a trustworthy JSON result."""


def _enforce_allowed_root(root: Path) -> None:
    raw = os.getenv(_ALLOWED_ROOTS_ENV)
    if raw is None:
        return
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AstGrepRunnerError("configured ast-grep roots are invalid") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise AstGrepRunnerError("configured ast-grep roots are invalid")
    allowed = tuple(Path(item).expanduser().resolve() for item in decoded)
    if not any(root == candidate or candidate in root.parents for candidate in allowed):
        raise AstGrepRunnerError("target directory is outside the configured source and cases roots")


def _find_ast_grep(executable: str | None = None) -> str:
    if executable:
        return executable
    for name in ("ast-grep", "sg"):
        binary = shutil.which(name)
        if binary:
            return binary
    raise AstGrepRunnerError("ast-grep is required but was not found on PATH")


def _has_top_level_key(query: str, key: str) -> bool:
    return any(line.startswith(f"{key}:") for line in query.splitlines())


def _make_inline_rule(query: str, language: str) -> str:
    prefix = []
    if not _has_top_level_key(query, "id"):
        prefix.append("id: agent-query")
    if not _has_top_level_key(query, "language"):
        prefix.append(f"language: {language}")
    if _has_top_level_key(query, "rule"):
        return "\n".join([*prefix, query]) if prefix else query
    indented = "\n".join(f"  {line}" if line else "" for line in query.splitlines())
    return "\n".join([*prefix, "rule:", indented])


def _parse_output(stdout: str) -> list[dict[str, Any]]:
    if not stdout.strip():
        return []
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AstGrepRunnerError(f"ast-grep returned invalid JSON: {exc}") from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AstGrepRunnerError("ast-grep returned an unexpected JSON shape")
    return value


def _relative_file(root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise AstGrepRunnerError("ast-grep returned a match without a file")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise AstGrepRunnerError(f"ast-grep returned a file outside the target directory: {value}") from exc


def _coordinate(value: Any, *, one_based: bool) -> int:
    if not isinstance(value, int):
        return 1 if one_based else 0
    return value + 1 if one_based else value


def _normalize_site(root: Path, raw: dict[str, Any], *, include_metavariables: bool) -> dict[str, Any]:
    range_info = raw.get("range") if isinstance(raw.get("range"), dict) else {}
    start = range_info.get("start") if isinstance(range_info.get("start"), dict) else {}
    end = range_info.get("end") if isinstance(range_info.get("end"), dict) else {}
    site: dict[str, Any] = {
        "file": _relative_file(root, raw.get("file")),
        "text": str(raw.get("text") or raw.get("lines") or ""),
        "start": {
            "line": _coordinate(start.get("line"), one_based=True),
            "column": _coordinate(start.get("column"), one_based=True),
        },
        "end": {
            "line": _coordinate(end.get("line"), one_based=True),
            "column": _coordinate(end.get("column"), one_based=True),
        },
    }
    if include_metavariables:
        site["meta_variables"] = raw.get("metaVariables") or {}
    return site


def run_ast_grep(
    target_dir: str | Path,
    *,
    language: str,
    query_type: QueryType,
    query: str,
    output: OutputMode = "sample",
    sample_size: int = 20,
    timeout_seconds: int = 60,
    executable: str | None = None,
) -> dict[str, Any]:
    """Run one ast-grep query against a directory and return normalized JSON data."""
    root = Path(target_dir).resolve()
    if not root.is_dir():
        raise AstGrepRunnerError(f"target directory does not exist: {root}")
    _enforce_allowed_root(root)
    if query_type not in {"pattern", "rule"}:
        raise AstGrepRunnerError(f"unsupported query type: {query_type!r}")
    if output not in {"count", "sample", "full"}:
        raise AstGrepRunnerError(f"unsupported output mode: {output!r}")
    if sample_size < 1:
        raise AstGrepRunnerError("sample_size must be positive")

    binary = _find_ast_grep(executable)
    if query_type == "pattern":
        command = [
            binary,
            "run",
            "--pattern",
            query,
            "--lang",
            language,
            "--json=compact",
            ".",
        ]
    else:
        command = [
            binary,
            "scan",
            "--inline-rules",
            _make_inline_rule(query, language),
            "--json=compact",
            ".",
        ]

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AstGrepRunnerError(f"ast-grep timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise AstGrepRunnerError(f"ast-grep could not start: {exc}") from exc

    if completed.returncode not in {0, 1}:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise AstGrepRunnerError(f"ast-grep query failed: {detail}")

    raw_matches = _parse_output(completed.stdout)
    include_metavariables = output == "full"
    matches = [_normalize_site(root, raw, include_metavariables=include_metavariables) for raw in raw_matches]
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

    result: dict[str, Any] = {
        "target_dir": root.as_posix(),
        "output": output,
        "match_count": len(matches),
        "matched_file_count": len({site["file"] for site in matches}),
    }
    if output == "sample":
        result["matches"] = matches[:sample_size]
        result["truncated"] = len(matches) > sample_size
    elif output == "full":
        result["matches"] = matches
        result["truncated"] = False
    if completed.stderr.strip():
        result["warnings"] = completed.stderr.strip()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_dir")
    parser.add_argument("--language", required=True)
    parser.add_argument("--query-type", choices=("pattern", "rule"), required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", choices=("count", "sample", "full"), default="sample")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()
    print(
        json.dumps(
            run_ast_grep(
                args.target_dir,
                language=args.language,
                query_type=args.query_type,
                query=args.query,
                output=args.output,
                sample_size=args.sample_size,
                timeout_seconds=args.timeout_seconds,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
