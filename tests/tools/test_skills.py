"""Tests for task-scoped skill-resource reads."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from src.miner.models import AstGrepExperience
from src.miner.tools import skills as skills_module
from src.miner.tools.skills import (
    AST_GREP_EXPERIENCES_RESOURCE,
    list_skill_resources,
    read_skill_resource,
    record_ast_grep_experiences,
)


def test_resources_are_task_scoped_and_bounded(tmp_path: Path):
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (skill / "references" / "rules.md").write_text(
        "first\nsecond\nthird\n",
        encoding="utf-8",
    )
    roots = {"ast-grep": skill}

    assert list_skill_resources(roots, "ast-grep") == "SKILL.md\nreferences/rules.md"
    assert read_skill_resource(
        roots,
        "ast-grep",
        "references/rules.md",
        start_line=2,
        end_line=3,
    ) == "==> ast-grep/references/rules.md | lines 2-3 of 3 <==\nsecond\nthird"
    with pytest.raises(ValueError, match="unknown task skill"):
        read_skill_resource(roots, "other", "SKILL.md")
    with pytest.raises(ValueError, match="stay inside"):
        read_skill_resource(roots, "ast-grep", "../outside.md")


def test_skill_resource_readers_can_share_the_lock(tmp_path: Path):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    first_entered = Event()
    second_entered = Event()
    release_first = Event()

    def hold_first_reader() -> None:
        with skills_module._skill_resource_lock(skill, exclusive=False):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def enter_second_reader() -> None:
        with skills_module._skill_resource_lock(skill, exclusive=False):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(hold_first_reader)
        assert first_entered.wait(timeout=2)
        second = executor.submit(enter_second_reader)
        try:
            assert second_entered.wait(timeout=2)
        finally:
            release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)


def test_cpp_experience_scope_is_normalized_without_changing_lesson(tmp_path: Path):
    skill = tmp_path / "ast-grep"
    references = skill / "references"
    references.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    experiences = skill / AST_GREP_EXPERIENCES_RESOURCE
    experiences.write_text(
        "# AST-Grep Query-Writing Experiences\n\n"
        "## C++ Query Lessons\n\n"
        "- [C++-1] Keep C++ syntax in the lesson text.\n",
        encoding="utf-8",
    )

    added = record_ast_grep_experiences(
        skill,
        [
            AstGrepExperience(
                mode="ADD",
                lesson_id="CPP-1",
                lesson="Another reusable CPP query lesson.",
            )
        ],
    )

    assert added == 1
    content = experiences.read_text(encoding="utf-8")
    assert "## CPP Query Lessons" in content
    assert "- [CPP-1] Keep C++ syntax in the lesson text." in content
    assert "- [CPP-2] Another reusable CPP query lesson." in content
