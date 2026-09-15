"""Deterministic validation for final VAS anchors."""

from __future__ import annotations

from pathlib import Path

from ...anchors.scanner import AnchorMatch, AnchorQueryError, scan_anchors
from ...models.analysis import GroundingPolicy, RootCauseAnalysis
from ...models.vas import VASCoreInfo
from ...utils.config import ADMISSION_QUERY_WEIGHT
from .analysis import (
    root_cause_source_spans,
    validate_root_cause_analysis,
)


def _normalized_source_path(value: str) -> str:
    return Path(value).as_posix().removeprefix("./")


def _root_cause_source_files(root_cause: RootCauseAnalysis) -> set[str]:
    return {
        _normalized_source_path(component.file)
        for component in root_cause_source_spans(root_cause)
    }


def disabled_anchor_ids(value: VASCoreInfo) -> tuple[str, ...]:
    """Return anchors intentionally disabled with an empty query."""
    return tuple(anchor.id for anchor in value.anchors if not anchor.query.strip())


def disabled_anchor_warnings(value: VASCoreInfo) -> tuple[str, ...]:
    """Return stable warnings for anchors intentionally disabled by empty queries."""
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
    return tuple(warnings)


def validate_anchors(
    value: VASCoreInfo,
    *,
    repo_path: Path | None = None,
    source_root: Path | None = None,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    grounding_policy: GroundingPolicy,
) -> list[str]:
    """Run ast-grep and validate every enabled anchor in the final VAS."""
    source_path = source_root or repo_path
    if source_path is None:
        return ["source_root or repo_path is required to validate anchors"]
    errors: list[str] = []
    if value.language != root_cause.language:
        errors.append(
            f"rule language {value.language.value} does not match RCA language "
            f"{root_cause.language.value}"
        )
    errors.extend(
        validate_root_cause_analysis(
            root_cause,
            source_root=source_path,
            cases_dir=cases_dir,
        )
    )
    anchor_ids = [anchor.id for anchor in value.anchors]
    if len(anchor_ids) != len(set(anchor_ids)):
        errors.append("anchor ids must be unique")
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
    except AnchorQueryError as exc:
        return [f"ast-grep validation failed: {exc}"]

    case_files = sorted(
        path.relative_to(cases_dir).as_posix()
        for path in cases_dir.rglob("*")
        if path.is_file()
    )
    anchor_case_matches: dict[str, list[AnchorMatch]] = {
        anchor.id: [] for anchor in enabled_anchors
    }
    for match in case_scan.matches:
        if match.anchor_id in disabled_ids:
            continue
        anchor_case_matches.setdefault(match.anchor_id, []).append(match)

    admitted_case_files = {
        candidate["file"]
        for candidate in case_scan.candidates(
            min_anchor_weight=ADMISSION_QUERY_WEIGHT
        )
    }
    missing_cases = sorted(set(case_files) - admitted_case_files)
    if missing_cases:
        errors.append("case files are not admitted: " + ", ".join(missing_cases))

    for anchor in enabled_anchors:
        matches = anchor_case_matches.get(anchor.id, [])
        if not matches:
            errors.append(f"anchor {anchor.id!r} has no case match")

    component_files = _root_cause_source_files(root_cause)
    grounded_ids: set[str] = set()
    for match in source_scan.matches:
        if match.anchor_id in disabled_ids:
            continue
        if _normalized_source_path(match.file) in component_files:
            grounded_ids.add(match.anchor_id)
    for anchor in enabled_anchors:
        if anchor.id not in grounded_ids:
            errors.append(f"anchor {anchor.id!r} has no match in an RCA-declared source file")

    if grounding_policy is GroundingPolicy.BAD_SPAN_COVERAGE:
        admitted_source_files = {
            _normalized_source_path(candidate["file"])
            for candidate in source_scan.candidates(
                min_anchor_weight=ADMISSION_QUERY_WEIGHT
            )
        }
        missing_source_files = sorted(component_files - admitted_source_files)
        if missing_source_files and not disabled_ids:
            errors.append(
                "RCA-declared bad-example files are not admitted: " + ", ".join(missing_source_files)
            )

    return errors


__all__ = [
    "disabled_anchor_ids",
    "disabled_anchor_warnings",
    "validate_anchors",
]
