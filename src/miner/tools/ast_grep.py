"""Workspace-confined ast-grep operations for SDK and CLI runtimes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from threading import Lock
from types import ModuleType
from typing import Any, Literal

from pydantic_ai import ModelRetry, RunContext, ToolFailed

from ..configs import (
    MINER_AST_GREP_MAX_SAMPLE_SIZE,
    MINER_AST_GREP_SAMPLE_SIZE,
    MINER_AST_GREP_TIMEOUT_SECONDS,
    MINER_SRC_DIR,
)
from ..core.context import MinerContext
from ..utils.models import QueryType

RUNNER_PATH = MINER_SRC_DIR / "skills" / "ast-grep" / "scripts" / "runner.py"
_RUNNER_MODULE_NAME = "_vaminer_ast_grep_skill_runner"
_RUNNER_LOAD_LOCK = Lock()


class AstGrepToolError(RuntimeError):
    """Raised when the shared ast-grep runner cannot execute a valid query."""


def _load_runner() -> ModuleType:
    with _RUNNER_LOAD_LOCK:
        existing = sys.modules.get(_RUNNER_MODULE_NAME)
        if existing is not None:
            return existing
        if not RUNNER_PATH.is_file():
            raise FileNotFoundError(f"ast-grep skill runner not found: {RUNNER_PATH}")
        spec = importlib.util.spec_from_file_location(_RUNNER_MODULE_NAME, RUNNER_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load ast-grep skill runner: {RUNNER_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_RUNNER_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_RUNNER_MODULE_NAME, None)
            raise
        return module


def resolve_workspace_target(workspace_root: Path, target_dir: str) -> Path:
    """Resolve an existing target directory without escaping ``workspace_root``."""
    if not target_dir.strip():
        raise ValueError("target_dir must be a non-empty directory path")
    root = workspace_root.resolve()
    candidate = Path(target_dir)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"target_dir must stay inside the active workspace: {target_dir}") from exc
    if not resolved.is_dir():
        raise ValueError(f"target_dir is not an existing directory: {target_dir}")
    return resolved


def _resolve_target(workspace_root: Path, target_dir: str) -> Path:
    """Backward-compatible Pydantic tool path resolver."""
    try:
        return resolve_workspace_target(workspace_root, target_dir)
    except ValueError as exc:
        raise ToolFailed(str(exc)) from exc


def run_ast_grep_query(
    workspace_root: Path,
    target_dir: str,
    *,
    language: str,
    query_type: str,
    query: str,
    output: Literal["count", "sample", "full"] = "sample",
    sample_size: int | None = None,
    default_sample_size: int = MINER_AST_GREP_SAMPLE_SIZE,
    max_sample_size: int = MINER_AST_GREP_MAX_SAMPLE_SIZE,
    timeout_seconds: int = MINER_AST_GREP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one ast-grep query using only explicit, runtime-neutral inputs."""
    resolved = resolve_workspace_target(workspace_root, target_dir)
    if not language.strip():
        raise ValueError("language must be non-empty")
    if query_type not in {"pattern", "rule"}:
        raise ValueError(f"unsupported query_type: {query_type!r}")
    if output not in {"count", "sample", "full"}:
        raise ValueError(f"unsupported output mode: {output!r}")

    resolved_sample_size = default_sample_size
    if output == "sample" and sample_size is not None:
        resolved_sample_size = sample_size
    if output == "sample" and not 1 <= resolved_sample_size <= max_sample_size:
        raise ValueError(
            f"sample_size must be between 1 and {max_sample_size}; received {resolved_sample_size}."
        )

    runner = _load_runner()
    try:
        return runner.run_ast_grep(
            resolved,
            language=language,
            query_type=query_type,
            query=query,
            output=output,
            sample_size=resolved_sample_size,
            timeout_seconds=timeout_seconds,
        )
    except runner.AstGrepRunnerError as exc:
        raise AstGrepToolError(str(exc)) from exc


def run_ast_grep(
    ctx: RunContext[MinerContext],
    target_dir: str,
    query_type: QueryType,
    query: str,
    output: Literal["count", "sample", "full"] = "sample",
    sample_size: int | None = None,
) -> dict[str, Any]:
    """Run one ast-grep query against a workspace directory.

    Args:
        target_dir: Absolute or active-workspace-relative directory to scan.
        query_type: Use pattern for a raw ast-grep pattern or rule for a YAML rule body.
        query: Raw pattern or YAML rule body.
        output: Return only counts, a bounded sample, or every normalized match.
        sample_size: Number of matches in sample mode; defaults to the Miner setting.
    """
    root_cause = ctx.deps.root_cause
    if root_cause is None:
        raise ToolFailed("root_cause is required to select the ast-grep language")
    try:
        return run_ast_grep_query(
            ctx.deps.workspace_root,
            target_dir,
            language=root_cause.language.value,
            query_type=query_type.value,
            query=query,
            output=output,
            sample_size=sample_size,
        )
    except ValueError as exc:
        if str(exc).startswith("sample_size must be between"):
            raise ModelRetry(str(exc)) from exc
        raise ToolFailed(str(exc)) from exc
    except AstGrepToolError as exc:
        raise ToolFailed(str(exc)) from exc


__all__ = [
    "RUNNER_PATH",
    "AstGrepToolError",
    "resolve_workspace_target",
    "run_ast_grep",
    "run_ast_grep_query",
]
