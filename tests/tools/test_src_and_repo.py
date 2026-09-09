import inspect
import json
import subprocess
from pathlib import Path

import pytest

from src.miner.tools import repo as repo_module
from src.miner.tools import src as src_module
from src.miner.tools.repo import read_patch_diff_from_repo
from src.miner.tools.src import list_src_files, read_src_file, search_src_files


@pytest.mark.parametrize("path", [None, "", ".", "./", "src/.."])
def test_patch_diff_without_specific_path_is_a_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str | None,
):
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="1 file changed, 2 insertions(+), 1 deletion(-)\n", stderr="")

    monkeypatch.setattr(repo_module.subprocess, "run", run)

    result = read_patch_diff_from_repo(tmp_path, path)

    assert "1 file changed" in result
    assert commands == [
        ["git", "diff", "--no-ext-diff", "--stat", "--stat-count=100", "buggy", "fixed", "--"]
    ]


def test_patch_diff_with_path_is_the_full_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    commands: list[list[str]] = []
    relative_path = (Path("src") / "example.py").as_posix()

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="@@ -1 +1 @@\n-old\n+new\n", stderr="")

    monkeypatch.setattr(repo_module.subprocess, "run", run)

    result = read_patch_diff_from_repo(tmp_path, relative_path)

    assert "@@ -1 +1 @@" in result
    assert commands == [["git", "diff", "--no-ext-diff", "buggy", "fixed", "--", relative_path]]
    with pytest.raises(ValueError, match="must be relative"):
        read_patch_diff_from_repo(tmp_path, str(tmp_path / "src/example.py"))


def test_rg_listing_and_literal_search_are_scoped_and_compact(tmp_path: Path):
    src_dir = Path("src").as_posix()
    src_a = (Path("src") / "a.c").as_posix()
    src_b = (Path("src") / "b.py").as_posix()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_bytes(
        b"foo(1);\r\nfoo.bar();\r\nexact\r\n exact \r\n"
    )
    (tmp_path / "src" / "b.py").write_text("foo(2)\n", encoding="utf-8")
    listed = list_src_files(tmp_path, path=src_dir, glob="*.c")
    assert listed == src_a

    literal = search_src_files(tmp_path, "foo.", path=src_dir)
    assert literal == f"{src_a}-1-foo(1);\n{src_a}:2:foo.bar();\n{src_a}-3-exact"
    assert "context" not in inspect.signature(search_src_files).parameters
    single_file = search_src_files(tmp_path, r"foo\(\d\)", path=src_a, mode="regex")
    assert single_file == f"{src_a}:1:foo(1);\n{src_a}-2-foo.bar();"
    spaced = search_src_files(tmp_path, " exact ", path=src_a)
    assert spaced == f"{src_a}-3-exact\n{src_a}:4: exact "
    regex = search_src_files(tmp_path, r"foo\(\d\)", path=src_dir, mode="regex")
    assert regex == f"{src_a}:1:foo(1);\n{src_a}-2-foo.bar();\n--\n{src_b}:1:foo(2)"
    assert search_src_files(tmp_path, "absent") == "(no matches)"
    assert read_src_file(tmp_path, src_a, start_line=2, end_line=2) == (
        f"==> {src_a} | lines 2-2 of 4 | more available <==\nfoo.bar();"
    )

    truncated = list_src_files(tmp_path, path=src_dir, max_results=1)
    assert truncated == f"{src_a}\n-- truncated"
    with pytest.raises(ValueError, match="not a directory"):
        list_src_files(tmp_path, path=src_a)
    missing_scope = Path("src", "apache", "cassandra").as_posix()
    with pytest.raises(ValueError) as missing:
        list_src_files(tmp_path, path=missing_scope)
    assert str(missing.value) == (
        f"src path does not exist relative to bound root {tmp_path.resolve().as_posix()}: "
        f"{missing_scope}; nearest existing directory: {src_dir}"
    )

    read = read_src_file(tmp_path, src_a, start_line=1, end_line=1)
    assert read == f"==> {src_a} | lines 1-1 of 4 | more available <==\nfoo(1);"


def test_search_path_rejects_repeated_checkout_prefix(tmp_path: Path):
    root = tmp_path / "vas_ws" / "miner" / "VAS-test" / "src" / "apache" / "cassandra"
    repeated_scope = Path("src", "apache", "cassandra").as_posix()
    root.mkdir(parents=True)

    with pytest.raises(ValueError) as repeated:
        list_src_files(root, path=repeated_scope)

    assert str(repeated.value) == (
        f"src path repeats the bound Src Root: {repeated_scope}; "
        f"bound root: {root.resolve().as_posix()}; use '.' or omit path for the root"
    )


def test_src_read_caps_large_ranges_for_paging(tmp_path: Path):
    path = tmp_path / "large.txt"
    path.write_text("".join(f"line {line}\n" for line in range(1, 251)), encoding="utf-8")

    first = read_src_file(tmp_path, "large.txt", start_line=1, end_line=250)
    assert first.startswith("==> large.txt | lines 1-200 of 250 | more available <==\n")
    assert len(first.splitlines()[1:]) == 200

    second = read_src_file(tmp_path, "large.txt", start_line=201, end_line=250)
    assert second.startswith("==> large.txt | lines 201-250 of 250 <==\n")
    with pytest.raises(ValueError, match="end_line"):
        read_src_file(tmp_path, "large.txt", start_line=10, end_line=9)

    complete = read_src_file(tmp_path, "large.txt", full_file=True)
    assert complete.startswith("==> large.txt | lines 1-250 of 250 <==\n")
    with pytest.raises(ValueError, match="full_file cannot be combined"):
        read_src_file(tmp_path, "large.txt", end_line=250, full_file=True)

    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (src_module.MAX_SRC_READ_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        read_src_file(tmp_path, "oversized.txt", full_file=True)


def test_src_read_past_eof_returns_recovery_information(tmp_path: Path):
    path = tmp_path / "short.txt"
    path.write_text("line 1\nline 2\n", encoding="utf-8")

    assert read_src_file(tmp_path, "short.txt", start_line=120, end_line=160) == (
        "==> short.txt | requested line 120; EOF at 2 <==\n"
        "-- start_line 120 is past EOF; short.txt has 2 lines; no content was returned"
    )

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert read_src_file(tmp_path, "empty.txt") == "==> empty.txt | empty <=="


def test_rg_tools_reject_invalid_scope_and_pattern(tmp_path: Path):
    (tmp_path / "src").mkdir()
    outside = tmp_path.parent / "outside-search"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="stay inside"):
        search_src_files(tmp_path, "x", path="../outside-search")
    with pytest.raises(RuntimeError, match="regex parse error"):
        search_src_files(tmp_path, "[", mode="regex")
    with pytest.raises(ValueError, match="search pattern must be a string"):
        search_src_files(tmp_path, None)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError(), "requires rg on PATH"),
        (subprocess.TimeoutExpired(["rg"], 20), "timed out"),
    ],
)
def test_rg_process_failures_are_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    message: str,
):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(src_module.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match=message):
        search_src_files(tmp_path, "x")


def test_rg_output_and_stderr_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    event = json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": str(tmp_path / "a.c")},
                "line_number": 7,
                "lines": {"text": "needle\n"},
            },
        }
    )
    huge = event + "\n" + "x" * src_module.MAX_SRC_SEARCH_BYTES
    monkeypatch.setattr(
        src_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=huge, stderr=""),
    )
    result = search_src_files(tmp_path, "needle")
    assert result.startswith("a.c:7:needle\n-- truncated: src search output exceeded")

    listed = list_src_files(tmp_path)
    assert "-- truncated: src file listing exceeded" in listed

    monkeypatch.setattr(
        src_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, stdout="", stderr="e" * 9_000),
    )
    with pytest.raises(RuntimeError) as error:
        search_src_files(tmp_path, "x")
    assert len(str(error.value)) == src_module.MAX_SRC_ERROR_CHARS


def test_patch_diff_output_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    huge = "x" * (repo_module.MAX_REPO_DIFF_BYTES + 1)
    monkeypatch.setattr(
        repo_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=huge, stderr=""),
    )

    with pytest.raises(ValueError, match="use a narrower path"):
        read_patch_diff_from_repo(tmp_path, "src/large.c")
