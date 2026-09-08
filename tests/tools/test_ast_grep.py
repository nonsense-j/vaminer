"""Tests for the ast-grep skill-owned runner."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from src.miner.tools.ast_grep import AstGrepQueryError

RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "miner"
    / "skills"
    / "ast-grep"
    / "scripts"
    / "runner.py"
)


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vaminer_ast_grep_skill_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_normalizes_directory_results(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    target = tmp_path / "arbitrary"
    target.mkdir()
    (target / "a.c").write_text(
        "void a(void) {\n  danger(1);\n}\n",
        encoding="utf-8",
    )
    (target / "b.c").write_text(
        "void b(void) {\n  danger(2);\n}\n",
        encoding="utf-8",
    )
    runner = _load_runner()

    count = runner.run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="count",
    )
    sample = runner.run_ast_grep(
        target,
        language="c",
        query_type="rule",
        query="rule:\n  pattern: danger($ARG);",
        output="sample",
        sample_size=1,
    )
    full = runner.run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="full",
    )

    assert count == "matches: 2\nmatched files: 2"
    assert "==> a.c:2:3-2:13 <==" in sample
    assert "==> b.c:" not in sample
    assert sample.endswith("-- truncated")
    assert "==> a.c:2:3-2:13 <==" in full
    assert "==> b.c:2:3-2:13 <==" in full
    assert "capture single.ARG: 1" in full
    assert "capture single.ARG: 2" in full


def test_runner_classifies_model_authored_invalid_pattern_as_repairable(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    target = tmp_path / "target"
    target.mkdir()
    (target / "a.c").write_text("void a(void) {}\n", encoding="utf-8")

    with pytest.raises(AstGrepQueryError, match="ERROR node"):
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger(",
        )


def test_runner_rejects_null_query_before_process_execution(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(AstGrepQueryError, match="query must be a non-empty string"):
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query=None,
        )
