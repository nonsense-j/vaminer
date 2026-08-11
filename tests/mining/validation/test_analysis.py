"""Root-cause analysis validation tests."""

from __future__ import annotations

from src.miner.mining.validation.analysis import validate_root_cause_analysis
from tests.support.factories import SOURCE, root_cause


def test_root_cause_snippet_does_not_require_exact_source_substring(tmp_path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")

    analysis = root_cause()
    component = analysis.buggy_components[0].model_copy(
        update={"snippet": "\tvoid trigger(void) { danger(1); }"}
    )
    analysis = analysis.model_copy(update={"buggy_components": [component]})

    assert validate_root_cause_analysis(
        analysis,
        source_root=source_root,
        cases_dir=cases_dir,
    ) == []


def test_root_cause_validation_deletes_undeclared_case_files(tmp_path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.txt").write_text("stale attempt\n", encoding="utf-8")
    nested = cases_dir / "scratch"
    nested.mkdir()
    (nested / "notes.txt").write_text("temporary\n", encoding="utf-8")

    assert validate_root_cause_analysis(
        root_cause(),
        source_root=source_root,
        cases_dir=cases_dir,
    ) == []
    assert [path.name for path in cases_dir.iterdir()] == ["case1.c"]
