import subprocess
from pathlib import Path

import pytest

from src.miner.tools import repo as repo_module
from src.miner.tools.repo import read_patch_diff_from_repo


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


def test_patch_diff_output_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    huge = "x" * (repo_module.MAX_REPO_DIFF_BYTES + 1)
    monkeypatch.setattr(
        repo_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=huge, stderr=""),
    )

    with pytest.raises(ValueError, match="use a narrower path"):
        read_patch_diff_from_repo(tmp_path, "src/large.c")
