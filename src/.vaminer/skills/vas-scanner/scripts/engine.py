"""Shared deterministic anchor execution and candidate ranking."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AnchorScanError(RuntimeError):
    """Raised when deterministic anchor scanning cannot safely continue."""


class AnchorExecutionError(AnchorScanError):
    """Raised when the scanner binary, process, or result protocol fails."""


class AnchorQueryError(AnchorScanError):
    """Raised when ast-grep rejects model-authored query syntax or semantics."""


QUERY_ERROR_MARKERS = (
    "pattern contains an error node",
    "cannot parse rule",
    "not a valid ast-grep rule",
    "fail to parse yaml as ruleconfig",
    "failed to parse pattern",
    "invalid pattern",
)
AST_GREP_CLI_REQUIREMENT = "ast-grep-cli==0.45.3"
AST_GREP_INSTALL_TIMEOUT_SECONDS = 300
AST_GREP_INSTALL_DIAGNOSTIC_CHARS = 4_000


@dataclass(frozen=True)
class AnchorMatch:
    anchor_id: str
    query_weight: int
    behavior: str
    inspect_hint: str
    file: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class AnchorRunResult:
    anchor: dict[str, Any]
    matches: list[AnchorMatch]

    @property
    def anchor_id(self) -> str:
        return str(self.anchor["id"])

    @property
    def query_weight(self) -> int:
        return int(self.anchor["query_weight"])


@dataclass(frozen=True)
class AnchorScanResult:
    root: Path
    anchor_results: list[AnchorRunResult]

    @property
    def matches(self) -> list[AnchorMatch]:
        return [match for result in self.anchor_results for match in result.matches]

    def candidates(self, *, min_anchor_weight: int = 1) -> list[dict[str, Any]]:
        if not 1 <= min_anchor_weight <= 5:
            raise ValueError("min_anchor_weight must be between 1 and 5")

        files: dict[str, dict[str, dict[str, Any]]] = {}
        for result in self.anchor_results:
            for match in result.matches:
                hints = files.setdefault(match.file, {})
                hint = hints.setdefault(
                    match.anchor_id,
                    {
                        "anchor_id": match.anchor_id,
                        "query_weight": match.query_weight,
                        "behavior": match.behavior,
                        "inspect_hint": match.inspect_hint,
                        "locations": [],
                    },
                )
                location = {"start_line": match.start_line, "end_line": match.end_line}
                if location not in hint["locations"]:
                    hint["locations"].append(location)

        candidates = []
        for file, hints_by_id in files.items():
            anchor_hints = list(hints_by_id.values())
            if not any(
                hint["query_weight"] >= min_anchor_weight
                for hint in anchor_hints
            ):
                continue
            for hint in anchor_hints:
                hint["locations"].sort(
                    key=lambda item: (item["start_line"], item["end_line"])
                )
            anchor_hints.sort(
                key=lambda item: (-item["query_weight"], item["anchor_id"])
            )
            candidates.append(
                {
                    "file": file,
                    "priority_score": sum(
                        hint["query_weight"] for hint in anchor_hints
                    ),
                    "max_anchor_weight": max(
                        hint["query_weight"] for hint in anchor_hints
                    ),
                    "distinct_anchor_count": len(anchor_hints),
                    "raw_match_count": sum(
                        len(hint["locations"]) for hint in anchor_hints
                    ),
                    "anchor_hints": anchor_hints,
                }
            )

        candidates.sort(
            key=lambda item: (
                -item["priority_score"],
                -item["max_anchor_weight"],
                -item["distinct_anchor_count"],
                -item["raw_match_count"],
                item["file"],
            )
        )
        return candidates


def sorted_anchors(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(anchor) for anchor in anchors),
        key=lambda anchor: (-int(anchor["query_weight"]), str(anchor["id"])),
    )


def _run_version_probe(binary: str) -> None:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnchorExecutionError(f"configured ast-grep executable could not run: {binary}: {exc}") from exc
    version = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0 or not version.casefold().startswith("ast-grep "):
        detail = version[:AST_GREP_INSTALL_DIAGNOSTIC_CHARS] or f"exit code {completed.returncode}"
        raise AnchorExecutionError(f"configured executable is not ast-grep: {binary}: {detail}")


def find_ast_grep(executable: str = "ast-grep") -> str:
    binary = shutil.which(executable)
    if binary is None:
        raise AnchorExecutionError(f"configured ast-grep executable was not found: {executable}")
    if sys.platform == "win32" and Path(binary).suffix.casefold() != ".exe":
        raise AnchorExecutionError(f"configured ast-grep executable is not a native .exe: {binary}")
    _run_version_probe(binary)
    return binary


def _managed_ast_grep_candidates(install_dir: Path) -> tuple[Path, ...]:
    filename = "ast-grep.exe" if sys.platform == "win32" else "ast-grep"
    return (
        install_dir / "bin" / filename,
        install_dir / "Scripts" / filename,
        install_dir / filename,
    )


def _installed_ast_grep(install_dir: Path) -> str | None:
    for candidate in _managed_ast_grep_candidates(install_dir):
        if candidate.is_file():
            return find_ast_grep(str(candidate))
    return None


def _pip_install_command(prefix: list[str], install_dir: Path) -> list[str]:
    return [
        *prefix,
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--upgrade",
        "--target",
        str(install_dir),
        AST_GREP_CLI_REQUIREMENT,
    ]


def install_ast_grep(install_dir: Path) -> str:
    install_dir.mkdir(parents=True, exist_ok=True)
    prefixes = [[sys.executable, "-m", "pip"]]
    if pip := shutil.which("pip"):
        prefixes.append([pip])
    diagnostics: list[str] = []
    for prefix in prefixes:
        command = _pip_install_command(prefix, install_dir)
        try:
            completed = subprocess.run(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=AST_GREP_INSTALL_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostics.append(f"{' '.join(prefix)}: {exc}")
            continue
        if completed.returncode == 0:
            binary = _installed_ast_grep(install_dir)
            if binary is not None:
                return binary
        detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        diagnostics.append(f"{' '.join(prefix)}: {detail or f'exit code {completed.returncode}'}")
    diagnostic = "\n".join(diagnostics)[:AST_GREP_INSTALL_DIAGNOSTIC_CHARS]
    raise AnchorExecutionError(
        f"could not install {AST_GREP_CLI_REQUIREMENT} into {install_dir}: {diagnostic}"
    )


def resolve_ast_grep(
    configured: str | None,
    install_dir: Path,
    *,
    install: bool,
) -> str:
    if configured is not None:
        return find_ast_grep(configured)
    binary = _installed_ast_grep(install_dir)
    if binary is not None:
        return binary
    if install:
        return install_ast_grep(install_dir)
    raise AnchorExecutionError(
        f"managed ast-grep is not installed under {install_dir}; run scanner preflight first"
    )


def has_top_level_key(query: str, key: str) -> bool:
    return any(line.startswith(f"{key}:") for line in query.splitlines())


def make_rule(query: str, language: str, rule_id: str) -> str:
    prefix = []
    if not has_top_level_key(query, "id"):
        prefix.append(f"id: {rule_id}")
    if not has_top_level_key(query, "language"):
        prefix.append(f"language: {language}")
    if has_top_level_key(query, "rule"):
        return "\n".join(prefix + [query]) if prefix else query
    indented = "\n".join(f"  {line}" if line else "" for line in query.splitlines())
    return "\n".join(prefix + ["rule:", indented])


def parse_ast_grep_output(stdout: str, anchor_id: str) -> list[dict[str, Any]]:
    if not stdout.strip():
        return []
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AnchorExecutionError(
            f"anchor {anchor_id!r} returned invalid JSON: {exc}"
        ) from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AnchorExecutionError(f"anchor {anchor_id!r} returned an unexpected JSON shape")
    return value


def relative_source_path(root: Path, file_value: Any, anchor_id: str) -> str:
    if not isinstance(file_value, str) or not file_value:
        raise AnchorExecutionError(f"anchor {anchor_id!r} returned a match without a file")
    file_path = Path(file_value)
    if not file_path.is_absolute():
        file_path = root / file_path
    resolved = file_path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise AnchorExecutionError(
            f"anchor {anchor_id!r} returned a file outside the scan root: {file_value}"
        ) from exc
    return relative.as_posix()


def run_ast_grep(command: list[str], *, timeout_seconds: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnchorExecutionError(f"ast-grep timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise AnchorExecutionError(f"ast-grep could not start: {exc}") from exc


def captured_output(
    completed: subprocess.CompletedProcess[str],
    anchor_id: str,
) -> tuple[str, str]:
    """Return captured text or a scanner error that identifies the affected anchor."""

    streams = {"stdout": completed.stdout, "stderr": completed.stderr}
    missing = [name for name, value in streams.items() if value is None]
    if missing:
        labels = " and ".join(missing)
        raise AnchorExecutionError(
            f"anchor {anchor_id!r} did not receive captured {labels} from ast-grep "
            f"(exit code {completed.returncode})"
        )

    normalized: dict[str, str] = {}
    for name, value in streams.items():
        if isinstance(value, (str, bytes)):
            text = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
            normalized[name] = text.replace("\r\n", "\n").replace("\r", "\n")
        else:
            raise AnchorExecutionError(
                f"anchor {anchor_id!r} received an invalid ast-grep {name} value "
                f"(exit code {completed.returncode})"
            )
    return normalized["stdout"], normalized["stderr"]


def run_anchor(
    anchor: dict[str, Any],
    root: Path,
    language: str,
    ast_grep: str | None,
) -> AnchorRunResult:
    if not str(anchor["query"]).strip():
        return AnchorRunResult(anchor=dict(anchor), matches=[])
    if ast_grep is None:
        raise AnchorExecutionError("ast-grep is required for enabled anchors")

    query_type = anchor["type"]
    if query_type == "pattern":
        command = [
            ast_grep,
            "run",
            "--pattern",
            anchor["query"],
            "--lang",
            language,
            "--json=compact",
            str(root),
        ]
        completed = run_ast_grep(command)
    elif query_type == "rule":
        with tempfile.TemporaryDirectory(prefix="vaminer-ast-grep-") as temp_dir:
            rule_file = Path(temp_dir) / "rule.yml"
            rule_file.write_text(
                make_rule(anchor["query"], language, anchor["id"]),
                encoding="utf-8",
            )
            command = [
                ast_grep,
                "scan",
                "--rule",
                str(rule_file),
                "--json=compact",
                str(root),
            ]
            completed = run_ast_grep(command)
    else:
        raise AnchorQueryError(f"unsupported anchor type: {query_type!r}")

    stdout, stderr = captured_output(completed, anchor["id"])
    if completed.returncode not in {0, 1} or stderr.strip():
        detail = (
            stderr.strip()
            or stdout.strip()
            or f"exit code {completed.returncode}"
        )
        error_type = (
            AnchorQueryError
            if any(marker in detail.lower() for marker in QUERY_ERROR_MARKERS)
            else AnchorExecutionError
        )
        raise error_type(f"anchor {anchor['id']!r} failed: {detail}")

    matches = []
    for raw in parse_ast_grep_output(stdout, anchor["id"]):
        range_info = raw.get("range") or {}
        start = range_info.get("start") or {}
        end = range_info.get("end") or {}
        start_line = start.get("line")
        end_line = end.get("line")
        matches.append(
            AnchorMatch(
                anchor_id=anchor["id"],
                query_weight=anchor["query_weight"],
                behavior=anchor["behavior"],
                inspect_hint=anchor["inspect_hint"],
                file=relative_source_path(root, raw.get("file"), anchor["id"]),
                start_line=start_line + 1 if isinstance(start_line, int) else 1,
                end_line=(
                    end_line + 1
                    if isinstance(end_line, int)
                    else (start_line + 1 if isinstance(start_line, int) else 1)
                ),
            )
        )
    return AnchorRunResult(anchor=dict(anchor), matches=matches)


def scan_anchors(
    anchors: list[dict[str, Any]],
    root: Path,
    language: str,
    *,
    ast_grep: str | None = None,
) -> AnchorScanResult:
    root = root.resolve()
    if not root.is_dir():
        raise AnchorExecutionError(f"scan root does not exist: {root}")
    ordered = sorted_anchors(anchors)
    binary = (
        find_ast_grep(ast_grep or "ast-grep")
        if any(str(anchor["query"]).strip() for anchor in ordered)
        else None
    )
    results = [
        run_anchor(anchor, root, language, binary) for anchor in ordered
    ]
    return AnchorScanResult(root=root, anchor_results=results)
