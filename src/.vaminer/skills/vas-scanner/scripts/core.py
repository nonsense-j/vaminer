"""Deterministic rule scanning and scan-state management for vas-scanner."""

from __future__ import annotations

from datetime import datetime
import json
import re
import shutil
from pathlib import Path
from typing import Any

from config import (
    CANDIDATE_BATCH_SIZE,
    HOTSPOT_CONTEXT_LINES_PER_SIDE,
    MAX_CANDIDATE_ATTEMPTS,
    MIN_ANCHOR_WEIGHT,
    WORKSPACE_DIR,
)
from engine import AnchorScanError as ScanError
from engine import scan_anchors


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
RULES_DIR = SKILL_DIR / "rules"
ANALYSIS_REFERENCE = SKILL_DIR / "references" / "file-analysis.md"
VAS_ID_RE = re.compile(r"^VAS-[0-9]+$")
CONFIDENCE_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def require_string(data: dict[str, Any], key: str, source: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string: {source}")
    return value.strip()


def require_string_list(data: dict[str, Any], key: str, source: Path) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings: {source}")
    return [item.strip() for item in value]


def validate_vas_id(vas_id: str) -> str:
    if not VAS_ID_RE.fullmatch(vas_id):
        raise ValueError(f"invalid VAS id: {vas_id!r}")
    return vas_id


def load_rule(vas_id: str, rules_dir: Path = RULES_DIR) -> dict[str, Any]:
    validate_vas_id(vas_id)
    source = rules_dir / f"{vas_id}.json"
    if not source.is_file():
        raise FileNotFoundError(f"VAS rule not found: {source}")
    raw = read_json(source)
    if not isinstance(raw, dict):
        raise ValueError(f"rule root must be an object: {source}")
    if "search_profile" in raw:
        raise ValueError(f"legacy search_profile is not supported: {source}")
    if "criteria" in raw:
        raise ValueError(f"legacy criteria is not supported: {source}")
    declared_id = require_string(raw, "vas_id", source)
    if declared_id != vas_id:
        raise ValueError(f"rule declares {declared_id!r}, expected {vas_id!r}: {source}")

    scenarios = raw.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError(f"scenarios must be an object: {source}")
    unexpected_scenario_fields = set(scenarios) - {"unsafe", "safe"}
    if unexpected_scenario_fields:
        fields = ", ".join(sorted(unexpected_scenario_fields))
        raise ValueError(f"unsupported scenario fields ({fields}): {source}")
    normalized_scenarios = {
        "unsafe": require_string_list(scenarios, "unsafe", source),
        "safe": require_string_list(scenarios, "safe", source),
    }
    if not normalized_scenarios["unsafe"]:
        raise ValueError(f"scenarios.unsafe must not be empty: {source}")

    anchors = raw.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError(f"anchors must be a non-empty list: {source}")

    normalized_anchors = []
    anchor_ids = set()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError(f"every anchor must be an object: {source}")
        unexpected = set(anchor) - {
            "id",
            "behavior_weight",
            "query_weight",
            "type",
            "query",
            "behavior",
            "inspect_hint",
        }
        if unexpected:
            fields = ", ".join(sorted(unexpected))
            raise ValueError(f"unsupported anchor fields ({fields}): {source}")
        anchor_id = require_string(anchor, "id", source)
        if anchor_id in anchor_ids:
            raise ValueError(f"duplicate anchor id {anchor_id!r}: {source}")
        anchor_ids.add(anchor_id)
        query_type = require_string(anchor, "type", source)
        if query_type not in {"pattern", "rule"}:
            raise ValueError(f"unsupported anchor type {query_type!r}: {source}")
        behavior_weight = anchor.get("behavior_weight")
        if (
            not isinstance(behavior_weight, int)
            or isinstance(behavior_weight, bool)
            or not 1 <= behavior_weight <= 5
        ):
            raise ValueError(
                f"anchor.behavior_weight must be an integer from 1 to 5: {source}"
            )
        query_weight = anchor.get("query_weight")
        if (
            not isinstance(query_weight, int)
            or isinstance(query_weight, bool)
            or not 1 <= query_weight <= behavior_weight
        ):
            raise ValueError(
                "anchor.query_weight must be an integer from 1 to behavior_weight: "
                f"{source}"
            )
        query = anchor.get("query")
        if not isinstance(query, str):
            raise ValueError(f"anchor.query must be a string: {source}")
        normalized_anchors.append(
            {
                "id": anchor_id,
                "behavior_weight": behavior_weight,
                "query_weight": query_weight,
                "type": query_type,
                "query": query,
                "behavior": require_string(anchor, "behavior", source),
                "inspect_hint": require_string(anchor, "inspect_hint", source),
            }
        )

    return {
        "vas_id": vas_id,
        "category": require_string(raw, "category", source),
        "language": require_string(raw, "language", source),
        "summary": require_string(raw, "summary", source),
        "scenarios": normalized_scenarios,
        "anchors": normalized_anchors,
    }


def discover_candidates(
    rule: dict[str, Any],
    repo_path: Path,
    min_anchor_weight: int,
    ast_grep: str | None = None,
) -> list[dict[str, Any]]:
    scan = scan_anchors(
        rule["anchors"],
        repo_path,
        rule["language"],
        ast_grep=ast_grep,
    )
    return scan.candidates(min_anchor_weight=min_anchor_weight)


def safe_hotspot_name(rank: int, file: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", file).strip("-").lower() or "candidate"
    return f"{rank:04d}-{stem[:100]}.md"


def merged_code_regions(hints: list[dict[str, Any]], line_count: int, context_lines: int) -> list[tuple[int, int]]:
    ranges = []
    for hint in hints:
        for location in hint["locations"]:
            ranges.append(
                (
                    max(1, location["start_line"] - context_lines),
                    min(line_count, location["end_line"] + context_lines),
                )
            )
    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def render_hotspot(
    repo_path: Path,
    rule: dict[str, Any],
    rank: int,
    candidate: dict[str, Any],
    context_lines: int,
) -> str:
    file_path = repo_path / candidate["file"]
    if not file_path.is_file():
        raise FileNotFoundError(f"candidate source file not found: {file_path}")
    source = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    hints = candidate["anchor_hints"]
    lines = [
        f"# Candidate {rank}: `{candidate['file']}`",
        "",
        f"- Priority score: {candidate['priority_score']}",
        f"- Matched anchors: {len(hints)}",
        "",
        "## Anchor Hints",
        "",
    ]
    for hint in hints:
        locations = ", ".join(
            str(item["start_line"])
            if item["start_line"] == item["end_line"]
            else f"{item['start_line']}-{item['end_line']}"
            for item in hint["locations"]
        )
        lines.extend(
            [
                f"### `{hint['anchor_id']}`",
                "",
                f"- Query weight: {hint['query_weight']}",
                f"- Locations: {locations}",
                f"- Behavior: {hint['behavior']}",
                f"- Inspect hint: {hint['inspect_hint']}",
                "",
            ]
        )

    lines.extend(["## Code Regions", ""])
    matched_ranges = [
        (item["start_line"], item["end_line"])
        for hint in hints
        for item in hint["locations"]
    ]
    language = re.sub(r"[^A-Za-z0-9_+-]", "", rule["language"]) or "text"
    for start, end in merged_code_regions(hints, len(source), context_lines):
        lines.extend([f"### Lines {start}-{end}", "", f"````{language}"])
        width = len(str(end))
        for line_no in range(start, end + 1):
            marker = ">>" if any(first <= line_no <= last for first, last in matched_ranges) else "  "
            lines.append(f"{marker} {line_no:>{width}} | {source[line_no - 1]}")
        lines.extend(["````", ""])
    return "\n".join(lines).rstrip() + "\n"


def next_run_dir(workspace_root: Path, vas_id: str) -> tuple[Path, Path]:
    parent = workspace_root / vas_id
    parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    destination = parent / run_id
    temporary = parent / f".{run_id}.tmp"
    return temporary, destination


def prepare_scan(
    vas_id: str,
    repo_path: Path,
    *,
    rules_dir: Path = RULES_DIR,
    workspace_dir: Path | str | None = WORKSPACE_DIR,
    min_anchor_weight: int = MIN_ANCHOR_WEIGHT,
    context_lines: int = HOTSPOT_CONTEXT_LINES_PER_SIDE,
    batch_size: int = CANDIDATE_BATCH_SIZE,
    max_attempts: int = MAX_CANDIDATE_ATTEMPTS,
    ast_grep: str | None = None,
) -> Path:
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise FileNotFoundError(f"repository does not exist: {repo_path}")
    if context_lines < 0:
        raise ValueError("HOTSPOT_CONTEXT_LINES_PER_SIDE must not be negative")
    if batch_size < 1:
        raise ValueError("CANDIDATE_BATCH_SIZE must be at least 1")
    if max_attempts < 1:
        raise ValueError("MAX_CANDIDATE_ATTEMPTS must be at least 1")

    rule = load_rule(vas_id, rules_dir)
    candidates = discover_candidates(rule, repo_path, min_anchor_weight, ast_grep)
    workspace_root = Path(workspace_dir).expanduser().resolve() if workspace_dir else repo_path / ".vas"
    temporary, destination = next_run_dir(workspace_root, vas_id)
    if temporary.exists() or destination.exists():
        raise FileExistsError(f"scan directory already exists: {destination}")

    try:
        hotspot_dir = temporary / "hotspots"
        hotspot_dir.mkdir(parents=True)
        scan_candidates = []
        for rank, candidate in enumerate(candidates, start=1):
            hotspot_name = safe_hotspot_name(rank, candidate["file"])
            (hotspot_dir / hotspot_name).write_text(
                render_hotspot(repo_path, rule, rank, candidate, context_lines),
                encoding="utf-8",
            )
            scan_candidates.append(
                {
                    "rank": rank,
                    "file": candidate["file"],
                    "score": candidate["priority_score"],
                    "hotspot": f"hotspots/{hotspot_name}",
                    "state": "pending",
                    "attempts": 0,
                    "last_error": None,
                }
            )

        scan = {
            "version": 1,
            "vas_id": vas_id,
            "repo": str(repo_path),
            "rule": {
                "category": rule["category"],
                "language": rule["language"],
                "summary": rule["summary"],
                "scenarios": rule["scenarios"],
                "anchors": rule["anchors"],
            },
            "settings": {
                "min_anchor_weight": min_anchor_weight,
                "hotspot_context_lines_per_side": context_lines,
                "candidate_batch_size": batch_size,
                "max_candidate_attempts": max_attempts,
            },
            "candidates": scan_candidates,
        }
        report = {"vas_id": vas_id, "repository": str(repo_path), "warnings": []}
        write_json(temporary / "scan.json", scan)
        write_json(temporary / "report.json", report)
        temporary.rename(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination


def load_scan(scan_dir: Path) -> tuple[Path, dict[str, Any]]:
    scan_dir = scan_dir.resolve()
    scan_path = scan_dir / "scan.json"
    if not scan_path.is_file():
        raise FileNotFoundError(f"scan.json not found: {scan_path}")
    scan = read_json(scan_path)
    if not isinstance(scan, dict) or scan.get("version") != 1:
        raise ScanError(f"unsupported scan state: {scan_path}")
    return scan_dir, scan


def find_candidate(scan: dict[str, Any], rank: int) -> dict[str, Any]:
    for candidate in scan["candidates"]:
        if candidate["rank"] == rank:
            return candidate
    raise ValueError(f"candidate rank not found: {rank}")


def next_candidates(scan_dir: Path) -> dict[str, Any]:
    scan_dir, scan = load_scan(scan_dir)
    active = [candidate for candidate in scan["candidates"] if candidate["state"] == "in_progress"]
    if active:
        selected = active
    else:
        pending = [candidate for candidate in scan["candidates"] if candidate["state"] == "pending"]
        exhausted = [
            candidate
            for candidate in pending
            if candidate["attempts"] >= scan["settings"]["max_candidate_attempts"]
        ]
        if exhausted:
            ranks = ", ".join(str(candidate["rank"]) for candidate in exhausted)
            raise ScanError(f"candidate attempts exhausted: {ranks}")
        selected = pending[: scan["settings"]["candidate_batch_size"]]
        for candidate in selected:
            candidate["state"] = "in_progress"
            candidate["attempts"] += 1
            candidate["last_error"] = None
        if selected:
            write_json(scan_dir / "scan.json", scan)

    payload = []
    for candidate in selected:
        payload.append(
            {
                "rank": candidate["rank"],
                "repo": scan["repo"],
                "candidate": candidate["file"],
                "summary": scan["rule"]["summary"],
                "scenarios": scan["rule"]["scenarios"],
                "hotspot": str((scan_dir / candidate["hotspot"]).resolve()),
                "analysis_reference": str(ANALYSIS_REFERENCE),
            }
        )
    done = not payload and all(item["state"] == "completed" for item in scan["candidates"])
    return {"done": done, "candidates": payload}


def repository_relative_path(value: Any, repo_path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty repository-relative path")
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"{field} must be repository-relative: {value}")
    resolved = (repo_path / path).resolve()
    try:
        relative = resolved.relative_to(repo_path)
    except ValueError as exc:
        raise ValueError(f"{field} escapes the repository: {value}") from exc
    if not resolved.is_file():
        raise ValueError(f"{field} does not identify a source file: {value}")
    return relative.as_posix()


def normalize_location(value: Any, repo_path: Path, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    start = value.get("start_line")
    end = value.get("end_line")
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise ValueError(f"{field}.start_line must be a positive integer")
    if not isinstance(end, int) or isinstance(end, bool) or end < start:
        raise ValueError(f"{field}.end_line must be an integer at or after start_line")
    return {
        "file": repository_relative_path(value.get("file"), repo_path, f"{field}.file"),
        "start_line": start,
        "end_line": end,
    }


def normalize_warning(value: Any, repo_path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("each warning must be an object")
    title = value.get("title")
    explanation = value.get("explanation")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("warning.title must be a non-empty string")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("warning.explanation must be a non-empty string")
    confidence = value.get("confidence")
    confidence = confidence.upper() if isinstance(confidence, str) else confidence
    if confidence not in CONFIDENCE_ORDER:
        raise ValueError("warning.confidence must be HIGH, MEDIUM, or LOW")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("warning.evidence must be a non-empty list")
    normalized_evidence = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ValueError(f"warning.evidence[{index}] must be an object")
        fact = item.get("fact")
        if not isinstance(fact, str) or not fact.strip():
            raise ValueError(f"warning.evidence[{index}].fact must be a non-empty string")
        normalized_evidence.append(
            {
                **normalize_location(item, repo_path, f"warning.evidence[{index}]"),
                "fact": fact.strip(),
            }
        )
    return {
        "title": title.strip(),
        "confidence": confidence,
        "primary_location": normalize_location(value.get("primary_location"), repo_path, "warning.primary_location"),
        "explanation": explanation.strip(),
        "evidence": normalized_evidence,
        "source_candidates": [{"rank": candidate["rank"], "file": candidate["file"]}],
    }


def warning_key(warning: dict[str, Any]) -> tuple[str, int, int]:
    location = warning["primary_location"]
    return location["file"], location["start_line"], location["end_line"]


def merge_warning(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    if CONFIDENCE_ORDER[incoming["confidence"]] > CONFIDENCE_ORDER[existing["confidence"]]:
        existing["title"] = incoming["title"]
        existing["confidence"] = incoming["confidence"]
        existing["explanation"] = incoming["explanation"]
    evidence_keys = {
        (item["file"], item["start_line"], item["end_line"], item["fact"])
        for item in existing["evidence"]
    }
    for item in incoming["evidence"]:
        key = (item["file"], item["start_line"], item["end_line"], item["fact"])
        if key not in evidence_keys:
            existing["evidence"].append(item)
            evidence_keys.add(key)
    source_keys = {(item["rank"], item["file"]) for item in existing["source_candidates"]}
    for item in incoming["source_candidates"]:
        key = (item["rank"], item["file"])
        if key not in source_keys:
            existing["source_candidates"].append(item)
            source_keys.add(key)


def deduplicate_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, int, int], dict[str, Any]] = {}
    for warning in warnings:
        key = warning_key(warning)
        if key in unique:
            merge_warning(unique[key], warning)
        else:
            unique[key] = warning
    for warning in unique.values():
        warning["evidence"].sort(
            key=lambda item: (item["file"], item["start_line"], item["end_line"], item["fact"])
        )
        warning["source_candidates"].sort(key=lambda item: (item["rank"], item["file"]))
    return sorted(
        unique.values(),
        key=lambda warning: (
            min(item["rank"] for item in warning["source_candidates"]),
            warning["primary_location"]["file"],
            warning["primary_location"]["start_line"],
            warning["primary_location"]["end_line"],
        ),
    )


def record_analysis(scan_dir: Path, rank: int, warnings: Any) -> dict[str, Any]:
    scan_dir, scan = load_scan(scan_dir)
    candidate = find_candidate(scan, rank)
    if candidate["state"] != "in_progress":
        raise ScanError(f"candidate {rank} is not in progress")
    if not isinstance(warnings, list):
        raise ValueError("record input must be a JSON array")
    repo_path = Path(scan["repo"]).resolve()
    normalized = [normalize_warning(warning, repo_path, candidate) for warning in warnings]

    report_path = scan_dir / "report.json"
    report = read_json(report_path)
    report["warnings"] = deduplicate_warnings(report["warnings"] + normalized)
    candidate["state"] = "completed"
    candidate["last_error"] = None
    write_json(report_path, report)
    write_json(scan_dir / "scan.json", scan)
    return {"rank": rank, "warnings_recorded": len(normalized), "state": "completed"}


def retry_candidate(scan_dir: Path, rank: int, error: str) -> dict[str, Any]:
    scan_dir, scan = load_scan(scan_dir)
    candidate = find_candidate(scan, rank)
    if candidate["state"] != "in_progress":
        raise ScanError(f"candidate {rank} is not in progress")
    if not error.strip():
        raise ValueError("retry input must describe the analysis error")
    candidate["state"] = "pending"
    candidate["last_error"] = error.strip()
    max_attempts = scan["settings"]["max_candidate_attempts"]
    retryable = candidate["attempts"] < max_attempts
    write_json(scan_dir / "scan.json", scan)
    return {"rank": rank, "retryable": retryable, "attempts": candidate["attempts"], "max_attempts": max_attempts}


def finalize_scan(scan_dir: Path) -> dict[str, Any]:
    scan_dir, scan = load_scan(scan_dir)
    unfinished = [candidate for candidate in scan["candidates"] if candidate["state"] != "completed"]
    if unfinished:
        ranks = ", ".join(str(candidate["rank"]) for candidate in unfinished)
        raise ScanError(f"cannot finalize; unfinished candidates: {ranks}")
    report_path = scan_dir / "report.json"
    report = read_json(report_path)
    report["warnings"] = deduplicate_warnings(report["warnings"])
    write_json(report_path, report)
    return {"report": str(report_path), "warning_count": len(report["warnings"])}
