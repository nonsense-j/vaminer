"""Compile one exact Claude invocation from a closed Phase Authority."""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ...agent.contracts import (
    AgentPhase,
    AgentTask,
    AnchorSynthesisAuthority,
    RootCauseAuthority,
    RuleGenerationAuthority,
)
from ...agent.schema import descriptive_json_schema
from ...models.vas import RuleGenerationDraft
from ...utils.config import GITHUB_MIRROR_ENABLED
from ...utils.telemetry import claude_trace_environment, propagated_trace_environment
from .config import ClaudeCodeConfig
from .errors import ClaudeCodeConfigurationError
from .mcp import (
    CASES_DIR_ENV,
    FIXED_DIFF_ENV,
    GITHUB_MIRROR_ENV,
    PROFILE_ENV,
    REPO_PATH_ENV,
    SERVER_NAME,
    SKILL_ROOT_ENV,
    SOURCE_ROOT_ENV,
    SYNTHESIS_CONTEXT_ENV,
    SYNTHESIS_LOG_ENV,
    TOOL_FAILURE_ENV,
    WORKSPACE_ROOT_ENV,
    MCPProfile,
)
from .synthesis import ClaudeSynthesisHostContext


@dataclass(frozen=True, slots=True)
class ClaudeTaskPolicy:
    profile: MCPProfile
    builtins: tuple[str, ...]
    mcp_tools: tuple[str, ...]

    @property
    def qualified_mcp_tools(self) -> tuple[str, ...]:
        return tuple(f"mcp__{SERVER_NAME}__{name}" for name in self.mcp_tools)

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return (*self.builtins, *self.qualified_mcp_tools)


@dataclass(frozen=True, slots=True)
class InvocationFiles:
    system_prompt: Path
    settings: Path
    mcp: Path
    session_id: str
    receipt: Path | None = None
    synthesis_failure: Path | None = None
    synthesis_log: Path | None = None
    tool_failure: Path | None = None


_PROFILES = {
    AgentPhase.ISSUE_COLLECTION: (MCPProfile.ISSUE, ("WebSearch", "WebFetch")),
    AgentPhase.ROOT_CAUSE: (MCPProfile.ROOT_CAUSE, ()),
    AgentPhase.RULE_GENERATION: (MCPProfile.RULE_GENERATION, ()),
    AgentPhase.AST_GREP_SYNTHESIS: (MCPProfile.AST_GREP_SYNTHESIS, ()),
}
_LANGFUSE_PLUGIN_ID = "langfuse-observability@langfuse-observability"


def model_output_type(task: AgentTask[Any]) -> type[BaseModel]:
    return RuleGenerationDraft if task.phase is AgentPhase.RULE_GENERATION else task.output_type


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


class PolicyCompiler:
    """Deep Claude-specific Module for authority, temp files, argv, and environment."""

    def __init__(self, config: ClaudeCodeConfig) -> None:
        self.config = config

    def validate(self, task: AgentTask[Any]) -> None:
        workspace = task.workspace_root.resolve()
        if not workspace.is_dir():
            raise ClaudeCodeConfigurationError(f"Claude workspace does not exist: {workspace}")
        paths: list[Path] = []
        if isinstance(task.authority, (RootCauseAuthority, RuleGenerationAuthority, AnchorSynthesisAuthority)):
            paths.extend((task.authority.source_root, task.authority.cases_dir))
        if isinstance(task.authority, RootCauseAuthority) and task.authority.repo_path is not None:
            paths.append(task.authority.repo_path)
        for path in paths:
            resolved = path.resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError as exc:
                raise ClaudeCodeConfigurationError(f"task path must stay inside workspace: {resolved}") from exc
            if not resolved.is_dir():
                raise ClaudeCodeConfigurationError(f"task directory does not exist: {resolved}")

    def compile(self, task: AgentTask[Any]) -> ClaudeTaskPolicy:
        profile, builtins = _PROFILES[task.phase]
        mcp_tools = tuple(name for name in task.tools if name not in {"web_search", "web_fetch"})
        return ClaudeTaskPolicy(profile=profile, builtins=builtins, mcp_tools=mcp_tools)

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        environment.update(propagated_trace_environment())
        environment.update(claude_trace_environment())
        return environment

    def resolve_executable(self, environment: dict[str, str]) -> str:
        configured = os.fspath(self.config.executable)
        if os.sep in configured:
            path = Path(configured).expanduser().resolve()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ClaudeCodeConfigurationError(f"Claude executable is not executable: {path}")
            return str(path)
        resolved = shutil.which(configured, path=environment.get("PATH"))
        if resolved is None:
            raise ClaudeCodeConfigurationError(f"Claude executable was not found on PATH: {configured}")
        return resolved

    @staticmethod
    def runtime_binding(task: AgentTask[Any]) -> str:
        if task.phase is AgentPhase.ISSUE_COLLECTION:
            detail = "Use the scoped issue MCP tools first; WebSearch/WebFetch are fallback evidence tools only."
        elif task.phase is AgentPhase.ROOT_CAUSE:
            detail = "Use scoped repository reads and typed Case Artifact operations; generic filesystem tools are unavailable."
        elif task.phase is AgentPhase.RULE_GENERATION:
            detail = "Read only Case Artifacts and submit complete plans through `mcp__vaminer__synthesize_anchor_plan`."
        else:
            detail = "Use scoped source/case/skill reads and execute queries only through `mcp__vaminer__run_ast_grep_query`."
        return f"""# Runtime Binding

## Claude Code

- {detail}
- Canonical MCP tool names in the shared instructions map to `mcp__{SERVER_NAME}__<tool_name>` in this runtime.
- Shell, generic filesystem writes, native delegation, and undeclared tools are unavailable.
- Return exactly one complete object satisfying the supplied JSON Schema.
"""

    def _mcp_environment(
        self,
        task: AgentTask[Any],
        policy: ClaudeTaskPolicy,
        *,
        synthesis_context: Path | None,
        synthesis_log: Path | None,
        tool_failure: Path | None,
    ) -> dict[str, str]:
        environment = {
            PROFILE_ENV: policy.profile.value,
            WORKSPACE_ROOT_ENV: str(task.workspace_root.resolve()),
            GITHUB_MIRROR_ENV: "1" if GITHUB_MIRROR_ENABLED else "0",
            "PYTHONPATH": str(self.config.project_root),
            **propagated_trace_environment(),
        }
        authority = task.authority
        if isinstance(authority, (RootCauseAuthority, RuleGenerationAuthority, AnchorSynthesisAuthority)):
            environment[SOURCE_ROOT_ENV] = str(authority.source_root.resolve())
            environment[CASES_DIR_ENV] = str(authority.cases_dir.resolve())
        if isinstance(authority, RootCauseAuthority):
            environment[FIXED_DIFF_ENV] = "1" if authority.fixed_diff else "0"
            if authority.repo_path is not None:
                environment[REPO_PATH_ENV] = str(authority.repo_path.resolve())
        if isinstance(authority, AnchorSynthesisAuthority):
            environment[SKILL_ROOT_ENV] = str(authority.skill_root.resolve())
        if synthesis_context is not None:
            environment[SYNTHESIS_CONTEXT_ENV] = str(synthesis_context)
        if synthesis_log is not None:
            environment[SYNTHESIS_LOG_ENV] = str(synthesis_log)
        if tool_failure is not None:
            environment[TOOL_FAILURE_ENV] = str(tool_failure)
        return environment

    def materialize(
        self,
        temporary_root: Path,
        *,
        task: AgentTask[Any],
        policy: ClaudeTaskPolicy,
        executable: str,
        model_id: str,
    ) -> InvocationFiles:
        system_prompt = temporary_root / "system-prompt.md"
        settings = temporary_root / "settings.json"
        mcp = temporary_root / "mcp.json"
        session_id = str(uuid.uuid4())
        receipt = temporary_root / "synthesis-receipt.json" if task.phase is AgentPhase.RULE_GENERATION else None
        synthesis_failure = (
            temporary_root / "synthesis-failure.json"
            if task.phase is AgentPhase.RULE_GENERATION
            else None
        )
        synthesis_log = temporary_root / "synthesis.log" if receipt is not None else None
        tool_failure = (
            temporary_root / "tool-failure.json"
            if task.phase is AgentPhase.AST_GREP_SYNTHESIS
            else None
        )
        synthesis_context = None
        if receipt is not None:
            synthesis_context = temporary_root / "synthesis-context.json"
            context = ClaudeSynthesisHostContext.from_parent(
                task,
                self.config,
                receipt_path=receipt,
                failure_path=synthesis_failure,
                executable=executable,
                model_id=model_id,
            )
            _write_private(synthesis_context, context.model_dump_json(indent=2))
            assert synthesis_log is not None
            _write_private(synthesis_log, "")

        _write_private(system_prompt, task.instructions.render(self.runtime_binding(task)))
        settings_payload: dict[str, Any] = {
            "permissions": {"defaultMode": "dontAsk", "allow": list(policy.allowed_tools)},
            "enableAllProjectMcpServers": False,
        }
        if not claude_trace_environment():
            # A user-installed plugin must not emit standalone traces when the
            # parent VAMiner trace is disabled or unavailable.
            settings_payload["enabledPlugins"] = {_LANGFUSE_PLUGIN_ID: False}
        _write_private(settings, json.dumps(settings_payload, indent=2))
        # Keep the virtual-environment entry point intact. Resolving this symlink
        # selects the base interpreter and drops the venv's installed packages.
        python = str(self.config.mcp_python or Path(sys.executable).absolute())
        _write_private(
            mcp,
            json.dumps(
                {
                    "mcpServers": {
                        SERVER_NAME: {
                            "type": "stdio",
                            "command": python,
                            "args": ["-m", "src.miner.runtimes.claude.mcp"],
                            "env": self._mcp_environment(
                                task,
                                policy,
                                synthesis_context=synthesis_context,
                                synthesis_log=synthesis_log,
                                tool_failure=tool_failure,
                            ),
                        }
                    }
                },
                indent=2,
            ),
        )
        return InvocationFiles(
            system_prompt=system_prompt,
            settings=settings,
            mcp=mcp,
            session_id=session_id,
            receipt=receipt,
            synthesis_failure=synthesis_failure,
            synthesis_log=synthesis_log,
            tool_failure=tool_failure,
        )

    def argv(
        self,
        *,
        executable: str,
        task: AgentTask[Any],
        policy: ClaudeTaskPolicy,
        files: InvocationFiles,
        model_id: str,
        resume: bool = False,
    ) -> list[str]:
        schema = json.dumps(descriptive_json_schema(model_output_type(task)), ensure_ascii=False)
        session_args = ["--resume", files.session_id] if resume else ["--session-id", files.session_id]
        argv = [
            executable,
            "--print",
            "--setting-sources",
            "user",
            "--settings",
            str(files.settings),
            "--system-prompt-file",
            str(files.system_prompt),
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--verbose",
            *session_args,
            "--json-schema",
            schema,
            "--mcp-config",
            str(files.mcp),
            "--strict-mcp-config",
            "--tools",
            ",".join(policy.builtins),
            "--allowedTools",
            ",".join(policy.allowed_tools),
            "--permission-mode",
            "dontAsk",
            "--disable-slash-commands",
        ]
        if self.config.model is not None:
            argv.extend(("--model", model_id))
        if task.limits.request_limit is not None:
            argv.extend(("--max-turns", str(task.limits.request_limit)))
        if self.config.effort is not None:
            argv.extend(("--effort", self.config.effort))
        return argv


def cleanup_session_transcript(session_id: str, environment: dict[str, str]) -> tuple[Path, ...]:
    """Remove only the transcript artifacts owned by one completed invocation."""

    try:
        if str(uuid.UUID(session_id)) != session_id:
            return ()
    except ValueError:
        return ()

    configured_root = environment.get("CLAUDE_CONFIG_DIR")
    if configured_root:
        config_root = Path(configured_root).expanduser().resolve()
    else:
        config_root = Path(environment.get("HOME") or Path.home()).expanduser().resolve() / ".claude"
    projects_root = config_root / "projects"
    if not projects_root.is_dir():
        return ()

    removed: list[Path] = []
    resolved_projects_root = projects_root.resolve()
    for transcript in projects_root.glob(f"*/{session_id}.jsonl"):
        try:
            resolved_transcript = transcript.resolve()
            resolved_transcript.relative_to(resolved_projects_root)
            if resolved_transcript.name != f"{session_id}.jsonl":
                continue
            resolved_transcript.unlink(missing_ok=True)
            removed.append(resolved_transcript)
            session_dir = resolved_transcript.with_suffix("")
            if session_dir.is_dir():
                shutil.rmtree(session_dir)
                removed.append(session_dir)
        except (OSError, ValueError):
            continue
    return tuple(removed)


__all__ = [
    "ClaudeTaskPolicy",
    "InvocationFiles",
    "PolicyCompiler",
    "cleanup_session_transcript",
    "model_output_type",
]
