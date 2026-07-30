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

    assert result == {"path": "case1.c", "bytes_written": 18}
    assert list_case_artifacts(cases) == ["case1.c", "case1_var1.c"]
    assert read_case_artifact(
        cases,
        "case1.c",
        start_line=2,
        end_line=3,
    ) == {
        "path": "case1.c",
        "content": "line2\nline3\n",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 3,
        "truncated": False,
    }

    with pytest.raises(ValueError, match="bare filename"):
        write_case_artifact(cases, "nested/case2.c", "bad\n")
    with pytest.raises(ValueError, match="caseN"):
        write_case_artifact(cases, "notes.c", "bad\n")
    with pytest.raises(ValueError, match="non-empty"):
        write_case_artifact(cases, "case2.c", " \n")
