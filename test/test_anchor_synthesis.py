"""Tests for isolated per-anchor synthesis and deterministic batch aggregation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.miner.core.validation import (
    aggregate_anchor_synthesis_runs,
    validate_anchor_synthesis_run,
)
from src.miner.utils.models import (
    AnalysisSubject,
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
    RootCauseAnalysis,
)

DANGER_BEHAVIOR = "Calls the dangerous operation with one argument."
DANGER_HINT = "Inspect whether the required guard applies before the matched call."
AUDIT_BEHAVIOR = "Records the value passed to the dangerous operation."
AUDIT_HINT = "Trace whether the recorded value corresponds to the dangerous operation."


def _prepare_sources(tmp_path: Path) -> tuple[Path, Path, RootCauseAnalysis]:
    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    repo_path.mkdir()
    cases_dir.mkdir()
    (repo_path / "bug.c").write_text(
        "void trigger(void) { danger(1); }\n" "void record(void) { audit(1); }\n",
        encoding="utf-8",
    )
    (cases_dir / "case1.c").write_text(
        "void first(void) { danger(1); audit(1); }\n",
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
            "analysis": "The dangerous operation and its associated audit site expose the causal chain.",
            "buggy_components": [
                {
                    "file": "bug.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": "void trigger(void) { danger(1); }",
                },
                {
                    "file": "bug.c",
                    "start_line": 2,
                    "end_line": 2,
                    "role": "Records the associated value.",
                    "snippet": "void record(void) { audit(1); }",
                },
            ],
            "fixing_pattern": "Establish the required guard before invoking danger.",
            "extracted_case_files": ["case1.c", "case2.c"],
        }
    )
    return repo_path, cases_dir, root_cause


def _request(root_cause: RootCauseAnalysis) -> AnchorSynthesisRequest:
    return AnchorSynthesisRequest.model_validate(
        {
            "root_cause": root_cause.model_dump(mode="json"),
            "summary": "Dangerous operations must run only after the required guard is established.",
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": DANGER_BEHAVIOR,
                    "inspect_hint": DANGER_HINT,
                    "required_cases": ["case1.c", "case2.c"],
                },
                {
                    "id": "audit-call",
                    "behavior_weight": 3,
                    "behavior": AUDIT_BEHAVIOR,
                    "inspect_hint": AUDIT_HINT,
                    "required_cases": ["case1.c"],
                },
            ],
        }
    )


def _run_result(anchor_id: str) -> AnchorSynthesisRunResult:
    if anchor_id == "danger-call":
        return AnchorSynthesisRunResult.model_validate(
            {
                "anchor": {
                    "id": anchor_id,
                    "behavior_weight": 5,
                    "query_weight": 5,
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": DANGER_BEHAVIOR,
                    "inspect_hint": DANGER_HINT,
                },
                "adjustments": ["Generalized the call argument with a metavariable."],
            }
        )
    return AnchorSynthesisRunResult.model_validate(
        {
            "anchor": {
                "id": anchor_id,
                "behavior_weight": 3,
                "query_weight": 3,
                "type": "pattern",
                "query": "audit($ARG);",
                "behavior": AUDIT_BEHAVIOR,
                "inspect_hint": AUDIT_HINT,
            },
            "adjustments": [],
        }
    )


def _run_result_with_query(
    anchor_id: str,
    query: str,
    *,
    query_type: str = "pattern",
) -> AnchorSynthesisRunResult:
    payload = _run_result(anchor_id).model_dump(mode="json", by_alias=True)
    payload["anchor"]["type"] = query_type
    payload["anchor"]["query"] = query
    return AnchorSynthesisRunResult.model_validate(payload)


def _issue_subject(source_root: Path, cases_dir: Path) -> AnalysisSubject:
    return AnalysisSubject(
        type="issue",
        source_root=source_root.as_posix(),
        cases_dir=cases_dir.as_posix(),
        grounding_policy="repo_evidence",
    )


def _example_subject(source_root: Path, cases_dir: Path) -> AnalysisSubject:
    return AnalysisSubject(
        type="example_suite",
        source_root=source_root.as_posix(),
        cases_dir=cases_dir.as_posix(),
        grounding_policy="bad_span_coverage",
    )


def _example_suite_root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The example suite demonstrates the dangerous operation without a preceding guard.",
            "buggy_components": [
                {
                    "file": "bad.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation without the required guard.",
                    "snippet": "void trigger(void) { danger(1); }",
                }
            ],
            "fixing_pattern": "Observed good examples establish the required guard.",
            "extracted_case_files": ["case1.c"],
        }
    )


def _example_suite_request(root_cause: RootCauseAnalysis) -> AnchorSynthesisRequest:
    return AnchorSynthesisRequest.model_validate(
        {
            "root_cause": root_cause.model_dump(mode="json"),
            "summary": "Dangerous operations must run only after the required guard is established.",
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": DANGER_BEHAVIOR,
                    "inspect_hint": DANGER_HINT,
                    "required_cases": ["case1.c"],
                }
            ],
        }
    )


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_per_anchor_validation_ignores_other_intents_case_contract(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)
    audit_request = AnchorSynthesisRunRequest(
        root_cause=root_cause,
        summary=request.summary,
        anchor_intent=request.anchor_intents[1],
    )

    assert (
        validate_anchor_synthesis_run(
            _run_result("audit-call"),
            repo_path=repo_path,
            cases_dir=cases_dir,
            root_cause=root_cause,
            run_request=audit_request,
            analysis_subject=_issue_subject(repo_path, cases_dir),
        )
        == []
    )


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_anchor_validation_reports_partial_case_matches_and_missing_source(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)
    run_request = AnchorSynthesisRunRequest(
        root_cause=root_cause,
        summary=request.summary,
        anchor_intent=request.anchor_intents[0],
    )

    errors = validate_anchor_synthesis_run(
        _run_result_with_query("danger-call", "audit($ARG);"),
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        run_request=run_request,
        analysis_subject=_issue_subject(repo_path, cases_dir),
    )

    rendered = "\n".join(errors)
    assert "does not match required cases: case2.c" in rendered
    assert "candidate query case matches (1 total across 1 file(s)): case1.c:1" in rendered
    assert "missing required case source evidence:\n[case2.c]" in rendered
    assert "danger(2)" in rendered
    assert "has no RCA-declared repository-site match" not in rendered


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_anchor_validation_distinguishes_zero_matches_and_lists_rca_targets(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)
    run_request = AnchorSynthesisRunRequest(
        root_cause=root_cause,
        summary=request.summary,
        anchor_intent=request.anchor_intents[0],
    )

    errors = validate_anchor_synthesis_run(
        _run_result_with_query("danger-call", "missing($ARG);"),
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        run_request=run_request,
        analysis_subject=_issue_subject(repo_path, cases_dir),
    )

    rendered = "\n".join(errors)
    assert "candidate query produced zero case matches" in rendered
    assert "candidate query produced zero repository matches" in rendered
    assert "accepted RCA repository target sites" in rendered
    assert "[bug.c:1-1] Executes the dangerous operation." in rendered
    assert "void trigger(void) { danger(1); }" in rendered


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_anchor_validation_reports_unrelated_repository_match_locations(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    (repo_path / "bug.c").write_text(
        "void trigger(void) { harmless(1); }\n"
        "void record(void) { audit(1); }\n"
        "void unrelated(void) { danger(3); }\n",
        encoding="utf-8",
    )
    payload = root_cause.model_dump(mode="json")
    payload["buggy_components"][0]["snippet"] = "void trigger(void) { harmless(1); }"
    root_cause = RootCauseAnalysis.model_validate(payload)
    request = _request(root_cause)
    run_request = AnchorSynthesisRunRequest(
        root_cause=root_cause,
        summary=request.summary,
        anchor_intent=request.anchor_intents[0],
    )

    errors = validate_anchor_synthesis_run(
        _run_result("danger-call"),
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        run_request=run_request,
        analysis_subject=_issue_subject(repo_path, cases_dir),
    )

    rendered = "\n".join(errors)
    assert "has no RCA-declared repository-site match" in rendered
    assert "candidate query repository matches (1 total across 1 file(s)): bug.c:3" in rendered
    assert "accepted RCA repository target sites" in rendered


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_anchor_validation_reports_parse_failure_with_repair_evidence(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)
    run_request = AnchorSynthesisRunRequest(
        root_cause=root_cause,
        summary=request.summary,
        anchor_intent=request.anchor_intents[0],
    )

    errors = validate_anchor_synthesis_run(
        _run_result_with_query("danger-call", "danger($ARG); audit($ARG);"),
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        run_request=run_request,
        analysis_subject=_issue_subject(repo_path, cases_dir),
    )

    rendered = "\n".join(errors)
    assert "could not be parsed or executed against required cases" in rendered
    assert "required case files: case1.c, case2.c" in rendered
    assert "missing required case source evidence" in rendered
    assert "accepted RCA repository target sites" in rendered


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_batch_aggregation_restores_request_order_and_derives_metadata(tmp_path: Path):
    repo_path, cases_dir, root_cause = _prepare_sources(tmp_path)
    request = _request(root_cause)

    result = aggregate_anchor_synthesis_runs(
        request,
        [_run_result("audit-call"), _run_result("danger-call")],
        repo_path=repo_path,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_issue_subject(repo_path, cases_dir),
    )

    assert [anchor.id for anchor in result.anchors] == ["danger-call", "audit-call"]
    assert [item.model_dump() for item in result.case_coverage] == [
        {"path": "case1.c", "anchor_ids": ["danger-call", "audit-call"]},
        {"path": "case2.c", "anchor_ids": ["danger-call"]},
    ]
    assert [item.model_dump() for item in result.repo_evidence] == [
        {"anchor_id": "danger-call", "file": "bug.c", "line": 1},
        {"anchor_id": "audit-call", "file": "bug.c", "line": 2},
    ]
    assert result.adjustments == [
        "danger-call: Generalized the call argument with a metavariable.",
    ]


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_example_suite_aggregation_uses_bad_span_coverage_without_repo_evidence(tmp_path: Path):
    source_root = tmp_path / "snapshot"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bad.c").write_text("void trigger(void) { danger(1); }\n", encoding="utf-8")
    (cases_dir / "case1.c").write_text("void sample(void) { danger(1); }\n", encoding="utf-8")
    root_cause = _example_suite_root_cause()
    request = _example_suite_request(root_cause)

    result = aggregate_anchor_synthesis_runs(
        request,
        [_run_result("danger-call")],
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=_example_subject(source_root, cases_dir),
    )

    assert [anchor.id for anchor in result.anchors] == ["danger-call"]
    assert [item.model_dump() for item in result.case_coverage] == [
        {"path": "case1.c", "anchor_ids": ["danger-call"]},
    ]
    assert result.repo_evidence == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep is required")
def test_example_suite_aggregation_requires_each_bad_span_to_be_covered(tmp_path: Path):
    source_root = tmp_path / "snapshot"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bad.c").write_text("void trigger(void) { unsafe(1); }\n", encoding="utf-8")
    (cases_dir / "case1.c").write_text("void sample(void) { danger(1); }\n", encoding="utf-8")
    root_cause = RootCauseAnalysis.model_validate(
        {
            **_example_suite_root_cause().model_dump(mode="json"),
            "buggy_components": [
                {
                    "file": "bad.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation without the required guard.",
                    "snippet": "void trigger(void) { unsafe(1); }",
                }
            ],
        }
    )
    request = _example_suite_request(root_cause)

    with pytest.raises(ValueError, match="inferred bad spans are not covered"):
        aggregate_anchor_synthesis_runs(
            request,
            [_run_result("danger-call")],
            source_root=source_root,
            cases_dir=cases_dir,
            root_cause=root_cause,
            analysis_subject=_example_subject(source_root, cases_dir),
        )
