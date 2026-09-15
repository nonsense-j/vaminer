"""Slim Claude Code CLI Adapter for Miner Agent tasks."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from ...agent.contracts import (
    AgentPhase,
    AgentRunResult,
    AgentSession,
    AgentTask,
    OutputT,
    RootCauseAuthority,
    RuleGenerationAuthority,
    RuntimeIdentity,
    RuntimeUsage,
)
from ...mining.validation.analysis import finalize_root_cause_cases
from ...models.analysis import RootCauseAnalysis
from ...models.vas import RuleGenerationDraft
from ...mining.synthesis import (
    AnchorSynthesisAcceptanceError,
    AnchorSynthesisReceipt,
    finalize_rule_generation,
)
from ...utils.log import RuntimeLog, logger
from ...utils.telemetry import trace_agent_observation, trace_value
from .config import ClaudeCodeConfig
from .errors import (
    ClaudeCodeChildSynthesisError,
    ClaudeCodeError,
    ClaudeCodeOutputLimitError,
    ClaudeCodeProcessError,
    ClaudeCodeProtocolError,
    ClaudeCodeRequestLimitError,
    ClaudeCodeTimeoutError,
    ClaudeCodeToolExecutionError,
)
from .mcp import SERVER_NAME
from .policy import InvocationFiles, PolicyCompiler, cleanup_session_transcript, model_output_type
from .process import ProcessResult, ProcessRunner, clip, redact
from .protocol import ClaudeStreamDecoder
from .tracing import emit_session_trace


def _sum(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _merge_usage(current: RuntimeUsage | None, added: RuntimeUsage) -> RuntimeUsage:
    if current is None:
        return added
    return RuntimeUsage(
        requests=_sum(current.requests, added.requests),
        turns=_sum(current.turns, added.turns),
        input_tokens=_sum(current.input_tokens, added.input_tokens),
        output_tokens=_sum(current.output_tokens, added.output_tokens),
        cache_creation_input_tokens=_sum(current.cache_creation_input_tokens, added.cache_creation_input_tokens),
        cache_read_input_tokens=_sum(current.cache_read_input_tokens, added.cache_read_input_tokens),
        duration_ms=_sum(current.duration_ms, added.duration_ms),
    )


async def _relay_synthesis_log(
    path: Path,
    finished: asyncio.Event,
    *,
    runtime_log: RuntimeLog,
) -> None:
    """Stream MCP-hosted Synthesizer panels through the parent logger."""

    pending = bytearray()
    try:
        with path.open("rb") as source:
            while True:
                pending.extend(source.read())
                while (newline := pending.find(b"\n")) >= 0:
                    raw_line = bytes(pending[:newline])
                    del pending[: newline + 1]
                    runtime_log.relay(raw_line.decode("utf-8", errors="replace").rstrip("\r"))
                if finished.is_set():
                    if pending:
                        runtime_log.relay(
                            bytes(pending).decode("utf-8", errors="replace").rstrip("\r")
                        )
                    return
                try:
                    await asyncio.wait_for(finished.wait(), timeout=0.05)
                except TimeoutError:
                    pass
    except OSError:
        logger.debug("Failed to relay Synthesizer diagnostics from %s", path, exc_info=True)


def _raise_failure_receipt(
    path: Path | None,
    *,
    label: str,
    error_type: type[ClaudeCodeError],
) -> None:
    if path is None or not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeCodeProtocolError(f"invalid {label} failure receipt: {exc}") from exc
    if not isinstance(payload, dict):
        raise ClaudeCodeProtocolError(f"{label} failure receipt must be an object")
    receipt_type = payload.get("type")
    message = payload.get("message")
    if not isinstance(receipt_type, str) or not isinstance(message, str):
        raise ClaudeCodeProtocolError(
            f"{label} failure receipt must contain string type and message"
        )
    raise error_type(f"{clip(receipt_type, 200)}: {redact(clip(message, 2_000))}")


class _ClaudeAgentSession:
    """One Claude CLI conversation that resumes for every follow-up request."""

    def __init__(self, runtime: "ClaudeCodeRuntime", task: AgentTask[Any]) -> None:
        self._runtime = runtime
        self._task = task
        self._compiler = runtime._compiler
        self._environment = self._compiler.environment()
        self._executable = self._compiler.resolve_executable(self._environment)
        self._policy = self._compiler.compile(task)
        self._timeout = task.limits.timeout_seconds or runtime.config.default_timeout_seconds
        self._observed_turns = 0
        self._usage: RuntimeUsage | None = None
        self._started = False
        self._closed = False
        self._temporary_roots: list[tempfile.TemporaryDirectory[str]] = []
        self._files = self._materialize()

    def _materialize(self) -> InvocationFiles:
        holder = tempfile.TemporaryDirectory(prefix="vaminer-claude-session-")
        self._temporary_roots.append(holder)
        return self._compiler.materialize(
            Path(holder.name),
            task=self._task,
            policy=self._policy,
            executable=self._executable,
            model_id=self._runtime.identity.model_id,
        )

    async def _relay(self, finished: asyncio.Event, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        finished.set()
        await task

    async def _send_once(self, prompt: str) -> tuple[AgentRunResult[Any] | None, list[str]]:
        remaining = self._task.limits.request_limit
        if remaining is not None:
            remaining -= self._observed_turns
            if remaining < 1:
                raise ClaudeCodeRequestLimitError(
                    self._task.limits.request_limit, observed=self._observed_turns + 1,
                    cli_name=self._runtime.config.display_name,
                )
        attempt_task = self._task
        if remaining != self._task.limits.request_limit:
            attempt_task = replace(
                self._task,
                limit_override=replace(self._task.limits, request_limit=remaining),
            )
        argv = self._compiler.argv(
            executable=self._executable,
            task=attempt_task,
            policy=self._policy,
            files=self._files,
            model_id=self._runtime.identity.model_id,
            resume=self._started,
        )
        decoder = ClaudeStreamDecoder(
            output_type=model_output_type(self._task),
            agent_name=self._task.agent_name,
            cli_name=self._runtime.config.display_name,
            runtime_log=self._runtime._runtime_log,
            expected_mcp_server=SERVER_NAME,
            expected_mcp_tools=self._policy.qualified_mcp_tools,
            session_mode="resumed" if self._started else "fresh",
            request_limit=remaining,
        )
        relay_finished = asyncio.Event()
        relay_task = None
        if self._files.synthesis_log is not None:
            self._files.synthesis_log.write_text("", encoding="utf-8")
            relay_task = asyncio.create_task(
                _relay_synthesis_log(
                    self._files.synthesis_log,
                    relay_finished,
                    runtime_log=self._runtime._runtime_log,
                )
            )
        try:
            process = await self._runtime._run_process(
                argv,
                files=self._files,
                cwd=self._task.workspace_root,
                environment=self._environment,
                prompt=prompt,
                timeout_seconds=self._timeout,
                stdout_line_handler=decoder.feed_line,
            )
            self._started = True
        finally:
            await self._relay(relay_finished, relay_task)
            try:
                await emit_session_trace(
                    self._files.session_id,
                    environment=self._environment,
                    state_dir=self._files.trace_state,
                    executable=self._executable,
                    display_name=self._runtime.config.display_name,
                )
            except Exception:  # noqa: BLE001 - tracing must never affect Agent execution.
                logger.debug(
                    "Failed to run bundled %s Langfuse hook",
                    self._runtime.config.display_name,
                    exc_info=True,
                )

        if decoder.line_number == 0:
            for raw in process.stdout.splitlines():
                decoder.feed_line(raw)
        self._runtime._raise_synthesis_failure(self._files.synthesis_failure)
        self._runtime._raise_tool_failure(self._files.tool_failure)
        decoded = decoder.finish(process)
        self._usage = _merge_usage(self._usage, decoded.usage)
        self._observed_turns += max(1, decoded.usage.turns or 0)
        if decoded.validation_errors:
            return None, list(decoded.validation_errors)
        assert decoded.output is not None
        try:
            final = self._runtime._final_output(self._task, decoded.output, receipt_path=self._files.receipt)
        except AnchorSynthesisAcceptanceError as exc:
            return None, [str(exc)]
        errors = list(self._task.validate_output(cast(Any, final)))
        if errors:
            return None, errors
        return (
            AgentRunResult(
                output=cast(Any, final),
                identity=self._runtime.identity,
                usage=self._usage,
                attempts=1,
            ),
            [],
        )

    async def _send_with_repairs(self, prompt: str) -> AgentRunResult[Any]:
        errors: list[str] = []
        attempts = 0
        while True:
            attempts += 1
            current_prompt = prompt
            if errors:
                current_prompt = (
                    "The previous complete output failed structured or deterministic acceptance. "
                    "Address the feedback using the available tools, then return a corrected output. Preserve the existing "
                    "evidence and tool results; do not repeat research unless the feedback challenges "
                    "that evidence. Return one corrected complete typed output:\n- "
                    + clip(redact("\n- ".join(errors)), self._runtime.config.max_repair_payload_chars)
                )
            result, errors = await self._send_once(current_prompt)
            if result is not None:
                return replace(result, attempts=attempts)

    def _recover_after_process_failure(self, error: BaseException) -> str:
        cleanup_session_transcript(
            self._files.session_id,
            self._environment,
            executable=self._executable,
        )
        self._files = self._materialize()
        self._started = False
        return (
            "Continue the ast-grep synthesis task after the previous process crashed and its session "
            "could not be resumed.\n\nPrevious process error:\n- "
            + redact(clip(str(error), self._runtime.config.max_repair_payload_chars))
            + "\n\nReturn one complete typed output for the most recent request."
        )

    async def send(self, prompt: str) -> AgentRunResult[Any]:
        if self._closed:
            raise ClaudeCodeError("Claude session is already closed")
        max_process_retries = (
            self._runtime.config.max_synthesis_process_retries
            if self._task.phase is AgentPhase.AST_GREP_SYNTHESIS else 0
        )
        current_prompt = prompt
        for process_attempt in range(max_process_retries + 1):
            try:
                result = await self._send_with_repairs(current_prompt)
                return replace(result, attempts=result.attempts + process_attempt)
            except (ClaudeCodeProcessError, ClaudeCodeTimeoutError, ClaudeCodeOutputLimitError) as exc:
                if process_attempt >= max_process_retries:
                    raise
                logger.warning(
                    "%s process failed for %s; retrying with a fresh recovery session: %s",
                    self._runtime.config.display_name,
                    self._task.task_id,
                    redact(clip(str(exc), 2_000)),
                )
                current_prompt = (
                    self._recover_after_process_failure(exc)
                    + "\n\nMost recent request:\n"
                    + current_prompt
                )
        raise AssertionError("unreachable")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup_session_transcript(
            self._files.session_id,
            self._environment,
            executable=self._executable,
        )
        for holder in self._temporary_roots:
            holder.cleanup()


class ClaudeCodeRuntime:
    runtime_id = "claude-cli"

    def __init__(
        self,
        config: ClaudeCodeConfig | None = None,
        *,
        runtime_log: RuntimeLog | None = None,
    ) -> None:
        self.config = config or ClaudeCodeConfig()
        self._runtime_log = runtime_log or RuntimeLog()
        self._compiler = PolicyCompiler(self.config)
        self._runner = ProcessRunner(
            max_stdout_bytes=self.config.max_stdout_bytes,
            max_stderr_bytes=self.config.max_stderr_bytes,
            terminate_grace_seconds=self.config.terminate_grace_seconds,
            cli_name=self.config.display_name,
        )

    @property
    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(
            runtime_id=self.runtime_id,
            model_id=self.config.model or "claude-session-default",
        )

    @staticmethod
    def _raise_synthesis_failure(path: Path | None) -> None:
        _raise_failure_receipt(
            path,
            label="child synthesis",
            error_type=ClaudeCodeChildSynthesisError,
        )

    @staticmethod
    def _raise_tool_failure(path: Path | None) -> None:
        _raise_failure_receipt(
            path,
            label="tool",
            error_type=ClaudeCodeToolExecutionError,
        )

    async def _run_process(
        self,
        argv: list[str],
        *,
        files: InvocationFiles,
        cwd: Path,
        environment: dict[str, str],
        prompt: str,
        timeout_seconds: float,
        stdout_line_handler: Callable[[str], None],
    ) -> ProcessResult:
        """Stop the CLI when an MCP tool or child reports a fatal failure."""
        invocation = asyncio.create_task(self._runner.run(
            argv, cwd=cwd, environment=environment, prompt=prompt,
            timeout_seconds=timeout_seconds, stdout_line_handler=stdout_line_handler,
        ))
        try:
            while not invocation.done():
                self._raise_synthesis_failure(files.synthesis_failure)
                self._raise_tool_failure(files.tool_failure)
                await asyncio.wait({invocation}, timeout=0.1)
            self._raise_synthesis_failure(files.synthesis_failure)
            self._raise_tool_failure(files.tool_failure)
            return await invocation
        finally:
            invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)

    @staticmethod
    def _final_output(
        task: AgentTask[Any],
        model_output: BaseModel,
        *,
        receipt_path: Path | None,
    ) -> BaseModel:
        if task.phase is AgentPhase.RULE_GENERATION:
            authority = cast(RuleGenerationAuthority, task.authority)
            if receipt_path is None or not receipt_path.is_file():
                raise AnchorSynthesisAcceptanceError(
                    "Rule Generation completed without an accepted Anchor Plan"
                )
            try:
                receipt = AnchorSynthesisReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as exc:
                raise ClaudeCodeProtocolError(f"invalid synthesis receipt: {exc}") from exc
            return finalize_rule_generation(authority, cast(RuleGenerationDraft, model_output), receipt)
        if task.phase is AgentPhase.ROOT_CAUSE:
            authority = cast(RootCauseAuthority, task.authority)
            finalize_root_cause_cases(cast(RootCauseAnalysis, model_output), cases_dir=authority.cases_dir)
        return model_output

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        self._runtime_log.started(task.agent_name, {"task_id": task.task_id, "prompt": task.prompt})
        try:
            with trace_agent_observation(
                name=f"{task.agent_name} Agent",
                input={"task_id": task.task_id, "prompt": task.prompt, "instructions": task.instructions.shared},
                metadata={"phase": task.phase.value, "runtime": self.runtime_id, "model": self.identity.model_id},
                truncate=False,
            ) as observation:
                session = self.open_session(task)
                try:
                    result = await session.send(task.prompt)
                finally:
                    await session.close()
                if observation is not None:
                    try:
                        observation.update(
                            output=trace_value(result.output),
                            metadata={"usage": trace_value(result.usage)} if result.usage else None,
                        )
                    except Exception:  # noqa: BLE001, S110 - tracing is observe-only.
                        pass
        except BaseException as error:
            self._runtime_log.failed(task.agent_name, error)
            raise
        self._runtime_log.finished(
            task.agent_name,
            {"output": result.output, "usage": result.usage},
        )
        return result

    def open_session(self, task: AgentTask[OutputT]) -> AgentSession[OutputT]:
        """Open a Claude conversation whose follow-ups use the same session id."""

        self._compiler.validate(task)
        return cast(AgentSession[OutputT], _ClaudeAgentSession(self, task))


__all__ = ["ClaudeCodeRuntime"]
