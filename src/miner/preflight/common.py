"""Runtime-neutral VAMiner preflight checks."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ..mining.tasks import AST_GREP_SKILL_ROOT
from ..tools.ast_grep import run_ast_grep
from ..utils.config import PROJECT_ROOT
from .models import CheckResult


def check_python() -> CheckResult:
    version = sys.version_info
    if version < (3, 12):
        return CheckResult.failed("python", "Python 3.12 or newer is required", detail=sys.version.split()[0])
    return CheckResult.passed("python", f"Python {version.major}.{version.minor}.{version.micro}")


def _writable_ancestor(path: Path) -> Path | None:
    current = path.expanduser().resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() else None


def check_paths(*, workspace_dir: Path, output_dir: Path, rules_dir: Path) -> CheckResult:
    problems: list[str] = []
    for label, path in (
        ("workspace", workspace_dir),
        ("output", output_dir),
        ("rules", rules_dir),
    ):
        ancestor = _writable_ancestor(path)
        if ancestor is None or not ancestor.is_dir():
            problems.append(f"{label}: no existing parent directory")
        elif not os.access(ancestor, os.W_OK | os.X_OK):
            problems.append(f"{label}: parent is not writable ({ancestor})")
    if problems:
        return CheckResult.failed("paths", "Miner output paths are not writable", detail="; ".join(problems))
    return CheckResult.passed("paths", "Workspace, output, and rules paths have writable parents")


def check_project_assets() -> CheckResult:
    required = (
        PROJECT_ROOT / "src" / "miner" / "instructions" / "issue_collector.md",
        PROJECT_ROOT / "src" / "miner" / "instructions" / "root_cause_analyzer.md",
        PROJECT_ROOT / "src" / "miner" / "instructions" / "rule_generator.md",
        AST_GREP_SKILL_ROOT / "SKILL.md",
        PROJECT_ROOT / "src" / ".vaminer" / "skills" / "vas-scanner" / "scripts" / "engine.py",
    )
    missing = [path.relative_to(PROJECT_ROOT).as_posix() for path in required if not path.is_file()]
    if missing:
        return CheckResult.failed("assets", "Required Miner assets are missing", detail=", ".join(missing))
    return CheckResult.passed("assets", "Instructions, ast-grep skill, and scanner assets are present")


def check_git() -> CheckResult:
    executable = shutil.which("git")
    if executable is None:
        return CheckResult.failed("git", "git was not found on PATH")
    return CheckResult.passed("git", f"git executable found at {executable}")


def check_rg() -> CheckResult:
    executable = shutil.which("rg")
    if executable is None:
        return CheckResult.failed("rg", "ripgrep (rg) was not found on PATH")
    return CheckResult.passed("rg", f"ripgrep executable found at {executable}")


def check_ast_grep(*, timeout_seconds: float) -> CheckResult:
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="vaminer-preflight-ast-grep-") as raw_temp:
            root = Path(raw_temp)
            (root / "probe.c").write_text("int vaminer_preflight(void) { return 0; }\n", encoding="utf-8")
            result = run_ast_grep(
                root,
                language="c",
                query_type="pattern",
                query="return 0;",
                output="count",
                timeout_seconds=max(1, round(timeout_seconds)),
            )
        if result.get("match_count") != 1:
            return CheckResult.failed(
                "ast-grep",
                "ast-grep started but did not return the expected probe match",
                detail=f"match_count={result.get('match_count')!r}",
            )
    except Exception as exc:  # noqa: BLE001 - diagnostics must turn failures into a report.
        return CheckResult.failed("ast-grep", "ast-grep functional probe failed", detail=f"{type(exc).__name__}: {exc}")
    return CheckResult.passed(
        "ast-grep",
        "ast-grep executed a C query successfully",
        duration_ms=round((time.monotonic() - started) * 1000),
    )


__all__ = ["check_ast_grep", "check_git", "check_paths", "check_project_assets", "check_python", "check_rg"]
