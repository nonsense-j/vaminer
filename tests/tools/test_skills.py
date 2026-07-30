"""Tests for task-scoped skill-resource operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.miner.tools.skills import list_skill_resources, read_skill_resource


def test_resources_are_task_scoped_and_bounded(tmp_path: Path):
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (skill / "references" / "rules.md").write_text(
        "first\nsecond\nthird\n",
        encoding="utf-8",
    )
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
