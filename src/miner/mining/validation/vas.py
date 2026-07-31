"""Deterministic VAS acceptance."""

from __future__ import annotations

from pathlib import Path

from ...models.analysis import AnalysisSubject, RootCauseAnalysis
from ...models.vas import VASCoreInfo
from .anchors import validate_anchors


def validate_vas_core(
    value: VASCoreInfo,
    *,
    source_root: Path,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    analysis_subject: AnalysisSubject | None,
) -> list[str]:
    """Validate a complete Rule Generator result against the authoritative RCA."""

    if analysis_subject is None:
        return ["analysis_subject is required to validate a generated VAS core"]
    errors: list[str] = []
    if value.language != root_cause.language:
        errors.append(
            f"rule language {value.language.value!r} differs from RCA language "
            f"{root_cause.language.value!r}"
        )
    errors.extend(
        validate_anchors(
            value,
            source_root=source_root,
            cases_dir=cases_dir,
            root_cause=root_cause,
            analysis_subject=analysis_subject,
        )
    )
    return errors
