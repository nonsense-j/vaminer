"""Tests for bounded case-artifact operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.miner.tools.cases import (
    list_case_artifacts,
    read_case_artifact,
    write_case_artifact,
)


def test_case_artifacts_are_bounded_and_top_level_only(tmp_path: Path):
    cases = tmp_path / "cases"
    cases.mkdir()

    result = write_case_artifact(cases, "case1.c", "line1\nline2\nline3\n")
    write_case_artifact(cases, "case1_var1.c", "variant\n")
    (cases / "notes.txt").write_text("not a case", encoding="utf-8")

    assert result == "wrote case1.c (18 bytes)"
    assert list_case_artifacts(cases) == "case1.c\ncase1_var1.c"
    assert read_case_artifact(
        cases,
        "case1.c",
        start_line=2,
        end_line=3,
    ) == "==> case1.c | lines 2-3 of 3 <==\nline2\nline3"

    with pytest.raises(ValueError, match="bare filename"):
        write_case_artifact(cases, "nested/case2.c", "bad\n")
    with pytest.raises(ValueError, match="caseN"):
        write_case_artifact(cases, "notes.c", "bad\n")
    with pytest.raises(ValueError, match="non-empty"):
        write_case_artifact(cases, "case2.c", " \n")


def test_case_read_past_eof_returns_recovery_information(tmp_path: Path):
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "case1.c").write_text("line1\nline2\n", encoding="utf-8")

    assert read_case_artifact(cases, "case1.c", start_line=120, end_line=160) == (
        "==> case1.c | requested line 120; EOF at 2 <==\n"
        "-- start_line 120 is past EOF; case1.c has 2 lines; no content was returned"
    )
