"""Compile Claude authority and materialize one isolated invocation."""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...agent.contracts import (
    AgentPhase,
    AgentTask,
    FileAccess,
    RuntimeCapability,
)
from ...agent.instructions import compose_instructions
from ...agent.schema import descriptive_json_schema
from ...models.anchors import AnchorSynthesisRequest
from ...utils.config import GITHUB_MIRROR_ENABLED
from ...utils.telemetry import propagated_trace_environment
from .artifacts import write_private
from .config import DEFAULT_ENV_ALLOWLIST, ClaudeCodeConfig
from .errors import ClaudeCodeConfigurationError
from .mcp import (
    CASES_DIR_ENV,
    FIXED_DIFF_ENV,
    GITHUB_MIRROR_ENV,
    PROFILE_ENV,
    REPO_PATH_ENV,
    SERVER_NAME,
    SOURCE_ROOT_ENV,
    SYNTHESIS_CONTEXT_ENV,
    SYNTHESIS_LOG_ENV,
    WORKSPACE_ROOT_ENV,
    MCPProfile,
)

COMPACT_HOOK = Path(__file__).resolve().parent / "compact_hook.py"
ACCESS_GUARD = Path(__file__).resolve().parent / "access_guard.py"
WORKSPACE_READ_TOOLS = ("Read", "Grep", "Glob")


@dataclass(frozen=True)
class ClaudePhaseProfile:
    """Native Claude tools and VAMiner MCP tools exposed to one phase."""

    built_in_tools: tuple[str, ...]
    mcp_profile: MCPProfile | None = None
    mcp_tools: tuple[str, ...] = ()


PHASE_PROFILES = MappingProxyType(
    {
        AgentPhase.ISSUE_COLLECTION: ClaudePhaseProfile(
            built_in_tools=("WebSearch", "WebFetch"),
            mcp_profile=MCPProfile.ISSUE,
            mcp_tools=(
                "fetch_cve",
                "fetch_github_issue",
                "parse_commit",
                "clone_repo",
                "search_commit_by_tag",
                "search_commit_by_time",
            ),
        ),
        AgentPhase.ROOT_CAUSE: ClaudePhaseProfile(
            built_in_tools=(*WORKSPACE_READ_TOOLS, "Write"),
            mcp_profile=MCPProfile.ROOT_CAUSE,
            mcp_tools=("read_patch_diff",),
        ),
        AgentPhase.RULE_GENERATION: ClaudePhaseProfile(
            built_in_tools=WORKSPACE_READ_TOOLS,
            mcp_profile=MCPProfile.RULE_GENERATION,
            mcp_tools=("synthesize_ast_grep_anchors",),
        ),
        AgentPhase.AST_GREP_SYNTHESIS: ClaudePhaseProfile(
            built_in_tools=WORKSPACE_READ_TOOLS,
            mcp_profile=MCPProfile.AST_GREP_SYNTHESIS,
            mcp_tools=("run_ast_grep_query",),
        ),
    }
)

ALWAYS_DENIED_TOOLS = (
    "Write",
    "Edit",
    "Task",
    "NotebookEdit",
    "Bash",
    "PowerShell",
    "AskUserQuestion",
)


@dataclass(frozen=True)
class ClaudeTaskPolicy:
    """One immutable compilation of every Claude invocation authority."""

    phase: AgentPhase
    built_in_tools: tuple[str, ...]
    registered_tools: tuple[str, ...]
    mcp_profile: MCPProfile | None
    mcp_tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]


@dataclass(frozen=True)
class PolicyFiles:
    """Invocation files produced from one compiled task policy."""

    system_prompt: Path
    settings: Path
    mcp: Path
    compact_events: Path | None
    synthesis_log: Path | None


class PolicyCompiler:
    """Own all Claude-specific authority, environment, and invocation inputs."""

    def __init__(self, config: ClaudeCodeConfig) -> None:
        self._config = config

    def validate(self, task: AgentTask[Any]) -> None:
        cwd = task.workspace.cwd.resolve()
        if not cwd.is_dir():
            raise ClaudeCodeConfigurationError(f"Claude working directory does not exist: {cwd}")
        if task.context.workspace_root.resolve() != cwd:
            raise ClaudeCodeConfigurationError("Claude working directory must equal the task workspace root")
        self._validate_context_path(
            label="source",
            path=task.context.source_root,
            cwd=cwd,
        )
        if task.context.repo_path is not None:
            self._validate_context_path(
                label="repository",
                path=task.context.repo_path,
                cwd=cwd,
            )
        self._validate_context_path(
            label="cases",
            path=task.context.cases_dir,
            cwd=cwd,
        )
        expected_access = {
            AgentPhase.ISSUE_COLLECTION: FileAccess.NONE,
            AgentPhase.ROOT_CAUSE: FileAccess.READ_WRITE,
            AgentPhase.RULE_GENERATION: FileAccess.READ_ONLY,
            AgentPhase.AST_GREP_SYNTHESIS: FileAccess.READ_ONLY,
        }[task.phase]
        if task.workspace.native_workspace_access is not expected_access:
            raise ClaudeCodeConfigurationError(
                f"{task.phase.value} requires native workspace access " f"{expected_access.value!r}"
            )
        missing = task.required_capabilities - self._config.capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise ClaudeCodeConfigurationError(f"Claude runtime is missing required capabilities: {names}")

    def compile(self, task: AgentTask[Any]) -> ClaudeTaskPolicy:
        """Compile native tools, MCP registration, and denials once."""

        profile = PHASE_PROFILES[task.phase]
        mcp_tools = list(profile.mcp_tools)
        if RuntimeCapability.FIXED_DIFF not in task.required_capabilities and "read_patch_diff" in mcp_tools:
            mcp_tools.remove("read_patch_diff")
        effective_builtins = profile.built_in_tools
        allowed_tools = list(effective_builtins)
        allowed_tools.extend(f"mcp__{SERVER_NAME}__{name}" for name in mcp_tools)
        registered_tools = effective_builtins
        denied = set(ALWAYS_DENIED_TOOLS)
        if not task.workspace.allow_network:
            denied.update(("WebFetch", "WebSearch"))
        denied.difference_update(allowed_tools)
        if any(tool == "Bash" or tool.startswith("Bash(") for tool in allowed_tools):
            denied.discard("Bash")
        return ClaudeTaskPolicy(
            phase=task.phase,
            built_in_tools=effective_builtins,
            registered_tools=registered_tools,
            mcp_profile=profile.mcp_profile if mcp_tools else None,
            mcp_tools=tuple(mcp_tools),
            allowed_tools=tuple(allowed_tools),
            denied_tools=tuple(sorted(denied)),
        )

    @staticmethod
    def _read_roots(task: AgentTask[Any]) -> tuple[Path, ...]:
        if task.phase is AgentPhase.RULE_GENERATION:
            assert task.context.cases_dir is not None
            return (task.context.cases_dir.resolve(),)
        if task.phase is AgentPhase.AST_GREP_SYNTHESIS:
            skill = next(
                (item for item in task.skills if item.name == "ast-grep"),
                None,
            )
            if skill is None:
                raise ClaudeCodeConfigurationError("AST-Grep synthesis requires the ast-grep skill")
            return (
                task.context.workspace_root.resolve(),
                (skill.root / "references").resolve(),
            )
        return (task.context.workspace_root.resolve(),)

    def _runtime_binding(
        self,
        task: AgentTask[Any],
    ) -> str:
        if task.phase is AgentPhase.ISSUE_COLLECTION:
            return """# Runtime Binding

## Claude Code

- Use the `mcp__vaminer__fetch_cve`,
  `mcp__vaminer__fetch_github_issue`, and `mcp__vaminer__parse_commit` tools
  for primary evidence.
- Use `mcp__vaminer__clone_repo` to prepare the verified checkout.
- Use the commit-history MCP tools only for the last-resort history search
  defined by the shared workflow.
- Use `WebSearch` and `WebFetch` only for a concrete evidence gap.
- Return exactly one complete object satisfying the supplied JSON Schema.
"""
        if task.phase is AgentPhase.ROOT_CAUSE:
            diff_line = (
                "- Use `mcp__vaminer__read_patch_diff` before broad exploration."
                if RuntimeCapability.FIXED_DIFF in task.required_capabilities
                else "- No fixed-diff tool is available for this input."
            )
            return f"""# Runtime Binding

## Claude Code

- Locate files or matching lines with `Grep` and `Glob`, then use `Read` on the
  smallest useful line range. ALWAYS specify the explicit root path when reading.
- Read only under the supplied `src` and `cases` roots. Do not inspect runtime
  artifacts, logs, invocation files, or VAMiner framework source.
- Create complete case files with `Write`.
- `Edit` and `Bash` are unavailable.
{diff_line}
- Return exactly one complete object satisfying the supplied JSON Schema.
"""
        if task.phase is AgentPhase.AST_GREP_SYNTHESIS:
            skill = next(
                (item for item in task.skills if item.name == "ast-grep"),
                None,
            )
            if skill is None:
                raise ClaudeCodeConfigurationError("AST-Grep synthesis requires the ast-grep skill")
            skill_path = skill.root / "SKILL.md"
            try:
                skill_text = skill_path.read_text(encoding="utf-8")
                _, _frontmatter, skill_body = skill_text.split("---", 2)
            except (OSError, ValueError) as exc:
                raise ClaudeCodeConfigurationError(f"invalid ast-grep skill: {skill_path}") from exc
            nested_skill_body = "\n".join(
                f"##{line}" if line.startswith("#") else line for line in skill_body.strip().splitlines()
            )
            return f"""# Runtime Binding

## Claude Code

- Use `Read`, `Grep`, and `Glob` only within the corresponding VAS workspace
  root `{task.context.workspace_root.resolve()}` and the supplied ast-grep
  reference root. ALWAYS specify the explicit root path when reading.
- Execute queries only with `mcp__vaminer__run_ast_grep_query`, selecting the
  logical target `cases` or `src`. The tool accepts no shell command or
  filesystem path.
- `Bash`, `Edit`, `Write`, and native delegation tools are unavailable.
- Return exactly one complete object satisfying the supplied JSON Schema.

## Loaded ast-grep skill

{nested_skill_body}
"""
        request_schema = json.dumps(
            descriptive_json_schema(AnchorSynthesisRequest),
            ensure_ascii=False,
            indent=2,
        )
        return f"""# Runtime Binding

## Claude Code

- Use `Read`, `Grep`, and `Glob` only within the cases root supplied in the task
  payload. The workspace source area is `src/`, and it is outside the Rule
  Generator's filesystem authority. ALWAYS specify the explicit `cases` path
  when reading.
- `Bash`, `Edit`, `Write`, and native delegation tools are unavailable.
- Do not inspect runtime artifacts, logs, invocation files, or VAMiner
  framework source.
- Submit one complete `AnchorSynthesisRequest` through
  `mcp__vaminer__synthesize_ast_grep_anchors`. The MCP host performs the
  per-intent fan-out and returns typed results in request order.
- Return exactly one complete object satisfying the supplied JSON Schema.

`AnchorSynthesisRequest` schema:

```json
{request_schema}
```
"""

    def build_environment(self) -> dict[str, str]:
        """Build the fixed environment inherited by Claude and its MCP child."""

        environment = {name: value for name in DEFAULT_ENV_ALLOWLIST if (value := os.environ.get(name)) is not None}
        environment.update(self._config.environment)
        environment.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        environment["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        environment["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
        environment["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        environment["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        return environment

    def resolve_executable(self, environment: Mapping[str, str]) -> str:
        configured = os.fspath(self._config.executable)
        if os.sep in configured:
            path = Path(configured).expanduser().resolve()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ClaudeCodeConfigurationError(f"Claude executable is not executable: {path}")
            return str(path)
        resolved = shutil.which(configured, path=environment.get("PATH"))
        if resolved is None:
            raise ClaudeCodeConfigurationError(f"Claude executable was not found on PATH: {configured}")
        return resolved

    def materialize(
        self,
        temporary_root: Path,
        *,
        task: AgentTask[Any],
        policy: ClaudeTaskPolicy,
        executable: str | None = None,
        model_id: str | None = None,
        trace_compaction: bool = False,
    ) -> PolicyFiles:
        """Render every runtime-owned file consumed by one invocation."""

        system_prompt = temporary_root / "system-prompt.md"
        write_private(
            system_prompt,
            self._compile_system_prompt(task).encode("utf-8"),
        )
        compact_events = temporary_root / "compact-events.jsonl" if trace_compaction else None
        if compact_events is not None:
            write_private(compact_events, b"")
        synthesis_log = (
            temporary_root / "synthesis.log"
            if policy.mcp_profile is MCPProfile.RULE_GENERATION
            else None
        )
        if synthesis_log is not None:
            write_private(synthesis_log, b"")
        return PolicyFiles(
            system_prompt=system_prompt,
            settings=self._materialize_settings(
                temporary_root,
                task=task,
                policy=policy,
                compact_events=compact_events,
            ),
            mcp=self._materialize_mcp(
                temporary_root,
                task=task,
                policy=policy,
                executable=executable,
                model_id=model_id,
                synthesis_log=synthesis_log,
            ),
            compact_events=compact_events,
            synthesis_log=synthesis_log,
        )

    def build_argv(
        self,
        *,
        executable: str,
        task: AgentTask[Any],
        files: PolicyFiles,
        schema_json: str,
        policy: ClaudeTaskPolicy,
        model_id: str | None,
    ) -> list[str]:
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
            self._config.output_format,
            "--no-session-persistence",
            "--json-schema",
            schema_json,
            "--mcp-config",
            str(files.mcp),
            "--strict-mcp-config",
            "--tools",
            ",".join(policy.registered_tools),
            "--allowedTools",
            ",".join(policy.allowed_tools),
            "--disallowedTools",
            ",".join(policy.denied_tools),
            "--permission-mode",
            self._config.permission_mode,
        ]
        if self._config.output_format == "stream-json":
            argv.append("--verbose")
        argv.append("--disable-slash-commands")
        if task.limits.request_limit is not None:
            argv.extend(("--max-turns", str(task.limits.request_limit)))
        if model_id:
            argv.extend(("--model", model_id))
        if self._config.effort:
            argv.extend(("--effort", self._config.effort))
        return argv

    def initial_prompt(self, task: AgentTask[Any]) -> str:
        return task.prompt

    @staticmethod
    def _validate_context_path(
        *,
        label: str,
        path: Path | None,
        cwd: Path,
    ) -> None:
        if path is None:
            return
        resolved = path.resolve()
        if not resolved.is_dir():
            raise ClaudeCodeConfigurationError(f"{label} directory does not exist: {resolved}")
        try:
            resolved.relative_to(cwd)
        except ValueError as exc:
            raise ClaudeCodeConfigurationError(
                f"{label} directory must stay under the Claude working directory: {resolved}"
            ) from exc

    def _materialize_settings(
        self,
        temporary_root: Path,
        *,
        task: AgentTask[Any],
        policy: ClaudeTaskPolicy,
        compact_events: Path | None,
    ) -> Path:
        guard_args = [str(ACCESS_GUARD)]
        for root in self._read_roots(task):
            guard_args.extend(("--root", str(root)))
        if task.phase is AgentPhase.RULE_GENERATION:
            assert task.context.source_root is not None
            guard_args.extend(
                (
                    "--role",
                    "rule-generator",
                    "--restricted-root",
                    str(task.context.source_root.resolve()),
                )
            )
        if task.phase is AgentPhase.ROOT_CAUSE:
            assert task.context.cases_dir is not None
            guard_args.extend(("--write-root", str(task.context.cases_dir.resolve())))
        hooks: dict[str, Any] = {
            "PreToolUse": [
                {
                    "matcher": "Read|Grep|Glob|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": guard_args,
                        }
                    ],
                }
            ]
        }
        if compact_events is not None:
            hooks["PostCompact"] = [
                {
                    "matcher": "auto|manual",
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": [
                                str(COMPACT_HOOK),
                                str(compact_events),
                            ],
                        }
                    ],
                }
            ]
        settings = {
            "permissions": {
                "defaultMode": self._config.permission_mode,
                "allow": list(policy.allowed_tools),
                "deny": list(policy.denied_tools),
            },
            "hooks": hooks,
        }
        path = temporary_root / "runtime-settings.json"
        write_private(
            path,
            (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return path

    def _materialize_mcp(
        self,
        temporary_root: Path,
        *,
        task: AgentTask[Any],
        policy: ClaudeTaskPolicy,
        executable: str | None,
        model_id: str | None,
        synthesis_log: Path | None,
    ) -> Path:
        servers: dict[str, Any] = {}
        if policy.mcp_profile is not None:
            python = self._config.mcp_python or Path(sys.executable).absolute()
            if not python.is_file() or not os.access(python, os.X_OK):
                raise ClaudeCodeConfigurationError(f"MCP Python executable is not executable: {python}")
            mcp_env = {
                "PYTHONPATH": str(self._config.project_root),
                PROFILE_ENV: policy.mcp_profile.value,
                WORKSPACE_ROOT_ENV: str(task.context.workspace_root.resolve()),
                GITHUB_MIRROR_ENV: "true" if GITHUB_MIRROR_ENABLED else "false",
            }
            if policy.mcp_profile is MCPProfile.ROOT_CAUSE:
                fixed_diff = RuntimeCapability.FIXED_DIFF in task.required_capabilities
                mcp_env[FIXED_DIFF_ENV] = "true" if fixed_diff else "false"
                if fixed_diff:
                    assert task.context.repo_path is not None
                    mcp_env[REPO_PATH_ENV] = str(task.context.repo_path.resolve())
            elif policy.mcp_profile is MCPProfile.RULE_GENERATION:
                from .synthesis import ClaudeSynthesisHostContext

                synthesis_context = temporary_root / "rule-synthesis-context.json"
                write_private(
                    synthesis_context,
                    (
                        ClaudeSynthesisHostContext.from_parent(
                            task,
                            self._config,
                            executable=executable,
                            model_id=model_id,
                        ).model_dump_json(indent=2)
                        + "\n"
                    ).encode("utf-8"),
                )
                mcp_env[SYNTHESIS_CONTEXT_ENV] = str(synthesis_context)
                mcp_env.update(propagated_trace_environment())
                assert synthesis_log is not None
                mcp_env[SYNTHESIS_LOG_ENV] = str(synthesis_log)
            elif policy.mcp_profile is MCPProfile.AST_GREP_SYNTHESIS:
                assert task.context.source_root is not None
                assert task.context.cases_dir is not None
                mcp_env[SOURCE_ROOT_ENV] = str(task.context.source_root.resolve())
                mcp_env[CASES_DIR_ENV] = str(task.context.cases_dir.resolve())
            servers[SERVER_NAME] = {
                "type": "stdio",
                "command": str(python),
                "args": ["-m", "src.miner.runtimes.claude.mcp"],
                "env": mcp_env,
                "alwaysLoad": True,
            }

        path = temporary_root / "mcp.json"
        write_private(
            path,
            (
                json.dumps(
                    {"mcpServers": servers},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )
        return path

    def _compile_system_prompt(
        self,
        task: AgentTask[Any],
    ) -> str:
        return compose_instructions(
            task.instructions,
            input_policy=task.input_instructions,
            runtime_binding=self._runtime_binding(task),
        )
