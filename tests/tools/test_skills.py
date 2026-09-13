"""Tests for task-scoped skill-resource operations."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, TimeoutError
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


def _record_concurrent_experience(arguments: tuple[str, int]) -> int:
    skill_root, index = arguments
    return record_ast_grep_experiences(
        skill_root,
        [
            AstGrepExperience(
                mode="ADD",
                lesson_id=f"all-{index + 1}",
                lesson=f"Reusable concurrent lesson number {index}.",
            )
        ],
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


def test_ast_grep_experiences_are_read_merged_and_deduplicated(tmp_path: Path):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    pitfall = AstGrepExperience(
        mode="ADD",
        lesson_id="all-1",
        lesson="A regex rule also needs an AST kind.",
    )

    assert record_ast_grep_experiences(skill, [pitfall]) == 1
    punctuation_only_duplicate = pitfall.model_copy(
        update={"lesson": " A regex rule also needs an AST kind. "},
    )
    extension = AstGrepExperience(
        mode="REPLACE",
        lesson_id="all-1",
        lesson="A regex rule also needs an AST kind for candidate-node selection.",
    )
    assert record_ast_grep_experiences(skill, [pitfall, punctuation_only_duplicate]) == 0
    assert record_ast_grep_experiences(skill, [extension]) == 1
    replacement = AstGrepExperience(
        mode="REPLACE",
        lesson_id="all-1",
        lesson=(
            "A regex rule also needs an AST kind for candidate-node selection "
            "in every query."
        ),
    )
    assert record_ast_grep_experiences(skill, [replacement]) == 1
    assert record_ast_grep_experiences(
        skill,
        [AstGrepExperience(mode="ADD", lesson_id="C-1", lesson="C-specific lesson.")],
    ) == 1
    rendered = read_skill_resource(
        {"ast-grep": skill},
        "ast-grep",
        AST_GREP_EXPERIENCES_RESOURCE,
    )

    assert "## Language-Agnostic Lessons" in rendered
    assert "## C Query Lessons" in rendered
    assert "- [all-1] A regex rule also needs an AST kind for candidate-node selection in every query." in rendered
    assert "- [C-1] C-specific lesson." in rendered


def test_experience_update_modes_are_id_addressed(tmp_path: Path):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    record_ast_grep_experiences(
        skill,
        [AstGrepExperience(mode="ADD", lesson_id="C-1", lesson="Original C lesson.")],
    )

    assert record_ast_grep_experiences(
        skill,
        [AstGrepExperience(mode="ADD", lesson_id="C-1", lesson="Another C lesson.")],
    ) == 1
    with pytest.raises(ValueError, match="unknown ast-grep experience all-1"):
        record_ast_grep_experiences(
            skill,
            [AstGrepExperience(mode="REPLACE", lesson_id="all-1", lesson="Missing lesson.")],
        )

    content = (skill / AST_GREP_EXPERIENCES_RESOURCE).read_text(encoding="utf-8")
    assert "- [C-1] Original C lesson." in content
    assert "- [C-2] Another C lesson." in content


def test_unknown_replace_does_not_partially_apply_same_batch(tmp_path: Path):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    record_ast_grep_experiences(
        skill,
        [AstGrepExperience(mode="ADD", lesson_id="all-1", lesson="Existing lesson.")],
    )

    with pytest.raises(ValueError, match="unknown ast-grep experience all-99"):
        record_ast_grep_experiences(
            skill,
            [
                AstGrepExperience(mode="ADD", lesson_id="all-2", lesson="Batch addition."),
                AstGrepExperience(mode="REPLACE", lesson_id="all-99", lesson="Missing lesson."),
            ],
        )

    content = (skill / AST_GREP_EXPERIENCES_RESOURCE).read_text(encoding="utf-8")
    assert "- [all-1] Existing lesson." in content
    assert "Batch addition." not in content


def test_concurrent_ast_grep_experience_writes_do_not_lose_updates(tmp_path: Path):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    experiences = [f"Reusable concurrent lesson number {index}." for index in range(24)]

    with ProcessPoolExecutor(max_workers=8) as executor:
        added = list(
            executor.map(
                _record_concurrent_experience,
                [(str(skill), index) for index in range(len(experiences))],
            )
        )

    content = (skill / AST_GREP_EXPERIENCES_RESOURCE).read_text(encoding="utf-8")
    assert sum(added) == len(experiences)
    assert all(lesson in content for lesson in experiences)

    overflow = [
        AstGrepExperience(
            mode="ADD",
            lesson_id=f"all-{index + 25}",
            lesson=f"Later query lesson number {index}.",
        )
        for index in range(5)
    ]
    assert record_ast_grep_experiences(skill, overflow) == len(overflow)
    expanded = (skill / AST_GREP_EXPERIENCES_RESOURCE).read_text(encoding="utf-8")
    assert expanded.count("- [") == 29
    assert all(item.lesson in expanded for item in overflow)


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


def test_skill_resource_reader_waits_for_experience_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    record_ast_grep_experiences(
        skill,
        [AstGrepExperience(mode="ADD", lesson_id="all-1", lesson="Initial reusable query lesson.")],
    )
    writer_entered = Event()
    allow_write = Event()
    atomic_write = skills_module._atomic_write_text

    def blocked_write(path: Path, content: str) -> None:
        writer_entered.set()
        assert allow_write.wait(timeout=2)
        atomic_write(path, content)

    monkeypatch.setattr(skills_module, "_atomic_write_text", blocked_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            record_ast_grep_experiences,
            skill,
            [AstGrepExperience(mode="ADD", lesson_id="all-2", lesson="Later reusable query lesson.")],
        )
        assert writer_entered.wait(timeout=2)
        reader = executor.submit(
            read_skill_resource,
            {"ast-grep": skill},
            "ast-grep",
            AST_GREP_EXPERIENCES_RESOURCE,
        )
        try:
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.05)
        finally:
            allow_write.set()

        assert writer.result(timeout=2) == 1
        rendered = reader.result(timeout=2)

    assert "Initial reusable query lesson." in rendered
    assert "Later reusable query lesson." in rendered
