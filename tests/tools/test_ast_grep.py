"""Tests for the ast-grep skill-owned runner."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def test_runner_rejects_invalid_target_and_sample_size(tmp_path: Path):
    runner = _load_runner()
    with pytest.raises(runner.AstGrepRunnerError, match="does not exist"):
        runner.run_ast_grep(
            tmp_path / "missing",
            language="c",
            query_type="pattern",
            query="danger($ARG);",
        )
    with pytest.raises(runner.AstGrepRunnerError, match="positive"):
        runner.run_ast_grep(
            tmp_path,
            language="c",
            query_type="pattern",
            query="danger($ARG);",
            sample_size=0,
        )


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


def test_runner_cli_emits_normalized_json(tmp_path: Path):
    if shutil.which("ast-grep") is None and shutil.which("sg") is None:
        pytest.skip("ast-grep is required")
    target = tmp_path / "target"
    target.mkdir()
    (target / "sample.c").write_text("void f(void) { danger(1); }\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            str(target),
            "--language",
            "c",
            "--query-type",
            "pattern",
            "--query",
            "danger($ARG);",
            "--output",
            "count",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout)["match_count"] == 1
