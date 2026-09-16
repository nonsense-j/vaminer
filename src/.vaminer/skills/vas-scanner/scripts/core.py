"""Standalone task-artifact workflow for the VAS scanner.

The scanner owns discovery, task generation, shallow result validation, and
final aggregation. The Agent owns scheduling and semantic analysis. A run has
an immutable manifest; task completion is represented by a result file.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
from typing import Any

from config import ADMISSION_QUERY_WEIGHT, CONCURRENCY, MAX_CANDIDATES, WORKSPACE_DIR
from engine import AnchorScanError as ScanError
from engine import find_ast_grep, scan_anchors


SKILL_DIR = Path(__file__).resolve().parent.parent
RULES_DIR = SKILL_DIR / "rules"
VAS_ID_RE = re.compile(r"^VAS-[0-9]+$")
FACT_FIELDS = {"anchorId", "startLine", "status", "fact"}
REPORT_FIELDS = {
    "buggyFilePath", "defectLevel", "defectType", "functionName", "mainBuggyLine",
    "description", "mainBuggyCode", "fixSuggestion", "fixCode", "events",
}
EVENT_FIELDS = {"description", "line", "main", "path", "mainBuggyCode", "codeContext"}
REPORT_STRINGS = REPORT_FIELDS - {"defectLevel", "mainBuggyLine", "events"}
EVENT_STRINGS = EVENT_FIELDS - {"line", "main"}
MANIFEST_FIELDS = {"rule_id", "repository", "overview", "config", "task_count", "tasks"}
MANIFEST_TASK_FIELDS = {"task_id", "task_file", "candidate_file"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, value: str) -> None:
    """Write a file atomically using only the standard library."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def require_string(data: dict[str, Any], key: str, source: Path | str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string: {source}")
    return value.strip()


def require_string_list(data: dict[str, Any], key: str, source: Path | str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings: {source}")
    return [item.strip() for item in value]


def load_rule(vas_id: str, rules_dir: Path = RULES_DIR) -> dict[str, Any]:
    if not VAS_ID_RE.fullmatch(vas_id):
        raise ValueError(f"invalid VAS id: {vas_id!r}")
    source = rules_dir / f"{vas_id}.json"
    if not source.is_file():
        raise FileNotFoundError(f"VAS rule not found: {source}")
    raw = read_json(source)
    required = {"vas_id", "category", "language", "summary", "scenarios", "anchors"}
    if not isinstance(raw, dict) or not required <= set(raw):
        raise ValueError(f"invalid rule fields: {source}")
    if "search_profile" in raw or "criteria" in raw:
        raise ValueError(f"legacy rule fields are not supported: {source}")
    if require_string(raw, "vas_id", source) != vas_id:
        raise ValueError(f"rule declares {raw['vas_id']!r}, expected {vas_id!r}: {source}")
    scenarios = raw["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != {"unsafe", "safe"}:
        raise ValueError(f"invalid scenarios: {source}")
    normalized_scenarios = {
        "unsafe": require_string_list(scenarios, "unsafe", source),
        "safe": require_string_list(scenarios, "safe", source),
    }
    if not normalized_scenarios["unsafe"]:
        raise ValueError(f"scenarios.unsafe must not be empty: {source}")
    anchors = raw["anchors"]
    if not isinstance(anchors, list) or not anchors:
        raise ValueError(f"anchors must be a non-empty list: {source}")
    normalized_anchors = []
    ids: set[str] = set()
    for anchor in anchors:
        expected = {"id", "behavior_weight", "query_weight", "type", "query", "behavior", "inspect_hint"}
        if not isinstance(anchor, dict) or set(anchor) != expected:
            raise ValueError(f"invalid anchor fields: {source}")
        anchor_id = require_string(anchor, "id", source)
        if anchor_id in ids:
            raise ValueError(f"duplicate anchor id {anchor_id!r}: {source}")
        ids.add(anchor_id)
        query_type = require_string(anchor, "type", source)
        if query_type not in {"pattern", "rule"}:
            raise ValueError(f"unsupported anchor type {query_type!r}: {source}")
        behavior_weight = anchor["behavior_weight"]
        query_weight = anchor["query_weight"]
        if (type(behavior_weight) is not int or not 1 <= behavior_weight <= 5
                or type(query_weight) is not int or not 1 <= query_weight <= behavior_weight):
            raise ValueError(f"invalid anchor weights: {source}")
        if not isinstance(anchor["query"], str):
            raise ValueError(f"anchor.query must be a string: {source}")
        normalized_anchors.append({
            "id": anchor_id,
            "behavior_weight": behavior_weight,
            "query_weight": query_weight,
            "type": query_type,
            "query": anchor["query"],
            "behavior": require_string(anchor, "behavior", source),
            "inspect_hint": require_string(anchor, "inspect_hint", source),
        })
    return {
        "vas_id": vas_id,
        "category": require_string(raw, "category", source),
        "language": require_string(raw, "language", source),
        "summary": require_string(raw, "summary", source),
        "scenarios": normalized_scenarios,
        "anchors": normalized_anchors,
    }


def source_excerpt(source: list[str], start_line: int) -> str:
    if not 1 <= start_line <= len(source):
        return "<no source line>"
    excerpt = " ".join(source[start_line - 1].strip().split()) or "<blank line>"
    return excerpt[:297] + "..." if len(excerpt) > 300 else excerpt


def candidate_anchors(hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "anchor_id": hint["anchor_id"],
        "query_weight": hint["query_weight"],
        "behavior": hint["behavior"],
        "hint": hint["inspect_hint"],
        "start_lines": sorted({location["start_line"] for location in hint["locations"]}),
    } for hint in hints]


def render_task_prompt(
    *,
    repo_path: Path,
    run_dir: Path,
    overview_path: Path,
    shared_checks_dir: Path,
    rule: dict[str, Any],
    candidate: dict[str, Any],
    source: list[str],
) -> str:
    """Fill the single task template with one candidate's runtime context."""

    from prompt import TASK_TEMPLATE

    anchor_sections: list[str] = []
    for anchor in candidate["anchors"]:
        lines = [
            f"### `{anchor['anchor_id']}`",
            "",
            f"- Query weight: {anchor['query_weight']}",
            f"- Behavior: {anchor['behavior']}",
            f"- Inspect hint: {anchor['hint']}",
        ]
        for line in anchor["start_lines"]:
            excerpt = source_excerpt(source, line).replace("`", "\\`")
            lines.append(f"- Line {line}: `{excerpt}`")
        lines.append("")
        anchor_sections.extend(lines)

    record_script = Path(__file__).resolve().parent / "scan.py"
    record_command = shlex.join([
        "python3", str(record_script), "record", str(run_dir), candidate["task_id"],
    ])
    return TASK_TEMPLATE.format(
        task_id=candidate["task_id"],
        rule_id=rule["vas_id"],
        rule_category=rule["category"],
        repository=repo_path,
        candidate_file=candidate["file"],
        overview_path=overview_path,
        shared_checks_path=shared_checks_dir,
        run_dir=run_dir,
        rule_summary=rule["summary"],
        unsafe_scenarios="\n".join(f"- {item}" for item in rule["scenarios"]["unsafe"]),
        safe_scenarios="\n".join(f"- {item}" for item in rule["scenarios"]["safe"])
        or "- None specified.",
        anchor_hints="\n".join(anchor_sections),
        record_command=record_command,
        result_path=run_dir / "results" / f"{candidate['task_id']}.json",
    )


def validate_relative_file(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"{field} must be a repository-relative path")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError(f"{field} must not escape the repository")
    return path.as_posix()


def make_manifest(
    vas_id: str,
    repo_path: Path,
    overview: Path,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    tasks = []
    for rank, item in enumerate(candidates, start=1):
        candidate_file = validate_relative_file(item["file"], "candidate file")
        tasks.append({
            "task_id": f"TASK-{rank:04d}",
            "task_file": f"tasks/TASK-{rank:04d}.md",
            "candidate_file": candidate_file,
        })
    return {
        "rule_id": vas_id,
        "repository": str(repo_path),
        "overview": overview.name,
        "config": {
            "concurrency": CONCURRENCY,
            "max_candidates": MAX_CANDIDATES,
            "admission_query_weight": ADMISSION_QUERY_WEIGHT,
        },
        "task_count": len(tasks),
        "tasks": tasks,
    }


def prepare_scan(
    vas_id: str,
    repo_path: Path,
    *,
    rules_dir: Path = RULES_DIR,
    workspace_dir: Path | str | None = WORKSPACE_DIR,
    ast_grep: str | None = None,
) -> dict[str, Any]:
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise FileNotFoundError(f"repository does not exist: {repo_path}")
    if type(ADMISSION_QUERY_WEIGHT) is not int or not 1 <= ADMISSION_QUERY_WEIGHT <= 5:
        raise ValueError("ADMISSION_QUERY_WEIGHT must be between 1 and 5")
    if type(MAX_CANDIDATES) is not int or MAX_CANDIDATES < 1:
        raise ValueError("MAX_CANDIDATES must be positive")
    if type(CONCURRENCY) is not int or CONCURRENCY < 1:
        raise ValueError("CONCURRENCY must be positive")
    rule = load_rule(vas_id, rules_dir)
    overview_source = repo_path / ".vas" / "repository_overview.md"
    if not overview_source.is_file():
        raise FileNotFoundError(
            f"repository overview is required before prepare: {overview_source}"
        )
    discovered = scan_anchors(rule["anchors"], repo_path, rule["language"], ast_grep=ast_grep)
    matched = discovered.candidates(min_anchor_weight=1)
    admitted = [item for item in matched if item["max_anchor_weight"] >= ADMISSION_QUERY_WEIGHT]
    scheduled = admitted[:MAX_CANDIDATES]
    parent = (Path(workspace_dir).expanduser().resolve() if workspace_dir else repo_path / ".vas") / vas_id
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    try:
        tasks_dir = temporary / "tasks"
        tasks_dir.mkdir()
        (temporary / "shared_checks").mkdir()
        (temporary / "results").mkdir()
        overview_snapshot = temporary / "repository_overview.md"
        shutil.copyfile(overview_source, overview_snapshot)
        manifest = make_manifest(
            vas_id, repo_path, overview_snapshot, scheduled,
        )
        for rank, item in enumerate(scheduled, start=1):
            task = manifest["tasks"][rank - 1]
            candidate = dict(item)
            candidate["anchors"] = candidate_anchors(item["anchor_hints"])
            candidate["task_id"] = task["task_id"]
            candidate["task_file"] = task["task_file"]
            source = (repo_path / task["candidate_file"]).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            task_text = render_task_prompt(
                repo_path=repo_path,
                run_dir=destination,
                overview_path=destination / "repository_overview.md",
                shared_checks_dir=destination / "shared_checks",
                rule=rule,
                candidate=candidate,
                source=source,
            )
            write_text(temporary / task["task_file"], task_text)
        write_json(temporary / "manifest.json", manifest)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "run_dir": str(destination.resolve()),
        "manifest": str((destination / "manifest.json").resolve()),
        "repository": str(repo_path),
        "concurrency": CONCURRENCY,
        "max_candidates": MAX_CANDIDATES,
        "task_count": len(scheduled),
        "tasks": manifest["tasks"],
    }


def load_manifest(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    state = read_json(manifest_path)
    if not isinstance(state, dict) or set(state) != MANIFEST_FIELDS:
        raise ScanError(f"unsupported manifest: {manifest_path}")
    if not isinstance(state["rule_id"], str) or not isinstance(state["repository"], str):
        raise ScanError(f"invalid manifest identity: {manifest_path}")
    if state["overview"] != "repository_overview.md":
        raise ScanError(f"manifest overview must be repository_overview.md: {manifest_path}")
    config = state["config"]
    if not isinstance(config, dict) or not isinstance(state["task_count"], int) or state["task_count"] < 0:
        raise ScanError(f"invalid manifest configuration: {manifest_path}")
    tasks = state["tasks"]
    if not isinstance(tasks, list) or len(tasks) != state["task_count"]:
        raise ScanError(f"manifest task_count does not match tasks: {manifest_path}")
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != MANIFEST_TASK_FIELDS:
            raise ScanError(f"invalid manifest task: {manifest_path}")
        task_id = task["task_id"]
        if not isinstance(task_id, str) or not re.fullmatch(r"TASK-[0-9]{4}", task_id) or task_id in seen:
            raise ScanError(f"invalid or duplicate task id: {manifest_path}")
        seen.add(task_id)
        if task["task_file"] != f"tasks/{task_id}.md":
            raise ScanError(f"invalid task file for {task_id}: {manifest_path}")
        validate_relative_file(task["candidate_file"], "candidate_file")
    return state


def validate_fields(value: Any, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{context} must have exactly these fields: {', '.join(sorted(fields))}")
    return value


def validate_result(value: Any) -> dict[str, Any]:
    payload = validate_fields(value, {"anchorFacts", "reports"}, "record input")
    facts = payload["anchorFacts"]
    reports = payload["reports"]
    if not isinstance(facts, list) or not isinstance(reports, list):
        raise ValueError("anchorFacts and reports must be arrays")
    for index, item in enumerate(facts):
        fact = validate_fields(item, FACT_FIELDS, f"anchorFacts[{index}]")
        if (not isinstance(fact["anchorId"], str) or type(fact["startLine"]) is not int
                or fact["startLine"] < 1 or not isinstance(fact["status"], str)
                or fact["status"] not in {"SAFE", "ALERT"}
                or not isinstance(fact["fact"], str) or not fact["fact"].strip()):
            raise ValueError(f"invalid anchorFacts[{index}]")
    for index, item in enumerate(reports):
        report = validate_fields(item, REPORT_FIELDS, f"reports[{index}]")
        if (any(not isinstance(report[key], str) for key in REPORT_STRINGS)
                or type(report["defectLevel"]) is not int or not 0 <= report["defectLevel"] <= 4
                or type(report["mainBuggyLine"]) is not int or report["mainBuggyLine"] < 1
                or not isinstance(report["events"], list)):
            raise ValueError(f"invalid reports[{index}]")
        for event_index, item in enumerate(report["events"]):
            event = validate_fields(item, EVENT_FIELDS, f"reports[{index}].events[{event_index}]")
            if (any(not isinstance(event[key], str) for key in EVENT_STRINGS)
                    or type(event["line"]) is not int or event["line"] < 1
                    or type(event["main"]) is not bool):
                raise ValueError(f"invalid reports[{index}].events[{event_index}]")
    return payload


def render_shared_checks(candidate_file: str, facts: list[dict[str, Any]]) -> str:
    lines = [f"# Shared Checks: `{candidate_file}`", ""]
    for fact in facts:
        lines.extend([
            f"## `{fact['anchorId']}` at line {fact['startLine']}", "",
            f"- Status: `{fact['status']}`",
            f"- Fact: {fact['fact']}", "",
        ])
    return "\n".join(lines)


def find_task(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in manifest["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise ValueError(f"task not found in manifest: {task_id}")


def record_analysis(run_dir: Path, task_id: str, result: Any) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest = load_manifest(run_dir)
    task = find_task(manifest, task_id)
    payload = validate_result(result)
    shared_path = run_dir / "shared_checks" / f"{task['candidate_file']}.md"
    result_path = run_dir / "results" / f"{task_id}.json"
    write_text(shared_path, render_shared_checks(task["candidate_file"], payload["anchorFacts"]))
    write_json(result_path, payload["reports"])
    (run_dir / "report.json").unlink(missing_ok=True)
    return {"task_id": task_id, "state": "completed", "result": str(result_path.resolve())}


def validate_reports_file(path: Path) -> list[dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, list):
        raise ValueError(f"result must be an array: {path}")
    validate_result({"anchorFacts": [], "reports": value})
    return value


def finalize_scan(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest = load_manifest(run_dir)
    result_dir = run_dir / "results"
    expected = {task["task_id"] for task in manifest["tasks"]}
    actual: set[str] = set()
    invalid: list[str] = []
    reports_by_task: dict[str, list[dict[str, Any]]] = {}
    if result_dir.is_dir():
        for path in result_dir.iterdir():
            if not path.is_file() or path.suffix != ".json":
                invalid.append(path.name)
                continue
            task_id = path.stem
            actual.add(task_id)
            if task_id not in expected:
                invalid.append(task_id)
                continue
            try:
                reports_by_task[task_id] = validate_reports_file(path)
            except (OSError, ValueError, json.JSONDecodeError):
                invalid.append(task_id)
    missing = sorted(expected - actual)
    if missing or invalid or actual - expected:
        (run_dir / "report.json").unlink(missing_ok=True)
        details = []
        if missing:
            details.append(f"missing tasks: {', '.join(missing)}")
        if invalid:
            details.append(f"invalid or extra results: {', '.join(sorted(set(invalid)))}")
        raise RuntimeError("finalize incomplete; " + "; ".join(details))
    seen: set[tuple[str, int, str]] = set()
    reports: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        for report in reports_by_task[task["task_id"]]:
            key = (report["buggyFilePath"], report["mainBuggyLine"], report["defectType"])
            if key not in seen:
                seen.add(key)
                reports.append(report)
    report_path = run_dir / "report.json"
    write_json(report_path, reports)
    return {
        "status": "complete",
        "report": str(report_path.resolve()),
        "task_count": manifest["task_count"],
        "report_count": len(reports),
    }


def preflight_scan(vas_id: str, repo_path: Path, *, rules_dir: Path = RULES_DIR) -> dict[str, Any]:
    checks: list[str] = []
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    checks.append(f"python {sys.version_info.major}.{sys.version_info.minor}")
    binary = find_ast_grep()
    checks.append(f"ast-grep {binary}")
    load_rule(vas_id, rules_dir)
    checks.append(f"rule {vas_id}")
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise FileNotFoundError(f"repository does not exist: {repo_path}")
    checks.append(f"repository {repo_path}")
    vas_dir = repo_path / ".vas"
    vas_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".preflight.", dir=vas_dir)
    os.close(descriptor)
    Path(temporary).unlink(missing_ok=True)
    checks.append(f"writable {vas_dir}")
    return {"status": "ready", "checks": checks, "repository": str(repo_path), "rule_id": vas_id}
