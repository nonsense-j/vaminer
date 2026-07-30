"""Claude Code CLI adapter for runtime-neutral Miner agent tasks.

The adapter intentionally treats Claude Code as an isolated subprocess protocol:
runtime-owned instructions, schemas, MCP configuration, and skills are supplied
explicitly for each run. Repository content and user project settings are never
allowed to become implicit instructions.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...agent.contracts import (
    AgentRunResult,
    AgentTask,
    OutputT,
    RuntimeArtifacts,
)
from ...agent.schema import descriptive_json_schema
from ...utils.log import logger
from .artifacts import ArtifactStore, AttemptArtifacts, write_private
from .config import ClaudeCodeConfig
from .errors import (
    ClaudeCodeConfigurationError,
    ClaudeCodeRequestLimitError,
    ClaudeCodeValidationError,
)
from .policy import PolicyCompiler
from .process import ProcessResult, ProcessRunner
from .protocol import (
    ParsedOutput,
    ProtocolDecoder,
    RequestCounter,
    StreamMonitor,
    aggregate_attempt_usage,
    subagent_events,
    sum_optional,
    validate_model_identity,
    validate_output,
)
from .telemetry import ClaudeTaskTrace, trace_claude_task

class ClaudeCodeRuntime:
    """Execute one :class:`AgentTask` with an isolated Claude Code CLI process."""

    runtime_id = "claude-code"

    def __init__(self, config: ClaudeCodeConfig | None = None) -> None:
        self.config = config or ClaudeCodeConfig()
        self.capabilities = self.config.capabilities

    def model_id_for(self, task: AgentTask[Any]) -> str:
        """Return the configured model identity used for cache names."""

        return self._resolve_model(task)

    def _resolve_model(self, task: AgentTask[Any]) -> str:
        model_id = (task.model_hint or self.config.model or "").strip()
        if not model_id:
            raise ClaudeCodeConfigurationError(
                "Claude model is required; set CLAUDE_CODE_MODEL or pass --claude-model"
            )
        return model_id

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        """Run a task and repair only schema or deterministic validation failures."""

        model_id = self._resolve_model(task)
        with trace_claude_task(task, model_id=model_id) as task_trace:
            result = await self._run(
                task,
                model_id=model_id,
                task_trace=task_trace,
            )
            if task_trace is not None:
                task_trace.complete(result)
            return result

    async def _run(
        self,
        task: AgentTask[OutputT],
        *,
        model_id: str,
        task_trace: ClaudeTaskTrace | None,
    ) -> AgentRunResult[OutputT]:
        policy_compiler = PolicyCompiler(self.config)
        policy_compiler.validate(task)
        timeout_seconds = task.limits.timeout_seconds or self.config.default_timeout_seconds
        max_repairs = min(task.limits.output_retries, self.config.max_repair_attempts)
        max_attempts = 1 + max_repairs

        artifact_store = ArtifactStore(self.config, runtime_id=self.runtime_id)
        process_runner = ProcessRunner(self.config)
        protocol_decoder = ProtocolDecoder(self.config)
        artifact_run_dir = artifact_store.create_run(task)
        attempt_artifacts: list[AttemptArtifacts] = []
        request_counter = RequestCounter(task.limits.request_limit)
        last_errors: tuple[str, ...] = ()
        last_candidate = ""
        completed_attempts: list[tuple[ParsedOutput, ProcessResult]] = []

        with tempfile.TemporaryDirectory(prefix="vaminer-claude-") as temporary:
            temporary_root = Path(temporary)
            environment = policy_compiler.build_environment(task)
            executable = policy_compiler.resolve_executable(environment)
            native_delegation_tool = policy_compiler.resolve_delegation_tool(task)
            policy = policy_compiler.compile(
                task,
                native_delegation_tool=native_delegation_tool,
            )
            policy_files = policy_compiler.materialize(
                temporary_root,
                task=task,
                policy=policy,
                model_id=model_id,
                trace_compaction=task_trace is not None,
            )
            schema_json = json.dumps(
                descriptive_json_schema(task.output_type),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            argv = policy_compiler.build_argv(
                executable=executable,
                task=task,
                files=policy_files,
                schema_json=schema_json,
                policy=policy,
                model_id=model_id,
            )
            invocation_path = artifact_store.persist_invocation(
                artifact_run_dir,
                task=task,
                policy=policy,
                system_prompt_file=policy_files.system_prompt,
                model_id=self.model_id_for(task),
            )

            prompt = policy_compiler.initial_prompt(task)
            for attempt in range(1, max_attempts + 1):
                remaining_turns = (
                    task.limits.request_limit - request_counter.count
                    if task.limits.request_limit is not None
                    else None
                )
                if remaining_turns is not None and remaining_turns < 1:
                    raise ClaudeCodeRequestLimitError(
                        task.limits.request_limit,
                        observed=request_counter.count,
                    )
                attempt_argv = _with_max_turns(argv, remaining_turns)
                requests_before_attempt = request_counter.count
                persisted = artifact_store.prepare_attempt(
                    artifact_run_dir,
                    attempt=attempt,
                )
                attempt_artifacts.append(persisted)
                if policy_files.compact_events is not None:
                    write_private(policy_files.compact_events, b"")
                if task_trace is not None:
                    task_trace.start_attempt(prompt, attempt=attempt)
                event_handler = (
                    (
                        lambda event, attempt=attempt: task_trace.observe_event(
                            event,
                            attempt=attempt,
                        )
                    )
                    if task_trace is not None
                    else None
                )
                stream_monitor = StreamMonitor(
                    task_id=task.task_id,
                    attempt=attempt,
                    counter=request_counter,
                    event_handler=event_handler,
                    compact_events_path=policy_files.compact_events,
                )
                logger.info(
                    "Claude Code attempt started: task=%s attempt=%s/%s",
                    task.task_id,
                    attempt,
                    max_attempts,
                )
                process = await process_runner.run(
                    attempt_argv,
                    prompt=prompt,
                    cwd=task.workspace.cwd,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    stdout_path=persisted.stdout_path,
                    stderr_path=persisted.stderr_path,
                    stdout_line_handler=stream_monitor.observe_line,
                )
                stream_monitor.finish()
                if (
                    persisted.stderr_path is not None
                    and persisted.stderr_path.is_file()
                    and persisted.stderr_path.stat().st_size
                ):
                    logger.warning(
                        "Claude Code stderr captured: task=%s attempt=%s path=%s",
                        task.task_id,
                        attempt,
                        persisted.stderr_path,
                    )

                parsed = protocol_decoder.decode(
                    process,
                    configured_model=model_id,
                    turn_limit=remaining_turns,
                )
                request_counter.reconcile_attempt(
                    previous_count=requests_before_attempt,
                    reported_turns=parsed.usage.turns if parsed.usage is not None else None,
                )
                completed_attempts.append((parsed, process))
                output, validation_errors, candidate = validate_output(task, parsed)
                if output is not None:
                    observed_models = validate_model_identity(completed_attempts)
                    last_artifacts = attempt_artifacts[-1]
                    usage = aggregate_attempt_usage(
                        completed_attempts,
                        requests=request_counter.count,
                    )
                    recorded_subagents = [
                        event
                        for attempt_output, _ in completed_attempts
                        for event in subagent_events(attempt_output.events)
                    ]
                    return AgentRunResult(
                        output=output,
                        runtime_id=self.runtime_id,
                        model_id=self.model_id_for(task),
                        usage=usage,
                        artifacts=RuntimeArtifacts(
                            final_text=parsed.final_text,
                            prompt_path=last_artifacts.prompt_path,
                            stdout_path=last_artifacts.stdout_path,
                            stderr_path=(
                                last_artifacts.stderr_path
                                if last_artifacts.stderr_path is not None
                                and last_artifacts.stderr_path.is_file()
                                else None
                            ),
                            events_path=last_artifacts.events_path,
                            invocation_path=invocation_path,
                        ),
                        attempts=attempt,
                        metadata={
                            "session_id": parsed.session_id,
                            "exit_code": process.returncode,
                            "duration_ms": usage.duration_ms,
                            "output_format": self.config.output_format,
                            "request_count": request_counter.count,
                            "claude_num_turns": usage.turns,
                            "claude_native_num_turns": sum_optional(
                                [
                                    attempt_output.usage.turns
                                    for attempt_output, _ in completed_attempts
                                    if attempt_output.usage is not None
                                ]
                            ),
                            "model_usage": dict(usage.model_usage),
                            "observed_model_ids": observed_models,
                            "subagent_events": recorded_subagents,
                            "structured_candidate_count": len(parsed.structured_candidates),
                            "attempt_artifacts": tuple(
                                {
                                    "prompt": str(item.prompt_path) if item.prompt_path else None,
                                    "stdout": str(item.stdout_path) if item.stdout_path else None,
                                    "stderr": (
                                        str(item.stderr_path)
                                        if item.stderr_path is not None
                                        and item.stderr_path.is_file()
                                        else None
                                    ),
                                    "events": str(item.events_path) if item.events_path else None,
                                    "validation_errors": (
                                        str(item.validation_errors_path)
                                        if item.validation_errors_path is not None
                                        and item.validation_errors_path.is_file()
                                        else None
                                    ),
                                }
                                for item in attempt_artifacts
                            ),
                        },
                    )

                last_errors = validation_errors
                last_candidate = candidate
                validation_errors_path = artifact_store.persist_validation_errors(
                    persisted,
                    last_errors,
                )
                logger.warning(
                    "Claude Code output validation failed: task=%s attempt=%s/%s "
                    "errors=%s validation_errors=%s",
                    task.task_id,
                    attempt,
                    max_attempts,
                    "; ".join(last_errors),
                    validation_errors_path,
                )
                if attempt >= max_attempts:
                    break
                prompt = self._repair_prompt(
                    task,
                    candidate=last_candidate,
                    errors=last_errors,
                    repair_number=attempt,
                )

        raise ClaudeCodeValidationError(last_errors or ("no structured output was returned",), attempts=max_attempts)

    def _repair_prompt(
        self,
        task: AgentTask[Any],
        *,
        candidate: str,
        errors: Sequence[str],
        repair_number: int,
    ) -> str:
        bounded_candidate = candidate[: self.config.max_repair_payload_chars]
        error_list = "\n".join(f"- {error}" for error in errors)
        prompt = f"""The previous structured response failed validation.

Repair attempt: {repair_number}

Validation errors:
{error_list}

Previous candidate:
{bounded_candidate}

Return one complete replacement object satisfying the supplied JSON Schema and every validation error.
Preserve evidence-backed conclusions unless a listed error requires changing them.

Original task:
{task.prompt}
"""
        return prompt


def _with_max_turns(argv: Sequence[str], remaining: int | None) -> list[str]:
    """Return one attempt argv with its decreasing native turn budget."""

    result = list(argv)
    if "--max-turns" in result:
        index = result.index("--max-turns")
        del result[index : index + 2]
    if remaining is not None:
        result.extend(("--max-turns", str(remaining)))
    return result
