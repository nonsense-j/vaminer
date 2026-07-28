"""Render a deterministic report of generated anchor results."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils.models import RootCauseAnalysis, VASCoreInfo
from .scanner import AnchorRunResult, AnchorScanResult, scan_anchors


def review_anchors(
    vas_id: str,
    core: VASCoreInfo,
    repo_path: Path,
    cases_dir: Path,
    *,
    output_path: Path | None = None,
    max_repo_files: int | None = None,
    context_lines: int = 1,
) -> str:
    """Scan cases and repository, render their anchor results, and optionally save the report."""
    anchors = [anchor.model_dump(mode="json", by_alias=True) for anchor in core.anchors]
    language = core.language.value
    case_scan = scan_anchors(anchors, cases_dir, language)
    repo_scan = scan_anchors(anchors, repo_path, language)
    anchor_labels = make_anchor_labels(repo_scan.anchor_results)
    hotspot_view = render_hotspot_annotated_view(
        repo_scan,
        max_files=max_repo_files,
        context_lines=context_lines,
    )
    markdown = (
        "\n".join(
            [
                f"# Anchor Report: {vas_id}",
                "",
                "## Rule Summary",
                "",
                core.summary,
                "",
                "## Scenarios",
                "",
                "### Unsafe",
                "",
                *[f"- {scenario}" for scenario in core.scenarios.unsafe],
                "",
                "### Safe",
                "",
                *([f"- {scenario}" for scenario in core.scenarios.safe] or ["- None specified."]),
                "",
                render_anchor_label_table(repo_scan.anchor_results, anchor_labels),
                "",
                "## Case Coverage",
                "",
                render_scan_result(case_scan, anchor_labels, label_prefix="F", is_case=True),
                "",
                "## Repository Matches",
                "",
                render_scan_result(repo_scan, anchor_labels, label_prefix="R"),
                "",
                "## Repository Hotspots",
                "",
                hotspot_view,
            ]
        ).rstrip()
        + "\n"
    )
    if output_path:
        output_path.write_text(markdown, encoding="utf-8")
    return markdown


def render_root_cause_analysis(analysis: RootCauseAnalysis) -> str:
    """Render the typed RCA as a stable human-readable artifact."""
    lines = [
        "## Root Cause Summary",
        "",
        analysis.root_cause_summary,
        "",
        "## Analysis",
        "",
        analysis.analysis,
        "",
        "## Concrete Buggy Components",
        "",
    ]
    for component in analysis.buggy_components:
        lines.extend(
            [
                f"### `{component.file}:{component.start_line}`",
                "",
                component.role,
                "",
                "```",
                component.snippet,
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Fixing Pattern",
            "",
            analysis.fixing_pattern,
            "",
            "## Extracted Case Files",
            "",
            *[f"- `cases/{Path(path).as_posix().removeprefix('cases/')}`" for path in analysis.extracted_case_files],
            "",
        ]
    )
    return "\n".join(lines)


def render_hotspot_annotated_view(
    scan: AnchorScanResult,
    *,
    output_path: Path | None = None,
    max_files: int | None = None,
    context_lines: int = 1,
) -> str:
    """Render ranked anchor hotspots from a shared scanner result."""
    candidates = scan.candidates()
    if not candidates:
        view = "No repository hotspots matched."
    else:
        rendered = candidates if max_files is None else candidates[: max(0, max_files)]
        sections = [
            render_hotspot_file(index, scan.root, candidate, context_lines=context_lines)
            for index, candidate in enumerate(rendered, start=1)
        ]
        remaining = len(candidates) - len(rendered)
        if remaining > 0:
            sections.append(f"_Omitted {remaining} lower-priority hotspot file(s)._")
        view = "\n\n".join(sections)
    if output_path:
        output_path.write_text(view.rstrip() + "\n", encoding="utf-8")
    return view


def render_scan_result(
    scan: AnchorScanResult,
    anchor_labels: dict[str, str],
    *,
    label_prefix: str,
    is_case: bool = False,
) -> str:
    matched_files = sorted({match.file for match in scan.matches})
    all_case_files = list_files(scan.root) if is_case else []
    missing_case_files = [file for file in all_case_files if file not in matched_files]
    worked_results = [result for result in scan.anchor_results if result.matches]
    missed_results = [result for result in scan.anchor_results if not result.matches]
    file_count = f"{len(matched_files)}/{len(all_case_files)}" if is_case else str(len(matched_files))
    lines = [
        f"- Total matched files: {file_count}",
        f"- Total matching anchors: {len(worked_results)}/{len(scan.anchor_results)}",
        f"- Total matches: {len(scan.matches)}",
        "",
        "### File Priority Distribution",
        "",
        render_file_priority_table(scan, anchor_labels, label_prefix),
        "",
    ]
    if is_case and not all_case_files:
        lines.append("❌ No case files exist; coverage is invalid.")
    elif is_case and missing_case_files:
        lines.append(f"⚠️ Missing cases: {format_path_list(missing_case_files)}")
    elif is_case:
        lines.append("✅ All cases are matched.")

    if missed_results:
        lines.append(f"ℹ️ Anchors with no matches: {format_anchor_list(missed_results, anchor_labels)}")
    else:
        target = "case file" if is_case else "repository file"
        lines.append(f"ℹ️ Every anchor matched at least one {target}.")

    lines.extend(
        [
            "",
            "### Anchor Matched Locations",
            "",
            render_anchor_matched_locations(scan, anchor_labels),
        ]
    )
    return "\n".join(lines)


def render_anchor_label_table(
    results: list[AnchorRunResult],
    anchor_labels: dict[str, str],
) -> str:
    lines = [
        "| Label | Anchor | Behavior weight | Query weight | Behavior | Inspect hint |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for result in results:
        anchor = result.anchor
        lines.append(
            f"| {anchor_labels[result.anchor_id]} | `{result.anchor_id}` | "
            f"{anchor['behavior_weight']} | {result.query_weight} | "
            f"{escape_table(anchor['behavior'])} | {escape_table(anchor['inspect_hint'])} |"
        )
    return "\n".join(lines)


def render_file_priority_table(
    scan: AnchorScanResult,
    anchor_labels: dict[str, str],
    label_prefix: str,
) -> str:
    candidates = scan.candidates()
    lines = ["| Label | File | Score | Matches |", "| --- | --- | ---: | --- |"]
    if not candidates:
        lines.append("| - | - | 0 | none |")
        return "\n".join(lines)

    for index, candidate in enumerate(candidates, start=1):
        lines.append(
            f"| {label_prefix}{index} | `{candidate['file']}` | "
            f"{candidate['priority_score']} | {render_match_counts(candidate, anchor_labels)} |"
        )
    return "\n".join(lines)


def render_match_counts(candidate: dict[str, Any], anchor_labels: dict[str, str]) -> str:
    return ", ".join(
        f"{len(hint['locations'])}@{anchor_labels[hint['anchor_id']]}" for hint in candidate["anchor_hints"]
    )


def render_anchor_matched_locations(scan: AnchorScanResult, anchor_labels: dict[str, str]) -> str:
    lines = []
    for result in scan.anchor_results:
        matches_by_file: dict[str, list[tuple[int, int]]] = {}
        for match in result.matches:
            matches_by_file.setdefault(match.file, []).append((match.start_line, match.end_line))
        for file in sorted(matches_by_file):
            lines.append(
                f"{len(lines) + 1}. `{anchor_labels[result.anchor_id]}:{result.anchor_id}` "
                f"`{file}`, {format_lines(matches_by_file[file])}"
            )
    return "\n".join(lines) if lines else "No matched locations."


def render_hotspot_file(
    index: int,
    root: Path,
    candidate: dict[str, Any],
    *,
    context_lines: int,
) -> str:
    file_path = root / candidate["file"]
    lines = [
        f"#### {index}. `{candidate['file']}`",
        "",
        f"> Score: {candidate['priority_score']}; Matches: {candidate['raw_match_count']}",
        "",
    ]
    for hint in candidate["anchor_hints"]:
        for location in hint["locations"]:
            lines.extend(
                [
                    f"- `{hint['anchor_id']}` line {location['start_line']} " f"query weight {hint['query_weight']}",
                    "",
                    "```",
                    render_source_excerpt(
                        file_path,
                        location["start_line"],
                        location["end_line"],
                        context_lines,
                    ),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip()


def render_source_excerpt(file_path: Path, start_line: int, end_line: int, context_lines: int) -> str:
    source_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    display_start = max(1, start_line - max(0, context_lines))
    display_end = min(len(source_lines), end_line + max(0, context_lines))
    width = len(str(display_end))
    rendered = []
    for line_no in range(display_start, display_end + 1):
        marker = ">>" if start_line <= line_no <= end_line else "  "
        rendered.append(f"{marker} {line_no:>{width}} | {source_lines[line_no - 1]}")
    return "\n".join(rendered)


def make_anchor_labels(results: list[AnchorRunResult]) -> dict[str, str]:
    return {result.anchor_id: f"A{index}" for index, result in enumerate(results, start=1)}


def list_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def format_anchor_list(results: list[AnchorRunResult], anchor_labels: dict[str, str]) -> str:
    return ", ".join(f"`{anchor_labels[result.anchor_id]}:{result.anchor_id}`" for result in results)


def format_path_list(paths: list[str]) -> str:
    return ", ".join(f"`{path}`" for path in paths) if paths else "none"


def format_lines(locations: list[tuple[int, int]]) -> str:
    start_lines = sorted({start for start, _ in locations})
    label = "line" if len(start_lines) == 1 else "lines"
    return f"{label} {', '.join(str(line) for line in start_lines)}"


def escape_table(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")
