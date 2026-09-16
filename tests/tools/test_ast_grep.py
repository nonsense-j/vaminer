"""Tests for the two runtime-neutral ast-grep operations."""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.miner.tools import ast_grep
from src.miner.tools.ast_grep import AstGrepUnavailableError, debug_pattern, run_query
from src.miner.tools.errors import ToolExecutionError, ToolInputError


def _native_ast_grep() -> str:
    executable = shutil.which("ast-grep") or shutil.which("sg")
    if executable is None:
        pytest.skip("ast-grep is required")
    return executable


def test_run_query_normalizes_results(tmp_path: Path):
    executable = _native_ast_grep()
    (tmp_path / "a.c").write_text("void a(void) { danger(1); }\n", encoding="utf-8")
    (tmp_path / "b.c").write_text("void b(void) { danger(2); }\n", encoding="utf-8")

    count = run_query(
        tmp_path,
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="count",
        executable=executable,
    )
    sample = run_query(
        tmp_path,
        language="c",
        query_type="rule",
        query="rule:\n  pattern: danger($ARG);",
        output="sample",
        sample_size=1,
        executable=executable,
    )
    full = run_query(
        tmp_path,
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="full",
        executable=executable,
    )

    assert count == "matches: 2\nmatched files: 2"
    assert "==> a.c:1:16-1:26 <==" in sample
    assert "==> b.c:" not in sample
    assert sample.endswith("-- truncated")
    assert "==> a.c:1:16-1:26 <==" in full
    assert "==> b.c:1:16-1:26 <==" in full
    assert "capture single.ARG: 1" in full
    assert "capture single.ARG: 2" in full


def test_debug_pattern_is_independent_from_query_execution(tmp_path: Path):
    executable = _native_ast_grep()
    (tmp_path / "a.c").write_text(
        "void f(char *d, char *s, int n) { memcpy(d, s, n); }\n",
        encoding="utf-8",
    )

    bare = debug_pattern(
        language="c",
        pattern="memcpy($$$ARGS)",
        debug_query="pattern",
        executable=executable,
        working_dir=tmp_path,
    )
    statement = debug_pattern(
        language="c",
        pattern="memcpy($$$ARGS);",
        debug_query="pattern",
        executable=executable,
        working_dir=tmp_path,
    )

    assert "Debug Pattern:\nmacro_type_specifier" in bare
    assert "Debug Pattern:\nexpression_statement\n  call_expression" in statement
    assert "debug_query" not in inspect.signature(run_query).parameters
    assert "query_type" not in inspect.signature(debug_pattern).parameters
    assert ast_grep.__all__ == ["debug_pattern", "run_query"]


@pytest.mark.parametrize(
    ("resolved", "message"),
    [
        (None, "was not found on PATH"),
        (r"C:\\Users\\test\\ast-grep.CMD", ".cmd shim"),
    ],
)
def test_only_unavailable_or_cmd_cli_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved: str | None,
    message: str,
):
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: resolved)

    with pytest.raises(AstGrepUnavailableError, match=message):
        run_query(
            tmp_path,
            language="c",
            query_type="pattern",
            query="danger($A);",
        )


@pytest.mark.parametrize(
    ("function", "arguments", "message"),
    [
        (run_query, {"language": None, "query_type": "pattern", "query": "x"}, "language must be"),
        (run_query, {"language": "c", "query_type": [], "query": "x"}, "unsupported query type"),
        (run_query, {"language": "c", "query_type": "pattern", "query": None}, "query must be"),
        (
            run_query,
            {"language": "c", "query_type": "pattern", "query": "x", "output": {}},
            "unsupported output mode",
        ),
        (
            run_query,
            {"language": "c", "query_type": "pattern", "query": "x", "sample_size": True},
            "sample_size must be",
        ),
        (debug_pattern, {"language": "c", "pattern": None}, "pattern must be"),
        (
            debug_pattern,
            {"language": "c", "pattern": "x", "debug_query": "tree"},
            "unsupported debug-query format",
        ),
    ],
)
def test_agent_correctable_arguments_are_tool_feedback(
    tmp_path: Path,
    function,
    arguments: dict[str, object],
    message: str,
):
    with pytest.raises(ToolInputError, match=message):
        if function is run_query:
            function(tmp_path, executable=_native_ast_grep(), **arguments)
        else:
            function(executable=_native_ast_grep(), working_dir=tmp_path, **arguments)


def test_debug_pattern_uses_empty_temporary_directory_when_cases_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    preferred = tmp_path / "missing-cases"
    observed_roots: list[Path] = []
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: "/tools/ast-grep")

    def run(command, **kwargs):
        root = Path(kwargs["cwd"])
        observed_roots.append(root)
        assert root.is_dir()
        assert list(root.iterdir()) == []
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="Debug Pattern:\n(identifier)\n")

    monkeypatch.setattr(ast_grep.subprocess, "run", run)

    result = debug_pattern(
        language="c",
        pattern="$A",
        executable="ast-grep",
        working_dir=preferred,
    )

    assert result == "Debug Pattern:\n(identifier)\n"
    assert preferred.exists() is False
    assert len(observed_roots) == 1
    assert observed_roots[0].exists() is False


def test_nonzero_exit_forwards_native_stderr_as_tool_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    raw_stderr = "fatal: worker terminated unexpectedly\n"
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: "/tools/ast-grep")
    monkeypatch.setattr(
        ast_grep.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            8,
            stdout="partial output",
            stderr=raw_stderr,
        ),
    )

    with pytest.raises(ToolExecutionError) as raised:
        run_query(
            tmp_path,
            language="c",
            query_type="pattern",
            query="danger($A);",
            executable="ast-grep",
        )

    assert type(raised.value) is ToolExecutionError
    assert str(raised.value) == raw_stderr


def test_timeout_forwards_partial_native_stderr_as_tool_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    raw_stderr = b"native partial diagnostic\n"
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: "/tools/ast-grep")

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=3, output=b"[", stderr=raw_stderr)

    monkeypatch.setattr(ast_grep.subprocess, "run", timeout)
    with pytest.raises(ToolExecutionError) as raised:
        run_query(
            tmp_path,
            language="c",
            query_type="pattern",
            query="danger($A);",
            timeout_seconds=3,
            executable="ast-grep",
        )

    assert str(raised.value) == raw_stderr.decode()


def test_invalid_native_output_is_tool_feedback_and_preserves_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    raw_stderr = "native warning\n"
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: "/tools/ast-grep")
    monkeypatch.setattr(
        ast_grep.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="{not-json",
            stderr=raw_stderr,
        ),
    )

    with pytest.raises(ToolExecutionError) as raised:
        run_query(
            tmp_path,
            language="c",
            query_type="pattern",
            query="danger($A);",
            executable="ast-grep",
        )

    assert "ast-grep returned invalid JSON" in str(raised.value)
    assert raw_stderr in str(raised.value)
    assert "{not-json" in str(raised.value)


def test_pattern_warning_is_feedback_but_debug_tree_text_is_not_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    outputs = iter(
        [
            "Warning: Pattern contains an ERROR node.\nrepair this exact diagnostic\n",
            "Debug Pattern:\nstring_literal\n  string_content invalid pattern\n",
        ]
    )
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: "/tools/ast-grep")
    monkeypatch.setattr(
        ast_grep.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="[]",
            stderr=next(outputs),
        ),
    )

    with pytest.raises(ToolExecutionError, match="ERROR node"):
        run_query(
            tmp_path,
            language="c",
            query_type="pattern",
            query="danger(",
            executable="ast-grep",
        )

    result = debug_pattern(
        language="c",
        pattern='puts("invalid pattern");',
        executable="ast-grep",
        working_dir=tmp_path,
    )
    assert result == "Debug Pattern:\nstring_literal\n  string_content invalid pattern\n"


def test_run_query_and_debug_pattern_build_separate_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    commands: list[list[str]] = []
    stderrs = iter(("", "Debug CST:\n(tree)\n"))
    monkeypatch.setattr(ast_grep.shutil, "which", lambda _name: "/tools/ast-grep")

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps([]), stderr=next(stderrs))

    monkeypatch.setattr(ast_grep.subprocess, "run", run)
    assert run_query(
        tmp_path,
        language="c",
        query_type="pattern",
        query="copy($A);",
        executable="ast-grep",
    ).startswith("matches: 0")
    assert debug_pattern(
        language="c",
        pattern="copy($A);",
        debug_query="cst",
        executable="ast-grep",
        working_dir=tmp_path,
    ) == "Debug CST:\n(tree)\n"

    assert all(not argument.startswith("--debug-query") for argument in commands[0])
    assert "--debug-query=cst" in commands[1]
