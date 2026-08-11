"""Contract tests for shared, input-specific, and runtime instruction layers."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.models import AnchorSynthesisRequest
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.policy import PolicyCompiler
from src.miner.runtimes.pydantic.runtime import _compiled_instructions as pydantic_instructions
from src.miner.runtimes.shared.synthesis import AnchorSynthesisContext
from tests.support.factories import (
    BEHAVIOR,
    INSPECT_HINT,
    analysis_subject,
    root_cause,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTIONS_DIR = PROJECT_ROOT / "src" / "miner" / "instructions"
AST_GREP_SKILL = PROJECT_ROOT / "src" / "miner" / "skills" / "ast-grep" / "SKILL.md"

SHARED_INSTRUCTION_FILES = (
    "issue_collector.md",
    "root_cause_analyzer.md",
    "rule_generator.md",
    "ast_grep_synthesizer.md",
)
SHARED_HEADINGS = (
    "# Role & Task",
    "# Context",
    "# Workflow",
    "# Constraints",
)
RUNTIME_MARKERS = (
    "Pydantic AI",
    "Claude Code",
    "mcp__",
    "run_ast_grep_query",
    "skill_read_file",
    "src_read_file",
    "source_read_file",
    "scripts/runner.py",
)


@pytest.mark.parametrize("name", SHARED_INSTRUCTION_FILES)
def test_shared_instructions_have_one_runtime_neutral_structure(name: str):
    text = (INSTRUCTIONS_DIR / name).read_text(encoding="utf-8")

    positions = [text.index(heading) for heading in SHARED_HEADINGS]
    assert positions == sorted(positions)
    assert all(text.count(heading) == 1 for heading in SHARED_HEADINGS)
    assert "# Input Policy" not in text
    assert not any(marker in text for marker in RUNTIME_MARKERS)


@pytest.mark.parametrize(
    "name",
    (
        "root_cause_analyzer.md",
        "rule_generator.md",
        "ast_grep_synthesizer.md",
    ),
)
def test_cross_input_instructions_do_not_embed_input_mode_policy(name: str):
    text = (INSTRUCTIONS_DIR / name).read_text(encoding="utf-8")

    for marker in (
        "repo_evidence",
        "bad_span_coverage",
        "example_suite",
        "fixed_revision_available",
    ):
        assert marker not in text


def test_root_cause_and_rule_generator_share_src_workspace_vocabulary():
    root_cause_text = (INSTRUCTIONS_DIR / "root_cause_analyzer.md").read_text(
        encoding="utf-8"
    )
    rule_text = (INSTRUCTIONS_DIR / "rule_generator.md").read_text(
        encoding="utf-8"
    )

    assert "`src/`" in root_cause_text
    assert "`cases/`" in root_cause_text
    assert "`src/`" in rule_text
    assert "`cases/`" in rule_text
    assert "`source_root`" not in rule_text


def test_ast_grep_skill_is_generic_structural_query_mechanics():
    text = AST_GREP_SKILL.read_text(encoding="utf-8")

    for caller_detail in (
        "`behavior`",
        "`inspect_hint`",
        "`required_cases`",
        "`query_weight`",
        "Anchor",
        "RCA",
        "VAS",
        "VAMiner",
        "run_ast_grep_query",
        "scripts/runner.py",
    ):
        assert caller_detail not in text


def test_runtime_adapters_append_only_their_binding_after_shared_input_policy(
    tmp_path: Path,
):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    task = make_rule_generation_task(
        root_cause(),
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
    )

    pydantic = pydantic_instructions(task)
    claude = PolicyCompiler(ClaudeCodeConfig())._compile_system_prompt(task)

    for compiled in (pydantic, claude):
        assert compiled.startswith(task.instructions.strip())
        assert compiled.index(task.input_instructions.strip()) > len(
            task.instructions.strip()
        )
        assert compiled.index("# Runtime Binding") > compiled.index(
            task.input_instructions.strip()
        )
        assert compiled.count("# Runtime Binding") == 1

    assert "## Pydantic AI" in pydantic
    assert "## Claude Code" not in pydantic
    assert "## Claude Code" in claude
    assert "## Pydantic AI" not in claude
    for compiled in (pydantic, claude):
        assert "workspace source area is `src/`" in compiled


def test_synthesizer_has_shared_task_but_runtime_native_binding(tmp_path: Path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    rca = root_cause()
    parent = make_rule_generation_task(
        rca,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
    )
    request = AnchorSynthesisRequest.model_validate(
        {
            "root_cause": rca.model_dump(mode="json"),
            "summary": "Dangerous operations require their guarding invariant.",
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": BEHAVIOR,
                    "inspect_hint": INSPECT_HINT,
                    "required_cases": ["case1.c"],
                }
            ],
        }
    )
    child = AnchorSynthesisContext.from_task(parent).child_task(
        request,
        request.anchor_intents[0],
    )

    pydantic = pydantic_instructions(child)
    claude = PolicyCompiler(ClaudeCodeConfig())._compile_system_prompt(child)

    for compiled in (pydantic, claude):
        assert compiled.startswith(child.instructions.strip())
        assert "# Input Policy" not in compiled
        assert compiled.count("# Runtime Binding") == 1
    assert "## Pydantic AI" in pydantic
    assert "## Claude Code" not in pydantic
    assert "workspace_read_file" in pydantic
    assert "## Claude Code" in claude
    assert "## Pydantic AI" not in claude
    assert str(tmp_path.resolve()) in claude
    assert "logical target `cases` or `src`" in claude
