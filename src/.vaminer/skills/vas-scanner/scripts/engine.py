"""Shared deterministic anchor execution and candidate ranking."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AnchorScanError(RuntimeError):
    """Raised when deterministic anchor scanning cannot safely continue."""


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


def find_ast_grep(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for name in ("ast-grep", "sg"):
        binary = shutil.which(name)
        if binary:
            return binary
    raise AnchorScanError("ast-grep is required but was not found on PATH")


def has_top_level_key(query: str, key: str) -> bool:
    return any(line.startswith(f"{key}:") for line in query.splitlines())


def make_inline_rule(query: str, language: str, rule_id: str) -> str:
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
        raise AnchorScanError(
            f"anchor {anchor_id!r} returned invalid JSON: {exc}"
        ) from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AnchorScanError(f"anchor {anchor_id!r} returned an unexpected JSON shape")
    return value


def relative_source_path(root: Path, file_value: Any, anchor_id: str) -> str:
    if not isinstance(file_value, str) or not file_value:
        raise AnchorScanError(f"anchor {anchor_id!r} returned a match without a file")
    file_path = Path(file_value)
    if not file_path.is_absolute():
        file_path = root / file_path
    resolved = file_path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise AnchorScanError(
            f"anchor {anchor_id!r} returned a file outside the scan root: {file_value}"
        ) from exc
    return relative.as_posix()


def run_anchor(
    anchor: dict[str, Any],
    root: Path,
    language: str,
    ast_grep: str,
) -> AnchorRunResult:
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
    elif query_type == "rule":
        command = [
            ast_grep,
            "scan",
            "--inline-rules",
            make_inline_rule(anchor["query"], language, anchor["id"]),
            "--json=compact",
            str(root),
        ]
    else:
        raise ValueError(f"unsupported anchor type: {query_type!r}")

    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode not in {0, 1} or completed.stderr.strip():
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"exit code {completed.returncode}"
        )
        raise AnchorScanError(f"anchor {anchor['id']!r} failed: {detail}")

    matches = []
    for raw in parse_ast_grep_output(completed.stdout, anchor["id"]):
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
        raise FileNotFoundError(f"scan root does not exist: {root}")
    binary = find_ast_grep(ast_grep)
    results = [
        run_anchor(anchor, root, language, binary) for anchor in sorted_anchors(anchors)
    ]
    return AnchorScanResult(root=root, anchor_results=results)
