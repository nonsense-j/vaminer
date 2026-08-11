"""Tests for compiled Claude invocation authority."""

from pathlib import Path

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.policy import PolicyCompiler
from tests.support.factories import SOURCE, analysis_subject, root_cause


def test_rule_generation_uses_cases_and_typed_mcp_only(tmp_path: Path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
    task = make_rule_generation_task(
        root_cause(),
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
    )

    compiler = PolicyCompiler(ClaudeCodeConfig())
    policy = compiler.compile(task)

    assert policy.built_in_tools == ("Read", "Grep", "Glob")
    assert policy.registered_tools == ("Read", "Grep", "Glob")
    assert "mcp__vaminer__synthesize_ast_grep_anchors" in policy.allowed_tools
    assert "Bash" not in policy.allowed_tools
    assert "Agent" not in policy.allowed_tools
    assert "Task" not in policy.allowed_tools
    assert {"Bash", "Task", "Write", "Edit"} <= set(policy.denied_tools)
