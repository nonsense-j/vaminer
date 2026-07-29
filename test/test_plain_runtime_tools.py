"""Tests for runtime-neutral repository, case, and ast-grep operations."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from git import Actor, Repo

from src.miner.core.context import MinerContext
from src.miner.tools.ast_grep import run_ast_grep, run_ast_grep_query
from src.miner.tools.cases import (
    list_case_artifacts,
    read_case_artifact,
    write_case_artifact,
)
from src.miner.tools.repo import (
    clone_repo,
    clone_repository,
    read_fixed_diff,
    read_fixed_diff_from_repo,
    read_repository_file,
    search_repository_files,
)
from src.miner.tools.skills import list_skill_resources, read_skill_resource
from src.miner.utils.models import QueryType


def _prepare_history(repo_path: Path) -> tuple[str, str]:
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


def test_plain_repository_operations_and_pydantic_diff_wrapper(tmp_path: Path):
    upstream = tmp_path / "upstream"
    buggy_sha, fixed_sha = _prepare_history(upstream)
    workspace = tmp_path / "workspace"

    checkout = clone_repository(
        workspace,
        upstream.as_posix(),
        buggy_sha,
        fixed_sha,
        github_mirror_enabled=False,
    )
    checkout_path = Path(checkout.repo_path)
    cloned = Repo(checkout_path)

    assert cloned.head.commit.hexsha == buggy_sha
    assert cloned.commit("fixed").hexsha == fixed_sha
    assert "-int value = 1;" in read_fixed_diff_from_repo(checkout_path, "bug.c")
    assert "+int value = 2;" in read_fixed_diff_from_repo(checkout_path, "bug.c")

    context = SimpleNamespace(
        deps=MinerContext(
            workspace_root=workspace,
            repo_path=checkout_path,
        )
    )
    assert read_fixed_diff(context) == read_fixed_diff_from_repo(checkout_path)
    with pytest.raises(ValueError, match="must stay inside"):
        read_fixed_diff_from_repo(checkout_path, "../outside.c")

    wrapper_context = SimpleNamespace(
        deps=MinerContext(workspace_root=tmp_path / "wrapper-workspace")
    )
    assert (
        Repo(
            clone_repo(
                wrapper_context,
                upstream.as_posix(),
                buggy_sha,
                fixed_sha,
            ).repo_path
        ).head.commit.hexsha
        == buggy_sha
    )


def test_case_artifacts_are_bounded_and_top_level_only(tmp_path: Path):
    cases = tmp_path / "cases"
    cases.mkdir()

    result = write_case_artifact(cases, "case1.c", "line1\nline2\nline3\n")
    write_case_artifact(cases, "case1_var1.c", "variant\n")
    (cases / "notes.txt").write_text("not a case", encoding="utf-8")

    assert result == {"path": "case1.c", "bytes_written": 18}
    assert list_case_artifacts(cases) == ["case1.c", "case1_var1.c"]
    assert read_case_artifact(cases, "case1.c", start_line=2, end_line=3) == {
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


def test_bounded_repository_navigation_rejects_escape_and_caps_results(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.c").write_text(
        "int guard = 1;\nvoid f(void) { guard++; }\nint other = guard;\n",
        encoding="utf-8",
    )

    assert read_repository_file(repo, "src.c", start_line=2, end_line=2)["content"] == (
        "void f(void) { guard++; }\n"
    )
    result = search_repository_files(repo, r"guard", max_results=2)
    assert result["matches"]
    assert all(match["file"] == "src.c" for match in result["matches"])
    with pytest.raises(ValueError, match="inside"):
        read_repository_file(repo, "../src.c")
    with pytest.raises(ValueError, match="inside"):
        search_repository_files(repo, "guard", path="../")


def test_plain_ast_grep_query_and_pydantic_wrapper_share_behavior(tmp_path: Path):
    if shutil.which("ast-grep") is None and shutil.which("sg") is None:
        pytest.skip("ast-grep is required")

    target = tmp_path / "target"
    target.mkdir()
    (target / "sample.c").write_text("void f(void) { danger(1); }\n", encoding="utf-8")

    plain = run_ast_grep_query(
        tmp_path,
        "target",
        language="c",
        query_type="pattern",
        query="danger($ARG);",
        output="count",
    )
    context = SimpleNamespace(
        deps=SimpleNamespace(
            workspace_root=tmp_path,
            root_cause=SimpleNamespace(language=SimpleNamespace(value="c")),
        )
    )
    assert plain == run_ast_grep(
        context,
        "target",
        QueryType.PATTERN,
        "danger($ARG);",
        output="count",
    )
    assert plain["match_count"] == 1
    with pytest.raises(ValueError, match="must stay inside"):
        run_ast_grep_query(
            tmp_path,
            tmp_path.parent.as_posix(),
            language="c",
            query_type="pattern",
            query="danger($ARG);",
        )
    with pytest.raises(ValueError, match="sample_size"):
        run_ast_grep_query(
            tmp_path,
            "target",
            language="c",
            query_type="pattern",
            query="danger($ARG);",
            sample_size=101,
        )


def test_skill_resources_are_task_scoped_and_bounded(tmp_path: Path):
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (skill / "references" / "rules.md").write_text("first\nsecond\nthird\n", encoding="utf-8")
    roots = {"ast-grep": skill}

    assert list_skill_resources(roots, "ast-grep") == {
        "skill": "ast-grep",
        "resources": ["SKILL.md", "references/rules.md"],
        "truncated": False,
    }
    assert read_skill_resource(
        roots,
        "ast-grep",
        "references/rules.md",
        start_line=2,
        end_line=3,
    ) == {
        "skill": "ast-grep",
        "path": "references/rules.md",
        "content": "second\nthird\n",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 3,
        "truncated": False,
    }
    with pytest.raises(ValueError, match="unknown task skill"):
        read_skill_resource(roots, "other", "SKILL.md")
    with pytest.raises(ValueError, match="stay inside"):
        read_skill_resource(roots, "ast-grep", "../outside.md")
