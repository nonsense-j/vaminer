"""Fail-closed PreToolUse guard for Claude filesystem and Bash access."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

_READ_PATH_FIELDS = {
    "Read": "file_path",
    "Grep": "path",
    "Glob": "path",
}
_WRITE_PATH_FIELD = "file_path"
_SHELL_OPERATORS = frozenset(
    {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "(", ")", "`"}
)
_SHELL_SUBSTITUTIONS = ("$(", "`", "<(", ">(")
_RUNNER_FLAGS = frozenset(
    {
        "--language",
        "--query-type",
        "--query",
        "--output",
        "--sample-size",
        "--timeout-seconds",
    }
)
_PLUGIN_RUNNER = "${CLAUDE_PLUGIN_ROOT}/skills/ast-grep/scripts/runner.py"


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _resolve(value: str, *, cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _validate_read(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    cwd: Path,
    roots: tuple[Path, ...],
) -> str | None:
    field = _READ_PATH_FIELDS[tool_name]
    value = tool_input.get(field)
    if not isinstance(value, str) or not value.strip():
        return f"{tool_name} must provide an explicit {field} under workspace_root"
    try:
        path = _resolve(value, cwd=cwd)
    except OSError:
        return f"{tool_name} path could not be resolved"
    if not _inside(path, roots):
        return f"{tool_name} is limited to workspace_root"
    return None


def _validate_write(
    tool_input: dict[str, Any],
    *,
    cwd: Path,
    write_root: Path | None,
) -> str | None:
    value = tool_input.get(_WRITE_PATH_FIELD)
    if not isinstance(value, str) or not value.strip():
        return "Write must provide an explicit file_path directly under cases_dir"
    if write_root is None:
        return "Write is not configured for this task"
    try:
        path = _resolve(value, cwd=cwd)
    except OSError:
        return "Write path could not be resolved"
    if path.parent != write_root:
        return "Write is limited to top-level files directly under cases_dir"
    return None


def _tokenize_command(command: str) -> list[str] | None:
    if any(marker in command for marker in _SHELL_SUBSTITUTIONS):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>()`")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(token in _SHELL_OPERATORS for token in tokens):
        return None
    return tokens


def _validate_runner_arguments(tokens: list[str]) -> bool:
    if len(tokens) < 9:
        return False
    remaining = tokens[3:]
    seen: set[str] = set()
    index = 0
    while index < len(remaining):
        flag = remaining[index]
        if flag not in _RUNNER_FLAGS or flag in seen or index + 1 >= len(remaining):
            return False
        seen.add(flag)
        index += 2
    return {"--language", "--query-type", "--query"} <= seen


def _validate_bash(
    event: dict[str, Any],
    *,
    cwd: Path,
    roots: tuple[Path, ...],
    python: Path | None,
    runner: Path | None,
) -> str | None:
    if event.get("agent_type") not in {
        "ast-grep-synthesizer",
        "vaminer:ast-grep-synthesizer",
    }:
        return "Bash is available only to the ast-grep synthesizer"
    if python is None or runner is None:
        return "Bash is not configured for this task"

    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return "Bash must invoke the configured ast-grep runner"
    tokens = _tokenize_command(command)
    if tokens is None or not _validate_runner_arguments(tokens):
        return "Bash must contain one direct ast-grep runner invocation"

    try:
        executable = _resolve(tokens[0], cwd=cwd)
        target = _resolve(tokens[2], cwd=cwd)
    except OSError:
        return "Bash runner paths could not be resolved"
    if executable != python:
        return "Bash must use the configured Python executable"
    if tokens[1] != _PLUGIN_RUNNER:
        try:
            invoked_runner = _resolve(tokens[1], cwd=cwd)
        except OSError:
            return "Bash must use the packaged ast-grep runner"
        if invoked_runner != runner:
            return "Bash must use the packaged ast-grep runner"
    if not _inside(target, roots):
        return "The ast-grep runner target must be under source_root or cases_dir"
    return None


def validate_event(
    event: Any,
    *,
    roots: tuple[Path, ...],
    write_root: Path | None = None,
    python: Path | None = None,
    runner: Path | None = None,
) -> str | None:
    """Return a denial reason, or ``None`` when the tool call is in scope."""

    if not isinstance(event, dict):
        return "Malformed Claude tool event"
    tool_name = event.get("tool_name")
    cwd_value = event.get("cwd")
    tool_input = event.get("tool_input")
    if not isinstance(cwd_value, str) or not isinstance(tool_input, dict):
        return "Malformed Claude tool event"
    try:
        cwd = Path(cwd_value).expanduser().resolve()
    except OSError:
        return "Claude working directory could not be resolved"

    if tool_name in _READ_PATH_FIELDS:
        return _validate_read(
            tool_name,
            tool_input,
            cwd=cwd,
            roots=roots,
        )
    if tool_name == "Write":
        return _validate_write(
            tool_input,
            cwd=cwd,
            write_root=write_root,
        )
    if tool_name == "Bash":
        return _validate_bash(
            event,
            cwd=cwd,
            roots=roots,
            python=python,
            runner=runner,
        )
    return "This tool is outside the VAMiner access guard"


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--root", action="append", default=[])
        parser.add_argument("--write-root")
        parser.add_argument("--python")
        parser.add_argument("--runner")
        args = parser.parse_args(argv)
        event = json.load(sys.stdin)
        roots = tuple(Path(value).expanduser().resolve() for value in args.root)
        write_root = (
            Path(args.write_root).expanduser().resolve()
            if args.write_root
            else None
        )
        python = Path(args.python).expanduser().resolve() if args.python else None
        runner = Path(args.runner).expanduser().resolve() if args.runner else None
        reason = validate_event(
            event,
            roots=roots,
            write_root=write_root,
            python=python,
            runner=runner,
        )
    except Exception:  # noqa: BLE001 - an access guard must fail closed.
        reason = "The VAMiner access guard could not validate this tool call"
    if reason is not None:
        _deny(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
