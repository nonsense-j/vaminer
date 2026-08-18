from pathlib import Path

from src.miner.mining.validation.analysis import (
    finalize_root_cause_cases,
    validate_root_cause_analysis,
)
from src.miner.models import AstGrepLanguage, BuggyComponent, RootCauseAnalysis


def _rca() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        language=AstGrepLanguage.C,
        root_cause_summary="unchecked copy length",
        analysis="A caller-controlled length reaches memcpy.",
        buggy_components=[
            BuggyComponent(file="bug.c", start_line=1, end_line=1, role="copy", snippet="copy();")
        ],
        fixing_pattern="bound the length",
        extracted_case_files=["case1.c"],
    )


def test_validation_is_pure_and_finalization_is_explicit(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_text("copy();\n", encoding="utf-8")
    (cases / "unexpected.c").write_text("extra\n", encoding="utf-8")
    nested = cases / "nested"
    nested.mkdir()
    (nested / "extra.c").write_text("extra\n", encoding="utf-8")

    errors = validate_root_cause_analysis(_rca(), source_root=source, cases_dir=cases)
    assert errors
    assert (cases / "unexpected.c").exists()
    assert nested.exists()

    finalize_root_cause_cases(_rca(), cases_dir=cases)
    assert {path.name for path in cases.iterdir()} == {"case1.c"}
    assert validate_root_cause_analysis(_rca(), source_root=source, cases_dir=cases) == []


def test_pure_validation_rejects_oversized_case_artifact(tmp_path: Path):
    source = tmp_path / "src"
    cases = tmp_path / "cases"
    source.mkdir()
    cases.mkdir()
    (source / "bug.c").write_text("copy();\n", encoding="utf-8")
    (cases / "case1.c").write_bytes(b"x" * (128 * 1024 + 1))
    errors = validate_root_cause_analysis(_rca(), source_root=source, cases_dir=cases)
    assert any("131072-byte limit" in error for error in errors)
