"""Private diagnostic artifacts for Claude invocations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...agent.contracts import AgentPhase, AgentTask
from ...utils.config import MINER_OUTPUT_DIR
from ...utils.workspace import safe_input_id
from .config import ClaudeCodeConfig


class ArtifactPolicy(Protocol):
    """Policy fields included in the invocation audit."""

    phase: AgentPhase
    registered_tools: tuple[str, ...]
    mcp_tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    main_allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    main_agent: str | None
    allowed_subagent: str | None
    native_delegation_tool: str | None

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|auth[_-]?token|secret)\s*[=:]\s*)[^\s,;]+"),
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[-_]?key|authorization|auth[-_]?token|access[-_]?token|password|secret|cookie)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AttemptArtifacts:
    prompt_path: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    events_path: Path | None
    validation_errors_path: Path | None = None


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "... <clipped>"


def redact(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r"\1<redacted>", result)
        else:
            result = pattern.sub("<redacted>", result)
    return result


def sanitize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY_RE.search(str(key)) else sanitize_json(item)
            for key, item in value.items()
            if not _is_cost_key(key)
        }
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def _is_cost_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return normalized in {"cost", "totalcost", "costdetails"} or normalized.endswith("costusd")


def write_private(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)


class ArtifactStore:
    """Persist one Claude invocation behind a small artifact interface."""

    def __init__(self, config: ClaudeCodeConfig, *, runtime_id: str) -> None:
        self._config = config
        self._runtime_id = runtime_id

    def create_run(self, task: AgentTask[Any]) -> Path:
        artifact_root = self._config.artifact_root
        if artifact_root is None:
            artifact_root = (task.context.output_root or MINER_OUTPUT_DIR) / "artifacts"
        input_id = safe_input_id(task.context.input_id or task.task_id)
        trace_id = task.context.trace_id or uuid.uuid4().hex
        phase = task.phase.value.replace("_", "-")
        run_dir = (
            artifact_root.expanduser().resolve()
            / self._runtime_id
            / input_id
            / trace_id
            / phase
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def persist_invocation(
        self,
        run_dir: Path,
        *,
        task: AgentTask[Any],
        policy: ArtifactPolicy,
        system_prompt_file: Path,
        model_id: str,
    ) -> Path:
        invocation_path = run_dir / "invocation.json"
        invocation = {
            "task_id": task.task_id,
            "phase": task.phase.value,
            "runtime": self._runtime_id,
            "input_id": run_dir.parents[1].name,
            "trace_id": run_dir.parent.name,
            "model": model_id,
            "cwd": str(task.workspace.cwd.resolve()),
            "prompt_sha256": hashlib.sha256(system_prompt_file.read_bytes()).hexdigest(),
            "tools": {
                "registered": list(policy.registered_tools),
                "allowed": list(policy.allowed_tools),
                "main_allowed": list(policy.main_allowed_tools),
                "denied": list(policy.denied_tools),
                "mcp": list(policy.mcp_tools),
                "main_agent": policy.main_agent,
                "allowed_subagent": policy.allowed_subagent,
                "native_delegation_tool": policy.native_delegation_tool,
            },
            "isolation": {
                "setting_sources": ["user"],
                "strict_mcp_config": True,
                "session_persistence": False,
                "auto_memory": False,
                "claude_ai_mcp_servers": False,
                "subagent_depth": self._config.max_subagent_depth,
                "subagent_count": self._config.max_subagents_per_session,
                "subagent_concurrency": self._config.max_concurrent_subagents,
            },
        }
        write_private(
            invocation_path,
            (json.dumps(invocation, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return invocation_path

    def prepare_attempt(
        self,
        run_dir: Path,
        *,
        attempt: int,
    ) -> AttemptArtifacts:
        stderr_path = run_dir / f"attempt-{attempt}.stderr.txt"
        validation_errors_path = run_dir / f"attempt-{attempt}.validation-errors.txt"
        return AttemptArtifacts(
            prompt_path=None,
            stdout_path=None,
            stderr_path=stderr_path,
            events_path=None,
            validation_errors_path=validation_errors_path,
        )

    def persist_validation_errors(
        self,
        artifacts: AttemptArtifacts,
        errors: Sequence[str],
    ) -> Path:
        """Persist application-level output validation errors for one attempt."""

        if artifacts.validation_errors_path is None:
            raise ValueError("validation error artifact path is unavailable")
        rendered = "Structured output validation failed:\n" + "".join(
            f"- {redact(error)}\n" for error in errors
        )
        write_private(
            artifacts.validation_errors_path,
            rendered.encode("utf-8"),
        )
        return artifacts.validation_errors_path
