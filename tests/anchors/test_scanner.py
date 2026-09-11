"""Tests for vas-scanner scan.version 1."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = PROJECT_ROOT / "src" / ".vaminer" / "skills" / "vas-scanner"
SCRIPT_DIR = SKILL_DIR / "scripts"
SCAN_CLI = SCRIPT_DIR / "scan.py"
sys.path.insert(0, str(SCRIPT_DIR))

import engine as scanner_engine
from core import (
    finalize_scan,
    load_rule,
    next_candidates,
    prepare_scan,
    record_analysis,
    retry_candidate,
)


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


def make_admission_rule() -> dict:
    anchors = []
    for name in ("alpha", "beta", "gamma"):
        anchors.append(
            {
                "id": f"{name}-call",
                "behavior_weight": 2,
                "query_weight": 2,
                "type": "pattern",
                "query": f"{name}();",
                "behavior": f"Invokes {name}.",
                "inspect_hint": f"Inspect {name}.",
            }
        )
    anchors.append(
        {
            "id": "gate-call",
            "behavior_weight": 4,
            "query_weight": 3,
            "type": "pattern",
            "query": "gate();",
            "behavior": "Invokes the admission gate.",
            "inspect_hint": "Inspect the admission gate.",
        }
    )
    return {
        "vas_id": "VAS-9002",
        "category": "SECURITY",
        "language": "c",
        "summary": "Calls must preserve the gate invariant.",
        "scenarios": {"unsafe": ["The gate invariant is violated."], "safe": []},
        "anchors": anchors,
    }


def write_rule(rules: Path, rule: dict) -> None:
    rules.mkdir(parents=True, exist_ok=True)
    (rules / f"{rule['vas_id']}.json").write_text(
        json.dumps(rule), encoding="utf-8"
    )


def write_overview(repo: Path) -> Path:
    overview = repo / ".vas" / "repository_overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(
        "# Repository Overview\n\n- `*.c`: compact test source modules.\n",
        encoding="utf-8",
    )
    return overview.resolve()


def require_ast_grep() -> None:
    if shutil.which("ast-grep") is None and shutil.which("sg") is None:
        pytest.skip("ast-grep is required")


def copy_cli_skill(destination: Path) -> Path:
    skill = destination / "vas-scanner"
    shutil.copytree(
        SCRIPT_DIR,
        skill / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(SKILL_DIR / "references", skill / "references")
    (skill / "rules").mkdir()
    return skill / "scripts" / "scan.py"


def warning(
    confidence: str,
    evidence_file: str,
    explanation: str,
    *,
    anchor_id: str = "danger-call",
    anchor_file: str = "b.c",
    anchor_start: int = 1,
    anchor_end: int = 1,
) -> dict:
    return {
        "title": "Missing guard",
        "confidence": confidence,
        "primary_location": {"file": "b.c", "start_line": 1, "end_line": 1},
        "explanation": explanation,
        "evidence": [
            {
                "file": evidence_file,
                "start_line": 1,
                "end_line": 1,
                "fact": explanation,
            },
            {
                "file": anchor_file,
                "start_line": anchor_start,
                "end_line": anchor_end,
                "fact": "The matched call participates in the unguarded operation.",
                "anchor_refs": [
                    {
                        "anchor_id": anchor_id,
                        "alert_hint": (
                            "Unsafe when this call is reachable without the required guard."
                        ),
                    }
                ],
            },
        ],
    }


def prepare_basic_scan(tmp_path: Path, *, max_candidates: int | None = 20) -> tuple[Path, Path]:
    require_ast_grep()
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    write_overview(repo)
    write_rule(rules, make_rule())
    (repo / "b.c").write_text(
        "void b(void) { danger(1); guard(1); }\n", encoding="utf-8"
    )
    return (
        prepare_scan(
            "VAS-9001",
            repo,
            rules_dir=rules,
            workspace_dir=workspace,
            max_candidates=max_candidates,
        ),
        repo,
    )


def test_rule_loader_rejects_invalid_missing_and_mismatched_ids(tmp_path: Path):
    rules = tmp_path / "rules"
    rules.mkdir()

    with pytest.raises(ValueError, match="invalid VAS id"):
        load_rule("rule.json", rules)
    with pytest.raises(FileNotFoundError, match="VAS rule not found"):
        load_rule("VAS-999999999", rules)

    mismatched = make_rule()
    mismatched["vas_id"] = "VAS-9002"
    (rules / "VAS-9001.json").write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(ValueError, match="expected 'VAS-9001'"):
        load_rule("VAS-9001", rules)


def test_cli_prepare_rejects_a_rule_path_in_place_of_vas_id(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rule_path = tmp_path / "VAS-9001.json"
    rule_path.write_text(json.dumps(make_rule()), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCAN_CLI), "prepare", str(rule_path), str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "invalid VAS id" in completed.stderr


def test_cli_prepare_reports_missing_and_internally_mismatched_bundled_rules(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    cli = copy_cli_skill(tmp_path)

    missing = subprocess.run(
        [sys.executable, str(cli), "prepare", "VAS-9001", str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode == 1
    assert "VAS rule not found" in missing.stderr

    mismatched = make_rule()
    mismatched["vas_id"] = "VAS-9002"
    (cli.parents[1] / "rules" / "VAS-9001.json").write_text(
        json.dumps(mismatched), encoding="utf-8"
    )
    mismatch = subprocess.run(
        [sys.executable, str(cli), "prepare", "VAS-9001", str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert mismatch.returncode == 1
    assert "expected 'VAS-9001'" in mismatch.stderr


def test_cli_prepare_accepts_all_candidate_budget(tmp_path: Path):
    require_ast_grep()
    repo = tmp_path / "repo"
    repo.mkdir()
    cli = copy_cli_skill(tmp_path)
    write_rule(cli.parents[1] / "rules", make_rule())
    (repo / "a.c").write_text("void a(void) { danger(1); }\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(cli),
            "prepare",
            "VAS-9001",
            str(repo),
            "--max-candidates",
            "all",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    output = json.loads(completed.stdout)
    state = json.loads(
        (Path(output["scan_dir"]) / "scan.json").read_text(encoding="utf-8")
    )

    assert completed.returncode == 0
    assert state["settings"]["max_candidates"] == "all"
    assert output["coverage"]["scheduled"] == 1


def test_packaged_scanner_accepts_all_disabled_rule(tmp_path: Path):
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    (repo / "a.c").write_text("void a(void) {}\n", encoding="utf-8")
    rule = make_rule()
    for anchor in rule["anchors"]:
        anchor["query"] = ""
    write_rule(rules, rule)

    scan_dir = prepare_scan(
        "VAS-9001",
        repo,
        rules_dir=rules,
        workspace_dir=workspace,
        ast_grep="unused-ast-grep",
    )
    scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    rule_snapshot = json.loads((scan_dir / "rule.json").read_text(encoding="utf-8"))
    anchor_map = json.loads((scan_dir / "anchor_map.json").read_text(encoding="utf-8"))

    assert scan["schema"] == "vas-scanner.scan.v1"
    assert scan["version"] == 1
    assert scan["rule"] == "rule.json"
    assert rule_snapshot["anchors"][0]["query"] == ""
    assert scan["candidates"] == []
    assert anchor_map["files"] == []
    assert [item["enabled"] for item in anchor_map["anchors"]] == [False, False]
    assert next_candidates(scan_dir) == {"done": True, "tasks": []}
    assert finalize_scan(scan_dir)["status"] == "complete"


def test_packaged_scanner_reports_missing_captured_output_as_execution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        scanner_engine.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=None,
            stderr=None,
        ),
    )

    with pytest.raises(
        scanner_engine.AnchorExecutionError,
        match=r"did not receive captured stdout and stderr from ast-grep \(exit code 0\)",
    ):
        scanner_engine.scan_anchors(
            [make_rule()["anchors"][0]],
            repo,
            "c",
            ast_grep="ast-grep",
        )


def test_q2_sum_cannot_admit_but_low_weight_still_scores_an_admitted_file(
    tmp_path: Path,
):
    require_ast_grep()
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    write_rule(rules, make_admission_rule())
    (repo / "low.c").write_text(
        "void low(void) { alpha(); beta(); gamma(); }\n", encoding="utf-8"
    )
    (repo / "high.c").write_text(
        "void high(void) { gate(); alpha(); }\n", encoding="utf-8"
    )

    scan_dir = prepare_scan(
        "VAS-9002", repo, rules_dir=rules, workspace_dir=workspace
    )
    scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    anchor_map = json.loads((scan_dir / "anchor_map.json").read_text(encoding="utf-8"))
    files = {item["file"]: item for item in anchor_map["files"]}

    assert files["low.c"]["priority_score"] == 6
    assert files["low.c"]["admitted"] is False
    assert "below the admission threshold" in files["low.c"]["exclusion_reason"]
    assert files["high.c"]["admitted"] is True
    assert files["high.c"]["priority_score"] == 5
    assert {hint["query_weight"] for hint in files["high.c"]["anchor_hints"]} == {2, 3}
    assert [item["file"] for item in scan["candidates"]] == ["high.c"]


def test_tasks_out_of_order_results_shared_hints_and_deduplication(tmp_path: Path):
    require_ast_grep()
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    overview = write_overview(repo)
    write_rule(rules, make_rule())
    (repo / "a.c").write_text(
        "void a(void) { danger(1); danger(2); }\n", encoding="utf-8"
    )
    (repo / "b.c").write_text(
        "void b(void) { danger(1); guard(1); }\n", encoding="utf-8"
    )
    (repo / "c.c").write_text("void c(void) { guard(1); }\n", encoding="utf-8")

    scan_dir = prepare_scan(
        "VAS-9001", repo, rules_dir=rules, workspace_dir=workspace
    )
    scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    anchor_map = json.loads((scan_dir / "anchor_map.json").read_text(encoding="utf-8"))

    assert [item["file"] for item in scan["candidates"]] == ["b.c", "a.c", "c.c"]
    assert [item["score"] for item in scan["candidates"]] == [8, 5, 3]
    assert len(list((scan_dir / "tasks").glob("*.md"))) == 3
    assert anchor_map["counts"] == {
        "matched": 3,
        "low_weight_excluded": 0,
        "admitted": 3,
        "scheduled": 3,
        "omitted_by_budget": 0,
    }

    first_task = (scan_dir / scan["candidates"][0]["task"]).read_text(encoding="utf-8")
    assert str(overview) in first_task
    assert str((scan_dir / "anchor_map.json").resolve()) in first_task
    assert str((scan_dir / "shared_checks").resolve()) in first_task
    assert make_rule()["summary"] in first_task
    assert make_rule()["scenarios"]["unsafe"][0] in first_task
    assert make_rule()["scenarios"]["safe"][0] in first_task
    assert "danger($A);" not in first_task
    assert "rule:\n  pattern: guard($A);" not in first_task
    assert "## Code Regions" not in first_task
    assert "source line 1:" in first_task

    first_batch = next_candidates(scan_dir)
    assert [item["rank"] for item in first_batch["tasks"]] == [1, 2, 3]

    record_analysis(
        scan_dir,
        2,
        [warning("HIGH", "a.c", "The call is concretely unguarded from a.c.")],
    )
    cross_file_shared = (scan_dir / "shared_checks" / "b.c.md").read_text(
        encoding="utf-8"
    )
    assert "partial cross-file alert information" in cross_file_shared
    assert "Status: `ALERT`" in cross_file_shared

    # Completing b.c with [] publishes its other high-weight location as safe, but
    # cannot overwrite the alert already contributed by the cross-file warning.
    record_analysis(scan_dir, 1, [])
    completed_shared = (scan_dir / "shared_checks" / "b.c.md").read_text(
        encoding="utf-8"
    )
    assert "complete primary-candidate analysis" in completed_shared
    assert completed_shared.count("Status: `ALERT`") == 1
    assert "Inspect whether the guard applies. — Already Checked Safe" in completed_shared

    record_analysis(
        scan_dir,
        3,
        [warning("MEDIUM", "c.c", "A second candidate reaches the same operation.")],
    )
    assert next_candidates(scan_dir) == {"done": True, "tasks": []}

    result = finalize_scan(scan_dir)
    report = json.loads((scan_dir / "report.json").read_text(encoding="utf-8"))

    assert result == {
        "report": str((scan_dir / "report.json").resolve()),
        "status": "complete",
        "warning_count": 1,
    }
    assert report["warnings"][0]["confidence"] == "HIGH"
    assert [item["rank"] for item in report["warnings"][0]["source_candidates"]] == [2, 3]
    assert len(report["warnings"][0]["evidence"]) == 3
    assert "related_anchors" not in report["warnings"][0]
    anchor_evidence = [
        item
        for item in report["warnings"][0]["evidence"]
        if item.get("anchor_refs")
    ]
    assert len(anchor_evidence) == 1
    assert sum("anchor_refs" in item for item in report["warnings"][0]["evidence"]) == 1
    assert anchor_evidence[0]["anchor_refs"][0]["anchor_id"] == "danger-call"
    assert report["coverage"]["checked_safe_anchor_locations"] == 3
    assert report["coverage"]["alert_anchor_locations"] == 1
    assert (scan_dir / "results" / "0001.json").is_file()
    assert (scan_dir / "results" / "0002.json").is_file()
    assert (scan_dir / "results" / "0003.json").is_file()


def test_default_numeric_and_all_candidate_budgets(tmp_path: Path):
    require_ast_grep()
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    write_overview(repo)
    rule = make_rule()
    rule["anchors"] = [rule["anchors"][0]]
    write_rule(rules, rule)
    for index in range(22):
        (repo / f"candidate_{index:02d}.c").write_text(
            f"void f{index}(void) {{ danger({index}); }}\n", encoding="utf-8"
        )

    default_scan = prepare_scan(
        "VAS-9001", repo, rules_dir=rules, workspace_dir=workspace
    )
    numeric_scan = prepare_scan(
        "VAS-9001",
        repo,
        rules_dir=rules,
        workspace_dir=workspace,
        max_candidates=7,
    )
    all_scan = prepare_scan(
        "VAS-9001",
        repo,
        rules_dir=rules,
        workspace_dir=workspace,
        max_candidates=None,
    )

    default_state = json.loads((default_scan / "scan.json").read_text(encoding="utf-8"))
    numeric_state = json.loads((numeric_scan / "scan.json").read_text(encoding="utf-8"))
    all_state = json.loads((all_scan / "scan.json").read_text(encoding="utf-8"))
    default_map = json.loads((default_scan / "anchor_map.json").read_text(encoding="utf-8"))

    assert len(default_state["candidates"]) == 20
    assert len(default_state["omissions"]) == 2
    assert default_map["counts"]["omitted_by_budget"] == 2
    assert sum(item["omitted_by_budget"] for item in default_map["files"]) == 2
    assert len(numeric_state["candidates"]) == 7
    assert len(all_state["candidates"]) == 22
    assert all_state["omissions"] == []


def test_budget_omission_makes_completed_report_incomplete_and_cli_exits_two(
    tmp_path: Path,
):
    require_ast_grep()
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    write_overview(repo)
    rule = make_rule()
    rule["anchors"] = [rule["anchors"][0]]
    write_rule(rules, rule)
    (repo / "a.c").write_text("void a(void) { danger(1); }\n", encoding="utf-8")
    (repo / "b.c").write_text("void b(void) { danger(2); }\n", encoding="utf-8")
    scan_dir = prepare_scan(
        "VAS-9001",
        repo,
        rules_dir=rules,
        workspace_dir=workspace,
        max_candidates=1,
    )

    task = next_candidates(scan_dir)["tasks"][0]
    record_analysis(scan_dir, task["rank"], [])
    completed = subprocess.run(
        [sys.executable, str(SCAN_CLI), "finalize", str(scan_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    output = json.loads(completed.stdout)
    report = json.loads((scan_dir / "report.json").read_text(encoding="utf-8"))

    assert completed.returncode == 2
    assert output["status"] == "incomplete"
    assert report["coverage"]["completed"] == 1
    assert report["coverage"]["unfinished"] == 0
    assert report["coverage"]["omitted_by_budget"] == 1


def test_next_requires_agent_written_overview_before_dispatch(tmp_path: Path):
    scan_dir, repo = prepare_basic_scan(tmp_path)
    (repo / ".vas" / "repository_overview.md").unlink()

    with pytest.raises(FileNotFoundError, match="repository overview is required"):
        next_candidates(scan_dir)


def test_invalid_evidence_anchor_path_and_line_are_rejected(tmp_path: Path):
    scan_dir, _ = prepare_basic_scan(tmp_path)
    next_candidates(scan_dir)

    bad_anchor = warning("HIGH", "b.c", "bad anchor")
    bad_anchor["evidence"][-1]["anchor_refs"][0]["anchor_id"] = "not-in-map"
    with pytest.raises(ValueError, match="exact match in anchor_map"):
        record_analysis(scan_dir, 1, [bad_anchor])

    duplicated_location = warning("HIGH", "b.c", "duplicate location")
    duplicated_location["evidence"][-1]["anchor_refs"][0]["file"] = "b.c"
    with pytest.raises(ValueError, match="unsupported fields: file"):
        record_analysis(scan_dir, 1, [duplicated_location])

    bad_path = warning("HIGH", "b.c", "bad path")
    bad_path["evidence"][-1]["file"] = "missing.c"
    with pytest.raises(ValueError, match="does not identify a source file"):
        record_analysis(scan_dir, 1, [bad_path])

    bad_line = warning("HIGH", "b.c", "bad line")
    bad_line["evidence"][-1]["end_line"] = 999
    with pytest.raises(ValueError, match="exceeds the source file line count"):
        record_analysis(scan_dir, 1, [bad_line])

    no_anchor_ref = warning("HIGH", "b.c", "missing link")
    del no_anchor_ref["evidence"][-1]["anchor_refs"]
    with pytest.raises(ValueError, match="at least one anchor_refs entry"):
        record_analysis(scan_dir, 1, [no_anchor_ref])

    legacy_shape = warning("HIGH", "b.c", "legacy shape")
    legacy_shape["related_anchors"] = []
    with pytest.raises(ValueError, match="related_anchors is not supported"):
        record_analysis(scan_dir, 1, [legacy_shape])

    record_analysis(scan_dir, 1, [])


def test_source_drift_marks_candidate_stale_and_scan_incomplete(tmp_path: Path):
    scan_dir, repo = prepare_basic_scan(tmp_path)
    (repo / "b.c").write_text(
        "void b(void) { danger(1); guard(1); } /* changed */\n",
        encoding="utf-8",
    )

    assert next_candidates(scan_dir) == {"done": True, "tasks": []}
    state = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    result = finalize_scan(scan_dir)
    report = json.loads((scan_dir / "report.json").read_text(encoding="utf-8"))

    assert state["candidates"][0]["state"] == "stale"
    assert result["status"] == "incomplete"
    assert report["coverage"]["stale"] == 1
    assert report["failures"][0]["state"] == "stale"


def test_record_and_finalize_each_revalidate_source_hashes(tmp_path: Path):
    record_scan, record_repo = prepare_basic_scan(tmp_path / "record")
    next_candidates(record_scan)
    (record_repo / "b.c").write_text(
        "void b(void) { danger(1); } /* changed before record */\n",
        encoding="utf-8",
    )
    recorded = record_analysis(record_scan, 1, [])
    assert recorded["state"] == "stale"
    assert not (record_scan / "results" / "0001.json").exists()

    finalize_run, finalize_repo = prepare_basic_scan(tmp_path / "finalize")
    next_candidates(finalize_run)
    record_analysis(finalize_run, 1, [])
    (finalize_repo / "b.c").write_text(
        "void b(void) { danger(1); } /* changed before finalize */\n",
        encoding="utf-8",
    )
    finalized = finalize_scan(finalize_run)
    report = json.loads((finalize_run / "report.json").read_text(encoding="utf-8"))

    assert finalized["status"] == "incomplete"
    assert report["coverage"]["stale"] == 1
    assert report["coverage"]["completed"] == 0


def test_retry_exhaustion_writes_errors_and_does_not_block_completion(tmp_path: Path):
    require_ast_grep()
    repo = tmp_path / "repo"
    rules = tmp_path / "rules"
    workspace = tmp_path / "workspace"
    repo.mkdir()
    write_overview(repo)
    write_rule(rules, make_rule())
    (repo / "a.c").write_text("void a(void) { danger(1); }\n", encoding="utf-8")
    (repo / "b.c").write_text(
        "void b(void) { danger(1); guard(1); }\n", encoding="utf-8"
    )
    scan_dir = prepare_scan(
        "VAS-9001", repo, rules_dir=rules, workspace_dir=workspace
    )

    first_batch = next_candidates(scan_dir)["tasks"]
    assert [item["rank"] for item in first_batch] == [1, 2]
    assert first_batch[0]["attempt"] == 1
    first = retry_candidate(scan_dir, 1, "analyzer crashed")
    assert first["retryable"] is True
    assert first["state"] == "pending"

    # The other candidate remains usable and is recorded before rank 1 retries.
    assert [item["rank"] for item in next_candidates(scan_dir)["tasks"]] == [2]
    record_analysis(scan_dir, 2, [])
    assert next_candidates(scan_dir)["tasks"][0]["attempt"] == 2
    second = retry_candidate(scan_dir, 1, "invalid analyzer JSON")
    assert second["retryable"] is False
    assert second["state"] == "failed"
    assert next_candidates(scan_dir) == {"done": True, "tasks": []}

    result = finalize_scan(scan_dir)
    report = json.loads((scan_dir / "report.json").read_text(encoding="utf-8"))
    error_files = sorted((scan_dir / "errors").glob("*.txt"))

    assert len(error_files) == 2
    assert result["status"] == "incomplete"
    assert report["coverage"]["failed"] == 1
    assert report["coverage"]["completed"] == 1
    assert report["coverage"]["unfinished"] == 0
