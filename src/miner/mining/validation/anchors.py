"""Deterministic validation for final VAS anchors."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...anchors.scanner import AnchorMatch, AnchorScanError, scan_anchors
from ...models.analysis import AnalysisSubject, RootCauseAnalysis
from ...models.vas import VASCoreInfo
from .analysis import (
    root_cause_source_spans,
    validate_root_cause_analysis,
)

_VALIDATION_MATCH_LOCATION_LIMIT = 12
_VALIDATION_CASE_EXCERPT_LINE_LIMIT = 60
_VALIDATION_CASE_EXCERPT_CHAR_LIMIT = 4_000
_VALIDATION_RCA_COMPONENT_LIMIT = 8
_VALIDATION_RCA_SNIPPET_CHAR_LIMIT = 2_000


def _bounded_text(value: str, limit: int) -> str:
    """Return a deterministic, visibly clipped diagnostic fragment."""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n... <{len(value) - limit} chars omitted>"


def _match_location(match: AnchorMatch) -> str:
    line_range = str(match.start_line)
    if match.end_line != match.start_line:
        line_range += f"-{match.end_line}"
    return f"{match.file}:{line_range}"


def _format_match_summary(scope: str, matches: Sequence[AnchorMatch]) -> str:
    """Describe actual deterministic matches without flooding repair prompts."""
    locations = sorted({_match_location(match) for match in matches})
    if not locations:
        return f"candidate query produced zero {scope} matches"
    shown = locations[:_VALIDATION_MATCH_LOCATION_LIMIT]
    omitted = len(locations) - len(shown)
    suffix = f", ... <{omitted} locations omitted>" if omitted else ""
    file_count = len({match.file for match in matches})
    return (
        f"candidate query {scope} matches ({len(matches)} total across {file_count} file(s)): "
        + ", ".join(shown)
        + suffix
    )


def _read_case_excerpt(cases_dir: Path, relative: str) -> str:
    """Read a bounded, line-numbered case excerpt for one uncovered case."""
    root = cases_dir.resolve()
    source = (root / relative).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return "<case path escapes the case root>"
    if not source.is_file():
        return "<case file is unavailable>"

    rendered: list[str] = []
    used_chars = 0
    with source.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number > _VALIDATION_CASE_EXCERPT_LINE_LIMIT:
                rendered.append(f"... <lines after {_VALIDATION_CASE_EXCERPT_LINE_LIMIT} omitted>")
                break
            item = f"{line_number:>4}: {line.rstrip()}"
            rendered.append(item)
            used_chars += len(item) + 1
            if used_chars > _VALIDATION_CASE_EXCERPT_CHAR_LIMIT:
                rendered.append("... <case excerpt clipped>")
                break
    return _bounded_text("\n".join(rendered), _VALIDATION_CASE_EXCERPT_CHAR_LIMIT)


def _format_missing_case_evidence(cases_dir: Path, missing_cases: Sequence[str]) -> str:
    selected = list(missing_cases[:4])
    sections = [f"[{relative}]\n{_read_case_excerpt(cases_dir, relative)}" for relative in selected]
    omitted = len(missing_cases) - len(selected)
    if omitted:
        sections.append(f"... <{omitted} missing case excerpts omitted>")
    return "missing case source evidence:\n" + "\n\n".join(sections)


def _format_rca_target_evidence(
    root_cause: RootCauseAnalysis,
    *,
    analysis_subject: AnalysisSubject,
) -> str:
    """Render the bounded set of source sites accepted by grounding validation."""
    all_components = root_cause_source_spans(root_cause)
    components = all_components[:_VALIDATION_RCA_COMPONENT_LIMIT]
    rendered = []
    for component in components:
        snippet = _bounded_text(component.snippet, _VALIDATION_RCA_SNIPPET_CHAR_LIMIT)
        rendered.append(
            f"[{component.file}:{component.start_line}-{component.end_line}] {component.role}\n{snippet}"
        )
    omitted = len(all_components) - len(components)
    if omitted:
        rendered.append(f"... <{omitted} RCA components omitted>")
    label = (
        "accepted RCA bad-span target sites"
        if analysis_subject.type == "example_suite"
        else "accepted RCA repository target sites"
    )
    return f"{label} (match must overlap at least one):\n" + "\n\n".join(rendered)


def _match_overlaps_buggy_component(
    *,
    file: str,
    start_line: int,
    end_line: int,
    root_cause: RootCauseAnalysis,
) -> bool:
    """Return whether a real query match overlaps an RCA-declared causal site."""
    return any(
        Path(file).as_posix().removeprefix("./")
        == Path(span.file).as_posix().removeprefix("./")
        and start_line <= span.end_line
        and end_line >= span.start_line
        for span in root_cause_source_spans(root_cause)
    )


def disabled_anchor_ids(value: VASCoreInfo) -> tuple[str, ...]:
    """Return anchors intentionally disabled with an empty query."""
    return tuple(anchor.id for anchor in value.anchors if not anchor.query.strip())


def disabled_anchor_warnings(value: VASCoreInfo) -> tuple[str, ...]:
    """Return stable warnings for a publishable degraded VAS."""
    disabled = disabled_anchor_ids(value)
    if not disabled:
        return ()
    warnings = [
        (
            f"anchor {anchor_id!r} is disabled because its query is empty; "
            "it contributes no matches or candidate-ranking weight"
        )
        for anchor_id in disabled
    ]
    warnings.append(
        "collective case and source-span coverage are advisory while disabled anchors exist"
    )
    return tuple(warnings)


def validate_anchors(
    value: VASCoreInfo,
    *,
    repo_path: Path | None = None,
    source_root: Path | None = None,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    analysis_subject: AnalysisSubject,
) -> list[str]:
    """Run ast-grep and validate every enabled anchor in the final VAS."""
    source_path = source_root or repo_path
    if source_path is None:
        return ["source_root or repo_path is required to validate anchors"]
    errors = validate_root_cause_analysis(
        root_cause,
        source_root=source_path,
        cases_dir=cases_dir,
    )
    if errors:
        return errors

    anchor_ids = [anchor.id for anchor in value.anchors]
    if len(anchor_ids) != len(set(anchor_ids)):
        errors.append("anchor ids must be unique")
    if value.language != root_cause.language:
        errors.append(
            f"rule language {value.language.value} does not match RCA language "
            f"{root_cause.language.value}"
        )
    if errors:
        return errors

    disabled_ids = set(disabled_anchor_ids(value))
    enabled_anchors = [anchor for anchor in value.anchors if anchor.id not in disabled_ids]
    serialized = [
        anchor.model_dump(mode="json", by_alias=True)
        for anchor in value.anchors
    ]
    try:
        case_scan = scan_anchors(serialized, cases_dir, root_cause.language.value)
        source_scan = scan_anchors(serialized, source_path, root_cause.language.value)
    except (AnchorScanError, FileNotFoundError, ValueError, OSError) as exc:
        return [
            f"ast-grep validation failed: {exc}",
            _format_rca_target_evidence(root_cause, analysis_subject=analysis_subject),
        ]

    case_files = sorted(
        path.relative_to(cases_dir).as_posix()
        for path in cases_dir.rglob("*")
        if path.is_file()
    )
    actual_coverage: dict[str, set[str]] = {path: set() for path in case_files}
    anchor_case_matches: dict[str, list[AnchorMatch]] = {
        anchor.id: [] for anchor in enabled_anchors
    }
    for match in case_scan.matches:
        if match.anchor_id in disabled_ids:
            continue
        actual_coverage.setdefault(match.file, set()).add(match.anchor_id)
        anchor_case_matches.setdefault(match.anchor_id, []).append(match)

    missing_cases = [path for path, matched_ids in actual_coverage.items() if not matched_ids]
    if missing_cases and not disabled_ids:
        errors.extend(
            (
                "anchors do not cover case files: " + ", ".join(missing_cases),
                _format_missing_case_evidence(cases_dir, missing_cases),
            )
        )

    for anchor in enabled_anchors:
        matches = anchor_case_matches.get(anchor.id, [])
        if not matches:
            errors.extend(
                (
                    f"anchor {anchor.id!r} has no case match",
                    _format_match_summary("case", matches),
                )
            )

    if analysis_subject.type == "example_suite":
        uncovered_spans = [
            f"{span.file}:{span.start_line}-{span.end_line}"
            for span in root_cause_source_spans(root_cause)
            if not any(
                Path(match.file).as_posix().removeprefix("./")
                == Path(span.file).as_posix().removeprefix("./")
                and match.start_line <= span.end_line
                and match.end_line >= span.start_line
                for match in source_scan.matches
                if match.anchor_id not in disabled_ids
            )
        ]
        if uncovered_spans and not disabled_ids:
            errors.extend(
                (
                    "inferred bad spans are not covered by anchors: "
                    + ", ".join(uncovered_spans),
                    _format_rca_target_evidence(
                        root_cause,
                        analysis_subject=analysis_subject,
                    ),
                )
            )
    else:
        source_matches_by_anchor: dict[str, list[AnchorMatch]] = {
            anchor.id: [] for anchor in enabled_anchors
        }
        grounded_ids: set[str] = set()
        for match in source_scan.matches:
            if match.anchor_id in disabled_ids:
                continue
            source_matches_by_anchor.setdefault(match.anchor_id, []).append(match)
            if _match_overlaps_buggy_component(
                file=match.file,
                start_line=match.start_line,
                end_line=match.end_line,
                root_cause=root_cause,
            ):
                grounded_ids.add(match.anchor_id)
        for anchor in enabled_anchors:
            if anchor.id not in grounded_ids:
                errors.extend(
                    (
                        f"anchor {anchor.id!r} has no RCA-declared repository-site match",
                        _format_match_summary(
                            "repository",
                            source_matches_by_anchor.get(anchor.id, []),
                        ),
                        _format_rca_target_evidence(
                            root_cause,
                            analysis_subject=analysis_subject,
                        ),
                    )
                )

    return errors


__all__ = [
    "disabled_anchor_ids",
    "disabled_anchor_warnings",
    "validate_anchors",
]
