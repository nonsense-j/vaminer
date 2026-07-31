"""Claude-native delegated Synthesizer execution tests."""

from __future__ import annotations

import json
import stat
import textwrap
from pathlib import Path

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.models import AnchorSynthesisRequest
from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.mcp import (
    CASES_DIR_ENV,
    PROFILE_ENV,
    SOURCE_ROOT_ENV,
)
from src.miner.runtimes.claude.synthesis import (
    ClaudeSynthesisHostContext,
    load_claude_synthesis_handler,
)
from tests.support.factories import (
    BEHAVIOR,
    INSPECT_HINT,
    SOURCE,
    analysis_subject,
    root_cause,
)


def _write_synthesizer_cli(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys

            def option(name):
                return sys.argv[sys.argv.index(name) + 1]

            prompt = json.loads(sys.stdin.read())
            run_request = prompt["anchor_synthesis_run_request"]
            target_id = run_request["target_anchor_id"]
            intent = next(
                item for item in run_request["anchor_plan"]
                if item["id"] == target_id
            )
            mcp = json.loads(Path(option("--mcp-config")).read_text())
            record = {
                "argv": sys.argv[1:],
                "prompt": prompt,
                "system_prompt": Path(option("--system-prompt-file")).read_text(),
                "mcp": mcp,
                "settings": json.loads(Path(option("--settings")).read_text()),
            }
            Path(os.environ["CLAUDE_CHILD_RECORD"]).write_text(json.dumps(record))
            output = {
                "anchor": {
                    "id": intent["id"],
                    "behavior_weight": intent["behavior_weight"],
                    "query_weight": intent["behavior_weight"],
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": intent["behavior"],
                    "inspect_hint": intent["inspect_hint"],
                },
                "adjustments": [],
                "plan_suggestion": "",
            }
            print(json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "synthesizer-session",
                "num_turns": 1,
                "duration_ms": 10,
                "usage": {"input_tokens": 9, "output_tokens": 4},
                "modelUsage": {
                    "claude-test": {"inputTokens": 9, "outputTokens": 4}
                },
                "permission_denials": [],
                "terminal_reason": "end_turn",
                "result": json.dumps(output),
                "structured_output": output,
            }))
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


async def test_delegated_synthesizer_runs_through_claude_cli_with_own_schema(
    tmp_path: Path,
):
    source_root = tmp_path / "source"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
    rca = root_cause()
    parent = make_rule_generation_task(
        rca,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
        output_root=tmp_path / "output",
        input_id="CVE-TEST",
        trace_id="trace",
    )
    request_rca = rca.model_copy(
        update={
            "root_cause_summary": (
                "The Rule Generator emitted a concise equivalent RCA summary."
            )
        }
    )
    request = AnchorSynthesisRequest.model_validate(
        {
            "root_cause": request_rca.model_dump(mode="json"),
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
    fake_claude = _write_synthesizer_cli(tmp_path / "claude")
    record_path = tmp_path / "child-call.json"
    config = ClaudeCodeConfig(
        executable=fake_claude,
        model="claude-test",
        effort="high",
        output_format="json",
        artifact_root=tmp_path / "artifacts",
        environment={"CLAUDE_CHILD_RECORD": str(record_path)},
    )
    host_context = ClaudeSynthesisHostContext.from_parent(
        parent,
        config,
        executable=str(fake_claude),
        model_id="claude-test",
    )
    context_path = tmp_path / "synthesis-context.json"
    context_path.write_text(host_context.model_dump_json(), encoding="utf-8")

    results = await load_claude_synthesis_handler(context_path)(request)

    assert [result.anchor.id for result in results] == ["danger-call"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    synthesis_rca = record["prompt"]["anchor_synthesis_run_request"]["root_cause"]
    assert (
        synthesis_rca["root_cause_summary"] == request_rca.root_cause_summary
    )
    argv = record["argv"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-test"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    allowed = set(argv[argv.index("--allowedTools") + 1].split(","))
    assert allowed == {
        "Read",
        "Grep",
        "Glob",
        "mcp__vaminer__run_ast_grep_query",
    }
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert {"anchor", "adjustments", "plan_suggestion"} <= set(
        schema["properties"]
    )
    mcp_env = record["mcp"]["mcpServers"]["vaminer"]["env"]
    assert mcp_env[PROFILE_ENV] == "ast_grep_synthesis"
    assert mcp_env[SOURCE_ROOT_ENV] == str(source_root)
    assert mcp_env[CASES_DIR_ENV] == str(cases_dir)
    assert "## Loaded ast-grep skill" in record["system_prompt"]
    assert "## Claude Code" in record["system_prompt"]
    assert "## Pydantic AI" not in record["system_prompt"]
    assert {"Bash", "Task", "Write", "Edit"} <= set(
        record["settings"]["permissions"]["deny"]
    )
