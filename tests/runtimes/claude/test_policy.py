"""Tests for compiled Claude invocation authority."""

import json
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
)

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.policy import PolicyCompiler
from src.miner.utils.log import run_log_file
from tests.support.factories import SOURCE, analysis_subject, root_cause


def test_rule_generation_uses_cases_and_typed_mcp_only(tmp_path: Path):
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
    policy = compiler.compile(task)

    assert policy.built_in_tools == ("Read", "Grep", "Glob")
    assert policy.registered_tools == ("Read", "Grep", "Glob")
    assert "mcp__vaminer__synthesize_ast_grep_anchors" in policy.allowed_tools
    assert "Bash" not in policy.allowed_tools
    assert "Agent" not in policy.allowed_tools
    assert "Task" not in policy.allowed_tools
    assert {"Bash", "Task", "Write", "Edit"} <= set(policy.denied_tools)


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
    policy = compiler.compile(task)

    untraced = compiler.materialize(
        tmp_path / "untraced",
        task=task,
        policy=policy,
    )
    traced = compiler.materialize(
        tmp_path / "traced",
        task=task,
        policy=policy,
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


def test_rule_generation_mcp_inherits_trace_and_run_log_context(tmp_path: Path):
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
    policy = compiler.compile(task)
    span_context = SpanContext(
        trace_id=int("0123456789abcdef0123456789abcdef", 16),
        span_id=int("0123456789abcdef", 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )

    with (
        run_log_file(
            tmp_path / "logs",
            "VAS-TEST",
            input_id="CVE-TEST",
            trace_id="trace-1",
            runtime="claude-code",
        ) as log_path,
        trace.use_span(
            NonRecordingSpan(span_context),
            end_on_exit=False,
        ),
    ):
        files = compiler.materialize(
            tmp_path / "observable",
            task=task,
            policy=policy,
        )

    mcp = json.loads(files.mcp.read_text(encoding="utf-8"))
    environment = mcp["mcpServers"]["vaminer"]["env"]
    assert environment["VAMINER_MCP_SYNTHESIS_LOG"] == str(log_path.resolve())
    assert environment["VAMINER_OTEL_TRACEPARENT"].startswith(
        "00-0123456789abcdef0123456789abcdef-0123456789abcdef-"
    )
