"""Tests for final VAS anchor validation and degraded anchors."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.miner.mining.validation.anchors import (
    disabled_anchor_ids,
    disabled_anchor_warnings,
    validate_anchors,
)
from src.miner.mining.validation.vas import validate_vas_core
from src.miner.models import AnalysisSubject, RootCauseAnalysis, VASCoreInfo

DANGER_BEHAVIOR = "Calls the dangerous operation with one argument."
DANGER_HINT = "Inspect whether the required guard applies before the matched call."


def _prepare_sources(tmp_path: Path) -> tuple[Path, Path, RootCauseAnalysis]:
    source_root = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(
        "void trigger(void) { danger(1); }\n",
        encoding="utf-8",
    )
    (cases_dir / "case1.c").write_text(
        "void first(void) { danger(1); }\n",
        encoding="utf-8",
    )
    (cases_dir / "case2.c").write_text(
        "void second(void) { danger(2); }\n",
        encoding="utf-8",
    )
    root_cause = RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The dangerous operation exposes the causal site.",
            "buggy_components": [
                {
                    "file": "bug.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": "void trigger(void) { danger(1); }",
                }
            ],
            "fixing_pattern": "Establish the required guard before invoking danger.",
            "extracted_case_files": ["case1.c", "case2.c"],
        }
    )
    return source_root, cases_dir, root_cause


def _core(root_cause: RootCauseAnalysis, *queries: str) -> VASCoreInfo:
    anchors = [
        {
            "id": f"danger-call-{index}",
            "behavior_weight": 5,
            "query_weight": 5,
            "type": "pattern",
            "query": query,
            "behavior": DANGER_BEHAVIOR,
            "inspect_hint": DANGER_HINT,
        }
        for index, query in enumerate(queries, start=1)
    ]
    return VASCoreInfo.model_validate(
        {
            "category": "SECURITY",
            "language": root_cause.language,
            "root_cause_summary": root_cause.root_cause_summary,
            "summary": "Dangerous operations require the established guard.",
            "scenarios": {
                "unsafe": ["The operation executes without its guard."],
                "safe": ["The guard applies before the operation."],
            },
            "anchors": anchors,
        }
    )


def _subject(source_root: Path, cases_dir: Path, *, kind: str = "issue") -> AnalysisSubject:
    return AnalysisSubject(
        type=kind,
        source_root=source_root.as_posix(),
        cases_dir=cases_dir.as_posix(),
        grounding_policy=(
            "repo_evidence"
            if kind == "issue"
            else "bad_span_coverage"
        ),
    )


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_final_validation_accepts_enabled_anchor_with_case_and_rca_matches(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    core = _core(root_cause, "danger($ARG);")

    assert validate_anchors(
        core,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_subject(source_root, cases_dir),
    ) == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_final_validation_accepts_a_reworded_root_cause_summary(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    core = _core(root_cause, "danger($ARG);").model_copy(
        update={
            "root_cause_summary": (
                "The dangerous operation can run before its guard is established."
            )
        }
    )

    assert validate_vas_core(
        core,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_subject(source_root, cases_dir),
    ) == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_final_validation_rejects_malformed_non_empty_query(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    core = _core(root_cause, "danger($ARG); audit($ARG);")

    errors = validate_anchors(
        core,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_subject(source_root, cases_dir),
    )

    assert "ast-grep validation failed" in "\n".join(errors)


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_final_validation_rejects_enabled_anchor_without_matches(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    core = _core(root_cause, "missing($ARG);")

    rendered = "\n".join(
        validate_anchors(
            core,
            source_root=source_root,
            cases_dir=cases_dir,
            root_cause=root_cause,
            analysis_subject=_subject(source_root, cases_dir),
        )
    )

    assert "has no case match" in rendered
    assert "has no RCA-declared repository-site match" in rendered
    assert "accepted RCA repository target sites" in rendered


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_disabled_anchor_allows_degraded_collective_coverage(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    core = _core(root_cause, "")

    assert validate_anchors(
        core,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_subject(source_root, cases_dir),
    ) == []
    assert disabled_anchor_ids(core) == ("danger-call-1",)
    warnings = disabled_anchor_warnings(core)
    assert "contributes no matches or candidate-ranking weight" in warnings[0]
    assert "collective case and source-span coverage are advisory" in warnings[1]


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_enabled_anchors_remain_strict_when_another_anchor_is_disabled(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    core = _core(root_cause, "", "missing($ARG);")

    rendered = "\n".join(
        validate_anchors(
            core,
            source_root=source_root,
            cases_dir=cases_dir,
            root_cause=root_cause,
            analysis_subject=_subject(source_root, cases_dir),
        )
    )

    assert "anchor 'danger-call-2' has no case match" in rendered
    assert "anchor 'danger-call-2' has no RCA-declared repository-site match" in rendered


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_disabled_anchor_downgrades_example_span_coverage(tmp_path: Path):
    source_root, cases_dir, root_cause = _prepare_sources(tmp_path)
    payload = root_cause.model_dump(mode="json")
    payload["buggy_components"][0].update(
        {
            "file": "bug.c",
            "snippet": "void trigger(void) { danger(1); }",
        }
    )
    root_cause = RootCauseAnalysis.model_validate(payload)
    core = _core(root_cause, "")

    assert validate_anchors(
        core,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_subject(source_root, cases_dir, kind="example_suite"),
    ) == []
