"""Tests for the standalone task-artifact VAS scanner protocol."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = PROJECT_ROOT / "src" / ".vaminer" / "skills" / "vas-scanner" / "scripts"
import sys

sys.path.insert(0, str(SCRIPT_DIR))

import config as scanner_config  # noqa: E402
import engine as scanner_engine  # noqa: E402
from core import finalize_scan, prepare_scan, record_analysis  # noqa: E402
from prompt import TASK_TEMPLATE  # noqa: E402


def require_ast_grep() -> None:
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")


def test_scanner_defaults_to_strict_ast_grep_path():
    assert scanner_config.AST_GREP_CLI_PATH == "ast-grep"


def test_configured_ast_grep_missing_does_not_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(scanner_engine.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        scanner_engine.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("configured paths must not trigger installation"),
    )

    with pytest.raises(scanner_engine.AnchorExecutionError, match="was not found"):
        scanner_engine.resolve_ast_grep("missing-ast-grep", tmp_path / "tools", install=True)


def test_ast_grep_resolution_never_falls_back_to_sg(monkeypatch: pytest.MonkeyPatch):
    resolved: list[str] = []

    def missing(name: str) -> None:
        resolved.append(name)
        return None

    monkeypatch.setattr(scanner_engine.shutil, "which", missing)

    with pytest.raises(scanner_engine.AnchorExecutionError, match="was not found"):
        scanner_engine.find_ast_grep()

    assert resolved == ["ast-grep"]


def test_windows_ast_grep_shims_are_rejected(monkeypatch: pytest.MonkeyPatch):
    cmd_shim = r"C:\Users\test\bin\ast-grep.cmd"
    monkeypatch.setattr(scanner_engine.sys, "platform", "win32")
    monkeypatch.setattr(scanner_engine.shutil, "which", lambda _name: cmd_shim)

    with pytest.raises(scanner_engine.AnchorExecutionError, match="native .exe"):
        scanner_engine.find_ast_grep()


def test_none_installs_and_reuses_private_ast_grep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    install_dir = tmp_path / ".tool" / "ast_grep"
    commands: list[list[str]] = []

    def which(name: str) -> str | None:
        candidate = Path(name)
        return str(candidate) if candidate.is_file() else None

    def run(command: list[str], **_kwargs):
        commands.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="ast-grep 0.45.3\n", stderr="")
        binary = install_dir / "bin" / "ast-grep"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("binary", encoding="utf-8")
        binary.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(scanner_engine.shutil, "which", which)
    monkeypatch.setattr(scanner_engine.subprocess, "run", run)

    installed = scanner_engine.resolve_ast_grep(None, install_dir, install=True)
    reused = scanner_engine.resolve_ast_grep(None, install_dir, install=False)

    assert installed == reused == str(install_dir / "bin" / "ast-grep")
    install_commands = [command for command in commands if command[-1] != "--version"]
    assert len(install_commands) == 1
    assert install_commands[0][:3] == [sys.executable, "-m", "pip"]
    assert install_commands[0][-1] == scanner_engine.AST_GREP_CLI_REQUIREMENT
    assert install_commands[0][install_commands[0].index("--target") + 1] == str(install_dir)


def test_none_requires_preflight_before_prepare(tmp_path: Path):
    with pytest.raises(scanner_engine.AnchorExecutionError, match="run scanner preflight first"):
        scanner_engine.resolve_ast_grep(None, tmp_path / "missing", install=False)


def make_rule(path: Path, vas_id: str = "VAS-9001") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{vas_id}.json").write_text(json.dumps({
        "vas_id": vas_id,
        "category": "SECURITY",
        "language": "c",
        "summary": "Dangerous operations need a guard.",
        "scenarios": {"unsafe": ["The operation is unguarded."], "safe": ["The operation is guarded."]},
        "anchors": [{
            "id": "danger-call", "behavior_weight": 5, "query_weight": 5,
            "type": "pattern", "query": "danger($A);", "behavior": "Calls danger.",
            "inspect_hint": "Check whether the call is guarded.",
        }],
    }), encoding="utf-8")


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    require_ast_grep()
    repo = tmp_path / "repo"
    (repo / ".vas").mkdir(parents=True)
    (repo / ".vas" / "repository_overview.md").write_text(
        "# Repository Overview\n\n## Code Layout\n- source\n\n"
        "## Module Relations\n- none\n\n## Key Interfaces\n- main\n",
        encoding="utf-8",
    )
    (repo / "a.c").write_text("void a(void) { danger(1); }\n", encoding="utf-8")
    rules = tmp_path / "rules"
    make_rule(rules)
    return repo, rules


def prepare_basic(tmp_path: Path) -> Path:
    repo, rules = make_repo(tmp_path)
    result = prepare_scan("VAS-9001", repo, rules_dir=rules, workspace_dir=tmp_path / "workspace")
    return Path(result["run_dir"])


def payload(reports: list[dict] | None = None) -> dict:
    return {
        "anchorFacts": [{
            "anchorId": "danger-call", "startLine": 1, "status": "SAFE",
            "fact": "The call is covered by the required guard.",
        }],
        "reports": reports or [],
    }


def test_prepare_writes_manifest_tasks_and_empty_artifact_dirs(tmp_path: Path):
    run_dir = prepare_basic(tmp_path)
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "repository_overview.md").is_file()
    assert (run_dir / "tasks" / "TASK-0001.md").is_file()
    assert (run_dir / "shared_checks").is_dir()
    assert (run_dir / "results").is_dir()
    assert not (run_dir / "scan.json").exists()
    assert not (run_dir / ".scan.lock").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_count"] == 1
    assert manifest["config"]["concurrency"] >= 1
    assert "state" not in manifest["tasks"][0]
    task = (run_dir / "tasks" / "TASK-0001.md").read_text(encoding="utf-8")
    assert isinstance(TASK_TEMPLATE, str)
    assert "{task_id}" in TASK_TEMPLATE
    assert "scan.py" in task
    assert "Shared checks" in task
    assert "VAS-9001" in task
    assert "danger-call" in task
    assert "Check whether the call is guarded." in task
    assert "Dangerous operations need a guard." in task
    assert "TASK-0001" in task
    assert str(run_dir / "results" / "TASK-0001.json") in task


def test_record_writes_facts_and_reports_then_finalize(tmp_path: Path):
    run_dir = prepare_basic(tmp_path)
    record_analysis(run_dir, "TASK-0001", payload())
    assert (run_dir / "shared_checks" / "a.c.md").is_file()
    assert json.loads((run_dir / "results" / "TASK-0001.json").read_text(encoding="utf-8")) == []
    result = finalize_scan(run_dir)
    assert result["status"] == "complete"
    assert json.loads((run_dir / "report.json").read_text(encoding="utf-8")) == []


def test_prepare_preserves_template_output_contract(tmp_path: Path):
    run_dir = prepare_basic(tmp_path)
    task = (run_dir / "tasks" / "TASK-0001.md").read_text(encoding="utf-8")
    template_contract = TASK_TEMPLATE.split("```json\n", 1)[1].split("```", 1)[0]
    rendered_contract = task.split("```json\n", 1)[1].split("```", 1)[0]
    assert rendered_contract == template_contract.format()


def test_generated_record_heredoc_preserves_json_from_copied_skill(tmp_path: Path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the generated heredoc")
    repo, rules = make_repo(tmp_path / "target with spaces")
    copied_skill = tmp_path / "scanner with 'quotes' and $literal"
    shutil.copytree(SCRIPT_DIR.parent, copied_skill)
    shutil.copyfile(rules / "VAS-9001.json", copied_skill / "rules" / "VAS-9001.json")
    script = copied_skill / "scripts" / "scan.py"
    prepared = subprocess.run(
        [sys.executable, "-S", str(script), "prepare", "VAS-9001", str(repo)],
        text=True, capture_output=True, check=True, timeout=30,
    )
    run_dir = Path(json.loads(prepared.stdout)["run_dir"])
    task = (run_dir / "tasks" / "TASK-0001.md").read_text(encoding="utf-8")
    heredoc = task.split("```bash\n", 1)[1].split("```", 1)[0]
    command = heredoc.splitlines()[0].removesuffix(" <<'JSON'")
    assert shlex.split(command) == ["python3", str(script), "record", str(run_dir), "TASK-0001"]

    result = payload()
    result["anchorFacts"][0]["fact"] = (
        "Literal source: $PATH, $(printf expanded), `printf expanded`, and \\\\n."
    )
    heredoc = heredoc.replace("<your JSON object>", json.dumps(result, indent=2))
    recorded = subprocess.run(
        [bash, "-c", heredoc], cwd=tmp_path,
        text=True, capture_output=True, check=True, timeout=30,
    )
    assert json.loads(recorded.stdout)["task_id"] == "TASK-0001"
    assert json.loads((run_dir / "results" / "TASK-0001.json").read_text(encoding="utf-8")) == []
    facts = (run_dir / "shared_checks" / "a.c.md").read_text(encoding="utf-8")
    assert result["anchorFacts"][0]["fact"] in facts


def test_record_rejects_invalid_schema_without_result(tmp_path: Path):
    run_dir = prepare_basic(tmp_path)
    with pytest.raises(ValueError, match="exactly these fields"):
        record_analysis(run_dir, "TASK-0001", {"reports": []})
    assert not (run_dir / "results" / "TASK-0001.json").exists()


def test_finalize_reports_missing_task_and_does_not_write_partial_report(tmp_path: Path):
    run_dir = prepare_basic(tmp_path)
    with pytest.raises(RuntimeError, match="missing tasks: TASK-0001"):
        finalize_scan(run_dir)
    assert not (run_dir / "report.json").exists()


def test_finalize_deduplicates_reports_in_task_order(tmp_path: Path):
    run_dir = prepare_basic(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["tasks"].append({
        "task_id": "TASK-0002", "task_file": "tasks/TASK-0002.md", "candidate_file": "a.c",
    })
    manifest["task_count"] = 2
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = {
        "buggyFilePath": "a.c", "defectLevel": 1, "defectType": "Missing guard",
        "functionName": "a", "mainBuggyLine": 1, "description": "unsafe",
        "mainBuggyCode": "danger(1);", "fixSuggestion": "guard it", "fixCode": "guard(1);",
        "events": [{"description": "call", "line": 1, "main": True, "path": "a.c",
                    "mainBuggyCode": "danger(1);", "codeContext": "a"}],
    }
    record_analysis(run_dir, "TASK-0001", payload([report]))
    record_analysis(run_dir, "TASK-0002", payload([report]))
    result = finalize_scan(run_dir)
    assert result["report_count"] == 1
    assert len(json.loads((run_dir / "report.json").read_text(encoding="utf-8"))) == 1
