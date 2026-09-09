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

from src.miner.tools import ast_grep as ast_grep_module
from src.miner.tools.ast_grep import AstGrepQueryError, AstGrepRunnerError

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


def test_debug_query_exposes_c_pattern_root_before_acceptance(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    target = tmp_path / "target"
    target.mkdir()
    (target / "a.c").write_text(
        "void f(char *d, char *s, int n) { memcpy(d, s, n); }\n",
        encoding="utf-8",
    )
    runner = _load_runner()

    bare = runner.run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="memcpy($$$ARGS)",
        output="count",
        debug_query="pattern",
    )
    statement = runner.run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="memcpy($$$ARGS);",
        output="count",
        debug_query="pattern",
    )

    assert bare.startswith("matches: 0\nmatched files: 0")
    assert "Debug Pattern:\nmacro_type_specifier" in bare
    assert statement.startswith("matches: 1\nmatched files: 1")
    assert "Debug Pattern:\nexpression_statement\n  call_expression" in statement


def test_runner_classifies_regex_rule_without_kind_as_repairable(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    target = tmp_path / "target"
    target.mkdir()
    (target / "a.c").write_text("void f(void) { danger(); }\n", encoding="utf-8")
    runner = _load_runner()

    with pytest.raises(AstGrepQueryError) as raised:
        runner.run_ast_grep(
            target,
            language="c",
            query_type="rule",
            query="regex: ^danger$",
        )

    assert "Rule must specify a set of AST kinds" in raised.value.stderr
    assert str(raised.value) == raised.value.stderr
    assert raised.value.returncode not in {0, 1}
    assert runner.run_ast_grep(
        target,
        language="c",
        query_type="rule",
        query="kind: identifier\nregex: ^danger$",
        output="count",
    ) == "matches: 1\nmatched files: 1"


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


@pytest.mark.parametrize(
    ("overrides", "error_type", "message"),
    [
        ({"language": None}, AstGrepQueryError, "language must be a non-empty string"),
        ({"query_type": []}, AstGrepQueryError, "unsupported query type"),
        ({"output": {}}, AstGrepQueryError, "unsupported output mode"),
        ({"sample_size": True}, AstGrepQueryError, "sample_size must be a positive integer"),
        ({"debug_query": "tree"}, AstGrepQueryError, "unsupported debug-query format"),
        ({"timeout_seconds": 0}, AstGrepRunnerError, "timeout_seconds must be a positive integer"),
    ],
)
def test_runner_validates_public_arguments_before_process_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    error_type: type[Exception],
    message: str,
):
    target = tmp_path / "target"
    target.mkdir()

    def unexpected_run(*_args, **_kwargs):
        pytest.fail("ast-grep must not run for an invalid public argument")

    monkeypatch.setattr(ast_grep_module.subprocess, "run", unexpected_run)
    arguments = {
        "language": "c",
        "query_type": "pattern",
        "query": "danger($A);",
        "output": "sample",
        "sample_size": 20,
        "debug_query": None,
        "timeout_seconds": 60,
    }
    arguments.update(overrides)

    with pytest.raises(error_type, match=message):
        _load_runner().run_ast_grep(target, executable="ast-grep", **arguments)


def test_runner_reports_missing_captured_output_as_runner_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        ast_grep_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=None,
            stderr=None,
        ),
    )

    with pytest.raises(
        AstGrepRunnerError,
        match=r"did not provide captured stdout and stderr \(exit code 0\)",
    ):
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger($A);",
            executable="ast-grep",
        )


def test_runner_forwards_verbatim_stderr_and_debug_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    commands: list[list[str]] = []
    raw_stderr = "Debug Sexp:\n(translation_unit (macro_type_specifier))\n"

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="[]",
            stderr=raw_stderr,
        )

    monkeypatch.setattr(ast_grep_module.subprocess, "run", run)
    result = _load_runner().run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query="memcpy($$$ARGS)",
        output="count",
        debug_query="sexp",
        executable="ast-grep",
    )

    assert "--debug-query=sexp" in commands[0]
    assert result == (
        "matches: 0\nmatched files: 0\n\n"
        "ast-grep stderr (verbatim):\n"
        + raw_stderr
    )


def test_runner_query_error_keeps_raw_stderr_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    raw_stderr = "Warning: Pattern contains an ERROR node.\nrepair this exact diagnostic\n"
    monkeypatch.setattr(
        ast_grep_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="[]",
            stderr=raw_stderr,
        ),
    )

    with pytest.raises(AstGrepQueryError) as raised:
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger(",
            executable="ast-grep",
        )

    assert str(raised.value) == raw_stderr
    assert raised.value.stderr == raw_stderr
    assert raised.value.returncode == 0


def test_debug_tree_text_does_not_trigger_a_false_query_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    raw_stderr = (
        "Debug Pattern:\n"
        "string_literal\n"
        "  string_content invalid pattern\n"
    )
    monkeypatch.setattr(
        ast_grep_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="[]",
            stderr=raw_stderr,
        ),
    )

    result = _load_runner().run_ast_grep(
        target,
        language="c",
        query_type="pattern",
        query='puts("invalid pattern");',
        output="count",
        debug_query="pattern",
        executable="ast-grep",
    )

    assert result.endswith(raw_stderr)


def test_runner_keeps_stderr_when_json_or_captures_are_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    raw_stderr = "Debug Sexp:\n(translation_unit)\n"
    outputs = iter(
        [
            "{not-json",
            json.dumps(
                [
                    {
                        "file": "a.c",
                        "text": "danger();",
                        "range": {
                            "start": {"line": 0, "column": 0},
                            "end": {"line": 0, "column": 9},
                        },
                        "metaVariables": {"single": {"ARG": {"range": {}}}},
                    }
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        ast_grep_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=next(outputs),
            stderr=raw_stderr,
        ),
    )
    runner = _load_runner()

    with pytest.raises(AstGrepRunnerError) as invalid_json:
        runner.run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger();",
            executable="ast-grep",
        )
    assert str(invalid_json.value).startswith("ast-grep returned invalid JSON:")
    assert str(invalid_json.value).endswith(raw_stderr)
    assert invalid_json.value.stderr == raw_stderr
    assert invalid_json.value.stdout == "{not-json"
    assert invalid_json.value.returncode == 0

    with pytest.raises(AstGrepRunnerError) as malformed_capture:
        runner.run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger($ARG);",
            output="full",
            executable="ast-grep",
        )
    assert str(malformed_capture.value).startswith(
        "ast-grep returned malformed metavariable captures"
    )
    assert str(malformed_capture.value).endswith(raw_stderr)
    assert malformed_capture.value.stderr == raw_stderr
    assert malformed_capture.value.returncode == 0


def test_runner_preserves_partial_stderr_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    raw_stderr = b"Debug Sexp:\n(partial query tree)\n"

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(
            command,
            timeout=3,
            output=b"[",
            stderr=raw_stderr,
        )

    monkeypatch.setattr(ast_grep_module.subprocess, "run", timeout)
    with pytest.raises(AstGrepRunnerError) as raised:
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger($A);",
            timeout_seconds=3,
            executable="ast-grep",
        )

    decoded_stderr = raw_stderr.decode()
    assert str(raised.value) == (
        "ast-grep timed out after 3 seconds\n\n"
        "ast-grep stderr (verbatim):\n"
        + decoded_stderr
    )
    assert raised.value.stderr == decoded_stderr
    assert raised.value.stdout == "["
    assert raised.value.returncode is None


def test_runner_distinguishes_non_query_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    raw_stderr = "fatal: worker terminated unexpectedly\n"
    monkeypatch.setattr(
        ast_grep_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            args=command,
            returncode=2,
            stdout="partial output",
            stderr=raw_stderr,
        ),
    )

    with pytest.raises(AstGrepRunnerError) as raised:
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="pattern",
            query="danger($A);",
            executable="ast-grep",
        )

    assert type(raised.value) is AstGrepRunnerError
    assert str(raised.value) == raw_stderr
    assert raised.value.stderr == raw_stderr
    assert raised.value.stdout == "partial output"
    assert raised.value.returncode == 2


def test_runner_rejects_debug_query_for_rule_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "target"
    target.mkdir()
    called = False

    def run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(ast_grep_module.subprocess, "run", run)
    with pytest.raises(AstGrepQueryError, match="only for pattern queries"):
        _load_runner().run_ast_grep(
            target,
            language="c",
            query_type="rule",
            query="rule:\n  kind: call_expression",
            debug_query="cst",
            executable="ast-grep",
        )

    assert called is False


def test_skill_cli_exposes_debug_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    target = tmp_path / "target"
    target.mkdir()
    captured: dict[str, object] = {}

    def run(target_dir, **kwargs):
        captured["target_dir"] = target_dir
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(ast_grep_module, "run_ast_grep", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUNNER_PATH),
            str(target),
            "--language",
            "c",
            "--query-type",
            "pattern",
            "--query",
            "danger($A);",
            "--debug-query",
        ],
    )
    runner = _load_runner()
    runner.main()

    assert runner.AstGrepQueryError is AstGrepQueryError
    assert captured["target_dir"] == str(target)
    assert captured["debug_query"] == "pattern"
    assert capsys.readouterr().out == "ok\n"
