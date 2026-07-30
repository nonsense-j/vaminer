"""Git repository builders shared by operation and adapter tests."""

from __future__ import annotations

from pathlib import Path

from git import Actor, Repo


def prepare_history(repo_path: Path) -> tuple[str, str]:
    repo_path.mkdir()
    repo = Repo.init(repo_path)
    actor = Actor("VAS Test", "vas-test@example.com")
    source = repo_path / "bug.c"
    source.write_text("int value = 1;\n", encoding="utf-8")
    repo.index.add(["bug.c"])
    buggy = repo.index.commit("buggy", author=actor, committer=actor)
    source.write_text("int value = 2;\n", encoding="utf-8")
    repo.index.add(["bug.c"])
    fixed = repo.index.commit("fixed", author=actor, committer=actor)
    return buggy.hexsha, fixed.hexsha
