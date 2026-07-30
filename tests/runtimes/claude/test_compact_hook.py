"""Tests for the Claude PostCompact hook transport."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

from src.miner.runtimes.claude import compact_hook


def test_compact_hook_persists_bounded_agent_context(tmp_path: Path):
    output_path = tmp_path / "compact-events.jsonl"
    payload = {
        "hook_event_name": "PostCompact",
        "trigger": "auto",
        "compact_summary": "Condensed context",
        "session_id": "session-1",
        "parent_tool_use_id": "agent-tool-1",
        "agent_id": "agent-1",
        "agent_type": "vaminer:rule-generator",
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(compact_hook.__file__).resolve()),
            str(output_path),
        ],
        input=json.dumps(payload).encode(),
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    event = json.loads(output_path.read_text(encoding="utf-8"))
    assert event == {
        "type": "hook",
        "hook_event_name": "PostCompact",
        "trigger": "auto",
        "compact_summary": "Condensed context",
        "summary_truncated": False,
        "session_id": "session-1",
        "parent_tool_use_id": "agent-tool-1",
        "agent_id": "agent-1",
        "agent_type": "vaminer:rule-generator",
    }
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
