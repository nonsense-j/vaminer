"""Tests for compiled Claude invocation authority."""

import json
from pathlib import Path

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.policy import PolicyCompiler
from tests.support.factories import SOURCE, analysis_subject, root_cause


def test_rule_generation_uses_agent_scoped_permission(tmp_path: Path):
    source_root = tmp_path / "source"
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
    policy = compiler.compile(
        task,
        native_delegation_tool="Agent",
    )

    assert policy.built_in_tools == ("Read", "Grep", "Glob", "Agent")
    assert policy.registered_tools == ("Read", "Grep", "Glob", "Agent", "Bash")
    assert "Agent(vaminer:ast-grep-synthesizer)" in policy.allowed_tools
    assert "Agent(vaminer:ast-grep-synthesizer)" in policy.main_allowed_tools
    assert any(tool.startswith("Bash(") for tool in policy.allowed_tools)
    assert all(not tool.startswith("Bash(") for tool in policy.main_allowed_tools)
    assert "Agent" not in policy.allowed_tools
    assert "Task" not in policy.allowed_tools
    assert "Task" not in policy.denied_tools


def test_post_compact_hook_is_materialized_only_for_traced_runs(tmp_path: Path):
    source_root = tmp_path / "source"
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
    policy = compiler.compile(task, native_delegation_tool="Agent")

    untraced = compiler.materialize(
        tmp_path / "untraced",
        task=task,
        policy=policy,
        model_id="claude-test",
    )
    traced = compiler.materialize(
        tmp_path / "traced",
        task=task,
        policy=policy,
        model_id="claude-test",
        trace_compaction=True,
    )

    untraced_settings = json.loads(untraced.settings.read_text(encoding="utf-8"))
    traced_settings = json.loads(traced.settings.read_text(encoding="utf-8"))
    assert "PostCompact" not in untraced_settings["hooks"]
    assert untraced.compact_events is None
    assert traced.compact_events is not None
    post_compact = traced_settings["hooks"]["PostCompact"][0]
    assert post_compact["matcher"] == "auto|manual"
    assert post_compact["hooks"][0]["args"][-1] == str(traced.compact_events)
