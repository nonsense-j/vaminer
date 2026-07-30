"""Shared model factories used across responsibility-focused tests."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from src.miner.models import AnalysisSubject, RootCauseAnalysis, VASCoreInfo

SOURCE = "void trigger(void) { danger(1); }\n"
BEHAVIOR = "Calls the dangerous operation with one argument."
INSPECT_HINT = "Inspect whether the required guard applies before the matched call."


def root_cause(
    *,
    file: str = "bug.c",
    case_files: Sequence[str] = ("case1.c",),
) -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": (
                "A dangerous operation executes without its required guard."
            ),
            "analysis": (
                "The dangerous operation is reached before the guard establishes "
                "the invariant."
            ),
            "buggy_components": [
                {
                    "file": file,
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": SOURCE.rstrip(),
                }
            ],
            "fixing_pattern": (
                "Establish the required guard before invoking danger."
            ),
            "extracted_case_files": list(case_files),
        }
    )


def vas_core(root_cause: RootCauseAnalysis) -> VASCoreInfo:
    return VASCoreInfo.model_validate(
        {
            "category": "SECURITY",
            "language": root_cause.language,
            "root_cause_summary": root_cause.root_cause_summary,
            "summary": (
                "Dangerous operations must run only after the required guard "
                "is established."
            ),
            "scenarios": {
                "unsafe": [
                    "The dangerous operation executes before its required guard."
                ],
                "safe": [
                    "The required guard is established before the dangerous operation."
                ],
            },
            "anchors": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "query_weight": 5,
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": BEHAVIOR,
                    "inspect_hint": INSPECT_HINT,
                }
            ],
        }
    )


def analysis_subject(
    source_root: Path,
    cases_dir: Path,
    *,
    source_type: str = "issue",
) -> AnalysisSubject:
    return AnalysisSubject(
        type=source_type,
        source_root=source_root.resolve().as_posix(),
        cases_dir=cases_dir.resolve().as_posix(),
        grounding_policy=(
            "repo_evidence"
            if source_type == "issue"
            else "bad_span_coverage"
        ),
    )
