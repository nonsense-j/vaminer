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
    MAX_AST_GREP_EXPERIENCES,
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
                outcome="success",
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
        outcome="pitfall",
        lesson="A regex rule also needs an AST kind.",
    )

    assert record_ast_grep_experiences(skill, [pitfall]) == 1
    punctuation_only_duplicate = pitfall.model_copy(
        update={"lesson": "a regex rule also needs an AST kind!"}
    )
    extension = pitfall.model_copy(
        update={"lesson": "A regex rule also needs an AST kind for candidate-node selection."}
    )
    assert record_ast_grep_experiences(skill, [pitfall, punctuation_only_duplicate]) == 0
    assert record_ast_grep_experiences(skill, [extension]) == 1
    conflicting_extension = AstGrepExperience(
        outcome="success",
        lesson=(
            "A regex rule also needs an AST kind for candidate-node selection "
            "in every query."
        ),
    )
    assert record_ast_grep_experiences(skill, [conflicting_extension]) == 0
    rendered = read_skill_resource(
        {"ast-grep": skill},
        "ast-grep",
        AST_GREP_EXPERIENCES_RESOURCE,
    )

    assert "A regex rule also needs an AST kind for candidate-node selection." in rendered
    assert rendered.count("**pitfall**") == 1


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
            outcome="pitfall",
            lesson=f"Later bounded query lesson number {index}.",
        )
        for index in range(5)
    ]
    assert record_ast_grep_experiences(skill, overflow) == len(overflow)
    bounded = (skill / AST_GREP_EXPERIENCES_RESOURCE).read_text(encoding="utf-8")
    assert bounded.count("- **") == MAX_AST_GREP_EXPERIENCES
    assert all(item.lesson in bounded for item in overflow)


def test_skill_resource_reader_waits_for_experience_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill = tmp_path / "ast-grep"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    record_ast_grep_experiences(
        skill,
        [AstGrepExperience(outcome="success", lesson="Initial reusable query lesson.")],
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
            [AstGrepExperience(outcome="pitfall", lesson="Later reusable query lesson.")],
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
