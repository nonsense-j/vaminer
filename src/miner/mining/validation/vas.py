"""Deterministic VAS acceptance."""

from __future__ import annotations

from pathlib import Path

from ...models.analysis import GroundingPolicy, RootCauseAnalysis
from ...models.vas import VASCoreInfo
from .anchors import validate_anchors


def validate_vas_core(
    value: VASCoreInfo,
    *,
    source_root: Path,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    grounding_policy: GroundingPolicy,
) -> list[str]:
    """Validate a complete Rule Generator result against the authoritative RCA."""

    return validate_anchors(
        value,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        grounding_policy=grounding_policy,
    )
