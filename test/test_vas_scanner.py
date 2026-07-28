"""End-to-end test for the deployable VAS scanner workflow."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "src" / ".vaminer" / "skills" / "vas-scanner" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from core import finalize_scan, next_candidates, prepare_scan, record_analysis


def make_rule() -> dict:
    return {
        "vas_id": "VAS-9001",
        "category": "SECURITY",
        "language": "c",
        "summary": "Dangerous operations must satisfy the required safety invariant.",
        "scenarios": {
            "unsafe": ["A dangerous operation executes without its required guard."],
            "safe": ["The required guard applies before the dangerous operation executes."],
        },
        "anchors": [
            {
                "id": "danger-call",
                "behavior_weight": 5,
                "query_weight": 5,
                "type": "pattern",
                "query": "danger($A);",
                "behavior": "Invokes a dangerous operation with one argument.",
                "inspect_hint": "Inspect the dangerous call.",
            },
            {
                "id": "guard-call",
                "behavior_weight": 5,
                "query_weight": 3,
                "type": "rule",
                "query": "rule:\n  pattern: guard($A);",
                "behavior": "Invokes a guard operation with one argument.",
                "inspect_hint": "Inspect whether the guard applies.",
            },
        ],
    }


def warning(confidence: str, file: str, explanation: str) -> dict:
    return {
        "title": "Missing guard",
        "confidence": confidence,
        "primary_location": {"file": "b.c", "start_line": 1, "end_line": 1},
        "explanation": explanation,
        "evidence": [
            {
                "file": file,
                "start_line": 1,
                "end_line": 1,
                "fact": explanation,
            }
        ],
    }


def test_scanner_ranks_hotspots_and_merges_analysis_end_to_end(tmp_path: Path):
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")

    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    rules.mkdir()
    (rules / "VAS-9001.json").write_text(json.dumps(make_rule()), encoding="utf-8")
    (repo / "a.c").write_text("void a(void) { danger(1); danger(2); }\n", encoding="utf-8")
    (repo / "b.c").write_text("void b(void) { danger(1); guard(1); }\n", encoding="utf-8")
    (repo / "c.c").write_text("void c(void) { guard(1); }\n", encoding="utf-8")

    scan_dir = prepare_scan(
        "VAS-9001",
        repo,
        rules_dir=rules,
        workspace_dir=workspace,
        batch_size=2,
    )
    scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))

    assert [item["file"] for item in scan["candidates"]] == ["b.c", "a.c", "c.c"]
    assert [item["score"] for item in scan["candidates"]] == [8, 5, 3]
    first_batch = next_candidates(scan_dir)
    assert [item["rank"] for item in first_batch["candidates"]] == [1, 2]

    record_analysis(scan_dir, 1, [warning("MEDIUM", "b.c", "The call may be unguarded.")])
    record_analysis(scan_dir, 2, [warning("HIGH", "a.c", "The call is unguarded.")])
    final_batch = next_candidates(scan_dir)
    assert [item["rank"] for item in final_batch["candidates"]] == [3]
    record_analysis(scan_dir, 3, [])

    result = finalize_scan(scan_dir)
    report = json.loads((scan_dir / "report.json").read_text(encoding="utf-8"))

    assert result["warning_count"] == 1
    assert report["warnings"][0]["confidence"] == "HIGH"
    assert [item["rank"] for item in report["warnings"][0]["source_candidates"]] == [1, 2]
    assert len(report["warnings"][0]["evidence"]) == 2
