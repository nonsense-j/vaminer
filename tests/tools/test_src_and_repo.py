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

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="@@ -1 +1 @@\n-old\n+new\n", stderr="")

    monkeypatch.setattr(repo_module.subprocess, "run", run)

    result = read_patch_diff_from_repo(tmp_path, "src/example.py")

    assert "@@ -1 +1 @@" in result
    assert commands == [["git", "diff", "--no-ext-diff", "buggy", "fixed", "--", "src/example.py"]]
    with pytest.raises(ValueError, match="must be relative"):
        read_patch_diff_from_repo(tmp_path, str(tmp_path / "src/example.py"))


def test_rg_listing_and_literal_search_are_scoped_and_compact(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("foo(1);\nfoo.bar();\nexact\n exact \n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("foo(2)\n", encoding="utf-8")
    listed = list_src_files(tmp_path, path="src", glob="*.c")
    assert listed == {"files": ["src/a.c"], "truncated": False}

    literal = search_src_files(tmp_path, "foo.", path="src")
    assert [(item["file"], item["line"]) for item in literal["matches"]] == [("src/a.c", 2)]
    single_file = search_src_files(tmp_path, r"foo\(\d\)", path="src/a.c", mode="regex")
    assert [(item["file"], item["line"]) for item in single_file["matches"]] == [("src/a.c", 1)]
    spaced = search_src_files(tmp_path, " exact ", path="src/a.c")
    assert [(item["file"], item["line"]) for item in spaced["matches"]] == [("src/a.c", 4)]
    regex = search_src_files(tmp_path, r"foo\(\d\)", path="src", mode="regex")
    assert len(regex["matches"]) == 2
    assert search_src_files(tmp_path, "absent")["matches"] == []
    assert read_src_file(tmp_path, "src/a.c", start_line=2, end_line=2)["content"] == "foo.bar();\n"

    truncated = list_src_files(tmp_path, path="src", max_results=1)
    assert truncated["files"] == ["src/a.c"]
    assert truncated["truncated"] is True
    with pytest.raises(ValueError, match="not a directory"):
        list_src_files(tmp_path, path="src/a.c")
    with pytest.raises(ValueError) as missing:
        list_src_files(tmp_path, path="src/apache/cassandra")
    assert str(missing.value) == (
        f"src path does not exist relative to bound root {tmp_path}: src/apache/cassandra; "
        "nearest existing directory: src"
    )

    read = read_src_file(tmp_path, "src/a.c", start_line=1, end_line=1)
    assert (read["total_lines"], read["end_line"], read["truncated"]) == (4, 1, True)


def test_search_path_rejects_repeated_checkout_prefix(tmp_path: Path):
    root = tmp_path / "vas_ws" / "miner" / "VAS-test" / "src" / "apache" / "cassandra"
    root.mkdir(parents=True)

    with pytest.raises(ValueError) as repeated:
        list_src_files(root, path="src/apache/cassandra")

    assert str(repeated.value) == (
        "src path repeats the bound Src Root: src/apache/cassandra; "
        f"bound root: {root}; use '.' or omit path for the root"
    )


def test_src_read_caps_large_ranges_for_paging(tmp_path: Path):
    path = tmp_path / "large.txt"
    path.write_text("".join(f"line {line}\n" for line in range(1, 251)), encoding="utf-8")

    first = read_src_file(tmp_path, "large.txt", start_line=1, end_line=250)
    assert (first["start_line"], first["end_line"], first["total_lines"], first["truncated"]) == (
        1,
        200,
        250,
        True,
    )
    assert len(str(first["content"]).splitlines()) == 200

    second = read_src_file(tmp_path, "large.txt", start_line=201, end_line=250)
    assert (second["start_line"], second["end_line"], second["truncated"]) == (201, 250, False)
    with pytest.raises(ValueError, match="end_line"):
        read_src_file(tmp_path, "large.txt", start_line=10, end_line=9)

    complete = read_src_file(tmp_path, "large.txt", full_file=True)
    assert (complete["start_line"], complete["end_line"], complete["truncated"]) == (1, 250, False)
    with pytest.raises(ValueError, match="full_file cannot be combined"):
        read_src_file(tmp_path, "large.txt", end_line=250, full_file=True)

    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (src_module.MAX_SRC_READ_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        read_src_file(tmp_path, "oversized.txt", full_file=True)


def test_src_read_past_eof_returns_recovery_information(tmp_path: Path):
    path = tmp_path / "short.txt"
    path.write_text("line 1\nline 2\n", encoding="utf-8")

    assert read_src_file(tmp_path, "short.txt", start_line=120, end_line=160) == {
        "path": "short.txt",
        "content": "",
        "start_line": 120,
        "end_line": 2,
        "total_lines": 2,
        "truncated": False,
        "message": "start_line 120 is past EOF; short.txt has 2 lines; no content was returned",
    }

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert read_src_file(tmp_path, "empty.txt") == {
        "path": "empty.txt",
        "content": "",
        "start_line": 1,
        "end_line": 0,
        "total_lines": 0,
        "truncated": False,
    }


def test_rg_tools_reject_escape_and_symlink(tmp_path: Path):
    (tmp_path / "src").mkdir()
    outside = tmp_path.parent / "outside-search"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="stay inside"):
        search_src_files(tmp_path, "x", path="../outside-search")
    with pytest.raises(ValueError, match="symbolic links"):
        list_src_files(tmp_path, path="link")
    (tmp_path / "inside").mkdir()
    (tmp_path / "inside" / "target.c").write_text("x\n", encoding="utf-8")
    (tmp_path / "internal-link").symlink_to(tmp_path / "inside", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        read_src_file(tmp_path, "internal-link/target.c")
    with pytest.raises(RuntimeError, match="regex parse error"):
        search_src_files(tmp_path, "[", mode="regex")


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
    assert result["matches"] == [{"file": "a.c", "line": 7, "text": "needle"}]
    assert result["truncated"] is True
    assert "output exceeded" in str(result["message"])

    listed = list_src_files(tmp_path)
    assert listed["truncated"] is True
    assert "listing exceeded" in str(listed["message"])

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
