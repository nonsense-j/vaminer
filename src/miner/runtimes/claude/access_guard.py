"""Fail-closed PreToolUse guard for Claude's native filesystem tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal

_READ_PATH_FIELDS = {
    "Read": "file_path",
    "Grep": "path",
    "Glob": "path",
}
_WRITE_PATH_FIELD = "file_path"
GuardRole = Literal["default", "rule-generator"]


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
    restricted_roots: tuple[Path, ...],
    role: GuardRole,
) -> str | None:
    field = _READ_PATH_FIELDS[tool_name]
    value = tool_input.get(field)
    if not isinstance(value, str) or not value.strip():
        return f"{tool_name} must provide an explicit {field} under an allowed read root"
    try:
        path = _resolve(value, cwd=cwd)
    except OSError:
        return f"{tool_name} path could not be resolved"
    if not _inside(path, roots):
        if role == "rule-generator":
            restricted_root = next(
                (root for root in restricted_roots if _inside(path, (root,))),
                None,
            )
            denied_location = (
                f"the source_root folder ({restricted_root})"
                if restricted_root is not None
                else str(path)
            )
            cases_location = ", ".join(str(root) for root in roots) or "the cases folder"
            return (
                f"You are not allowed to access {denied_location} as the Rule Generator. "
                "This location is unnecessary for rule generation. Use the authoritative "
                "RCA directly, then locate and read only the RCA-declared case files under "
                f"{cases_location}. Generate the complete queryless plan and pass that plan "
                "to mcp__vaminer__synthesize_ast_grep_anchors for source-grounded queries."
            )
        allowed = ", ".join(str(root) for root in roots) or "<none>"
        return f"{tool_name} is limited to the configured read roots: {allowed}"
    if tool_name == "Read" and path.is_dir():
        if role == "rule-generator":
            return (
                f"Read accepts a file, not the directory {path}. Use Glob under the cases "
                "folder to locate the RCA-declared case files, then Read those files directly."
            )
        return (
            f"Read accepts a file, not the directory {path}. "
            "Use Glob to list files before reading one."
        )
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


def validate_event(
    event: Any,
    *,
    roots: tuple[Path, ...],
    restricted_roots: tuple[Path, ...] = (),
    write_root: Path | None = None,
    role: GuardRole = "default",
) -> str | None:
    """Return a denial reason, or ``None`` when the filesystem call is in scope."""

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
            restricted_roots=restricted_roots,
            role=role,
        )
    if tool_name == "Write":
        return _validate_write(
            tool_input,
            cwd=cwd,
            write_root=write_root,
        )
    return "This tool is outside the VAMiner filesystem access guard"


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
        parser.add_argument("--restricted-root", action="append", default=[])
        parser.add_argument("--write-root")
        parser.add_argument(
            "--role",
            choices=("default", "rule-generator"),
            default="default",
        )
        args = parser.parse_args(argv)
        event = json.load(sys.stdin)
        roots = tuple(Path(value).expanduser().resolve() for value in args.root)
        restricted_roots = tuple(
            Path(value).expanduser().resolve()
            for value in args.restricted_root
        )
        write_root = (
            Path(args.write_root).expanduser().resolve()
            if args.write_root
            else None
        )
        reason = validate_event(
            event,
            roots=roots,
            restricted_roots=restricted_roots,
            write_root=write_root,
            role=args.role,
        )
    except Exception:  # noqa: BLE001 - an access guard must fail closed.
        reason = "The VAMiner access guard could not validate this tool call"
    if reason is not None:
        _deny(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
