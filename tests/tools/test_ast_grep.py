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

    assert count == {
        "target_dir": target.as_posix(),
        "output": "count",
        "match_count": 2,
        "matched_file_count": 2,
    }
    assert sample["truncated"] is True
    assert sample["matches"][0]["file"] == "a.c"
    assert sample["matches"][0]["start"] == {"line": 2, "column": 3}
    assert [site["file"] for site in full["matches"]] == ["a.c", "b.c"]
    assert all("meta_variables" in site for site in full["matches"])


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
