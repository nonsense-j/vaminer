"""Check generated Anchors against cases and source without persisting a review."""

from __future__ import annotations

from pathlib import Path

from ..mining.validation.analysis import root_cause_source_spans
from ..mining.validation.anchors import disabled_anchor_warnings
from ..models.analysis import GroundingPolicy, RootCauseAnalysis
from ..models.vas import VASCoreInfo
from ..utils.config import ADMISSION_QUERY_WEIGHT
from .scanner import scan_anchors


def check_anchor_coverage(
    core: VASCoreInfo,
    source_root: Path,
    cases_dir: Path,
    *,
    root_cause: RootCauseAnalysis | None = None,
    grounding_policy: GroundingPolicy | None = None,
) -> list[str]:
    """Return deterministic, non-blocking warnings about generated Anchor coverage."""

    anchors = [anchor.model_dump(mode="json", by_alias=True) for anchor in core.anchors]
    language = core.language.value
    case_scan = scan_anchors(anchors, cases_dir, language)
    source_scan = scan_anchors(anchors, source_root, language)
    warnings = list(disabled_anchor_warnings(core))
    admitted_cases = {
        item["file"] for item in case_scan.candidates(min_anchor_weight=ADMISSION_QUERY_WEIGHT)
    }
    missing_cases = sorted(
        path.relative_to(cases_dir).as_posix()
        for path in cases_dir.rglob("*")
        if path.is_file() and path.relative_to(cases_dir).as_posix() not in admitted_cases
    )
    if missing_cases:
        warnings.append("enabled anchors do not admit case files: " + ", ".join(missing_cases))
    if root_cause is not None and grounding_policy is GroundingPolicy.BAD_SPAN_COVERAGE:
        admitted_source = {
            Path(item["file"]).as_posix().removeprefix("./")
            for item in source_scan.candidates(min_anchor_weight=ADMISSION_QUERY_WEIGHT)
        }
        missing_source = sorted({
            Path(span.file).as_posix().removeprefix("./")
            for span in root_cause_source_spans(root_cause)
        } - admitted_source)
        if missing_source:
            warnings.append(
                "enabled anchors do not admit RCA-declared bad-example files: "
                + ", ".join(missing_source)
            )
    return warnings
