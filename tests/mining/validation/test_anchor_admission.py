from pathlib import Path

import pytest

from src.miner.anchors.scanner import (
    AnchorMatch,
    AnchorRunResult,
    AnchorScanResult,
)
from src.miner.mining.validation import anchors as anchor_validation
from src.miner.models import (
    Anchor,
    AstGrepLanguage,
    BuggyComponent,
    GroundingPolicy,
    IssueCategory,
    RootCauseAnalysis,
    Scenarios,
    VASCoreInfo,
)


def _root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        language=AstGrepLanguage.C,
        root_cause_summary="two-file defect",
        analysis="A primary operation and supporting operation form the defect.",
        buggy_components=[
            BuggyComponent(
                file="bug1.c",
                start_line=2,
                end_line=2,
                role="primary",
                snippet="primary();",
            ),
            BuggyComponent(
                file="bug2.c",
                start_line=2,
                end_line=2,
                role="supporting",
                snippet="supporting();",
            ),
        ],
        fixing_pattern="restore the invariant",
        extracted_case_files=["case1.c", "case2.c"],
    )


def _core(*, supporting_weight: int) -> VASCoreInfo:
    return VASCoreInfo(
        category=IssueCategory.SECURITY,
        language=AstGrepLanguage.C,
        root_cause_summary="two-file defect",
        summary="The two operations must preserve the invariant.",
        scenarios=Scenarios(unsafe=["The invariant is violated."], safe=[]),
        anchors=[
            Anchor(
                id="primary",
                behavior_weight=4,
                query_weight=3,
                type="pattern",
                query="primary()",
                behavior="Performs the primary operation.",
                inspect_hint="Inspect the supporting operation.",
            ),
            Anchor(
                id="supporting",
                behavior_weight=3,
                query_weight=supporting_weight,
                type="pattern",
                query="supporting()",
                behavior="Performs the supporting operation.",
                inspect_hint="Inspect the primary operation.",
            ),
        ],
    )


def _scan_result(
    root: Path,
    anchors: list[dict[str, object]],
    locations: dict[str, tuple[str, int]],
) -> AnchorScanResult:
    results = []
    for anchor in anchors:
        anchor_id = str(anchor["id"])
        file, line = locations[anchor_id]
        match = AnchorMatch(
            anchor_id=anchor_id,
            query_weight=int(anchor["query_weight"]),
            behavior=str(anchor["behavior"]),
            inspect_hint=str(anchor["inspect_hint"]),
            file=file,
            start_line=line,
            end_line=line,
        )
        results.append(AnchorRunResult(anchor=anchor, matches=[match]))
    return AnchorScanResult(root=root.resolve(), anchor_results=results)


def test_final_validation_uses_real_file_admission_not_match_or_span_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug1.c").write_text("context();\nprimary();\n", encoding="utf-8")
    (source / "bug2.c").write_text("context();\nsupporting();\n", encoding="utf-8")
    (cases / "case1.c").write_text("primary();\n", encoding="utf-8")
    (cases / "case2.c").write_text("supporting();\n", encoding="utf-8")

    def fake_scan(anchors, root, _language):
        resolved = Path(root).resolve()
        if resolved == cases.resolve():
            locations = {"primary": ("case1.c", 1), "supporting": ("case2.c", 1)}
        else:
            # Both matches are in RCA-declared files but outside the exact line-2 spans.
            locations = {"primary": ("bug1.c", 1), "supporting": ("bug2.c", 1)}
        return _scan_result(resolved, anchors, locations)

    monkeypatch.setattr(anchor_validation, "scan_anchors", fake_scan)

    errors = anchor_validation.validate_anchors(
        _core(supporting_weight=2),
        source_root=source,
        cases_dir=cases,
        root_cause=_root_cause(),
        grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
    )
    assert any("case2.c" in error and "not admitted" in error for error in errors)
    assert any("bug2.c" in error and "not admitted" in error for error in errors)
    assert not any("no match in an RCA-declared source file" in error for error in errors)

    assert anchor_validation.validate_anchors(
        _core(supporting_weight=3),
        source_root=source,
        cases_dir=cases,
        root_cause=_root_cause(),
        grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
    ) == []


def test_disabled_anchor_does_not_relax_collective_case_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug1.c").write_text("context();\nprimary();\n", encoding="utf-8")
    (source / "bug2.c").write_text("context();\nsupporting();\n", encoding="utf-8")
    (cases / "case1.c").write_text("primary();\n", encoding="utf-8")
    (cases / "case2.c").write_text("supporting();\n", encoding="utf-8")

    complete = _core(supporting_weight=3)
    degraded = complete.model_copy(
        update={"anchors": [complete.anchors[0], complete.anchors[1].model_copy(update={"query": ""})]}
    )

    def fake_scan(anchors, root, _language):
        resolved = Path(root).resolve()
        file = "case1.c" if resolved == cases.resolve() else "bug1.c"
        match = AnchorMatch(
            anchor_id="primary",
            query_weight=3,
            behavior="Performs the primary operation.",
            inspect_hint="Inspect the supporting operation.",
            file=file,
            start_line=1,
            end_line=1,
        )
        primary = next(anchor for anchor in anchors if anchor["id"] == "primary")
        return AnchorScanResult(
            root=resolved,
            anchor_results=[AnchorRunResult(anchor=primary, matches=[match])],
        )

    monkeypatch.setattr(anchor_validation, "scan_anchors", fake_scan)

    errors = anchor_validation.validate_anchors(
        degraded,
        source_root=source,
        cases_dir=cases,
        root_cause=_root_cause(),
        grounding_policy=GroundingPolicy.BAD_SPAN_COVERAGE,
    )

    assert any("case2.c" in error and "not admitted" in error for error in errors)
