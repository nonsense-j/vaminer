"""Slim Claude Code CLI Adapter for Miner Agent tasks."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from ...anchors.scanner import AnchorScanError
from ...agent.contracts import (
    AgentPhase,
    AgentRunResult,
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
    ClaudeCodeProtocolError,
    ClaudeCodeToolExecutionError,
    ClaudeCodeValidationError,
)
from .mcp import SERVER_NAME
from .policy import PolicyCompiler, cleanup_session_transcript, model_output_type
from .process import ProcessRunner, clip, redact
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
                        runtime_log.relay(bytes(pending).decode("utf-8", errors="replace"))
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

    async def _run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        self._compiler.validate(task)
        environment = self._compiler.environment()
        executable = self._compiler.resolve_executable(environment)
        policy = self._compiler.compile(task)
        timeout = task.limits.timeout_seconds or self.config.default_timeout_seconds
        max_repairs = min(task.limits.output_retries, self.config.max_repair_attempts)
        errors: list[str] = []
        usage: RuntimeUsage | None = None
        observed_turns = 0
        completed_attempts = 0

        with tempfile.TemporaryDirectory(prefix="vaminer-claude-") as raw_temp:
            temporary_root = Path(raw_temp)
            files = self._compiler.materialize(
                temporary_root,
                task=task,
                policy=policy,
                executable=executable,
                model_id=self.identity.model_id,
            )
            try:
                for attempt in range(1, max_repairs + 2):
                    remaining = task.limits.request_limit
                    if remaining is not None:
                        remaining -= observed_turns
                        if remaining < 1:
                            break
                    completed_attempts = attempt
                    attempt_task = task
                    if remaining != task.limits.request_limit:
                        attempt_task = replace(
                            task,
                            limit_override=replace(task.limits, request_limit=remaining),
                        )
                    if errors:
                        feedback = "\n- ".join(errors)
                        prompt = (
                            "The previous complete output failed structured or deterministic acceptance. "
                            "Correct only the fields implicated by the feedback below. Preserve the existing "
                            "evidence and tool results; do not repeat research unless the feedback challenges "
                            "that evidence. Return one corrected complete typed output:\n- "
                            + clip(redact(feedback), self.config.max_repair_payload_chars)
                        )
                    else:
                        prompt = task.prompt

                    argv = self._compiler.argv(
                        executable=executable,
                        task=attempt_task,
                        policy=policy,
                        files=files,
                        model_id=self.identity.model_id,
                        resume=attempt > 1,
                    )
                    decoder = ClaudeStreamDecoder(
                        output_type=model_output_type(task),
                        agent_name=task.agent_name,
                        cli_name=self.config.display_name,
                        runtime_log=self._runtime_log,
                        expected_mcp_server=SERVER_NAME,
                        expected_mcp_tools=policy.qualified_mcp_tools,
                        session_mode="resumed" if attempt > 1 else "fresh",
                    )
                    relay_finished = asyncio.Event()
                    relay_task = None
                    if files.synthesis_log is not None:
                        files.synthesis_log.write_text("", encoding="utf-8")
                        relay_task = asyncio.create_task(
                            _relay_synthesis_log(
                                files.synthesis_log,
                                relay_finished,
                                runtime_log=self._runtime_log,
                            )
                        )
                    try:
                        process = await self._runner.run(
                            argv,
                            cwd=task.workspace_root,
                            environment=environment,
                            prompt=prompt,
                            timeout_seconds=timeout,
                            stdout_line_handler=decoder.feed_line,
                        )
                    finally:
                        if relay_task is not None:
                            relay_finished.set()
                            await relay_task
                        try:
                            await emit_session_trace(
                                files.session_id,
                                environment=environment,
                                state_dir=files.trace_state,
                                executable=executable,
                                display_name=self.config.display_name,
                            )
                        except Exception:  # noqa: BLE001 - tracing must never affect Agent execution.
                            logger.debug(
                                "Failed to run bundled %s Langfuse hook",
                                self.config.display_name,
                                exc_info=True,
                            )
                    decoded = None
                    final = None
                    try:
                        self._raise_synthesis_failure(files.synthesis_failure)
                        self._raise_tool_failure(files.tool_failure)
                        if decoder.line_number == 0:
                            for raw in process.stdout.splitlines():
                                decoder.feed_line(raw)
                        decoded = decoder.finish(process)
                        usage = _merge_usage(usage, decoded.usage)
                        observed_turns += decoded.usage.turns or 0
                        if decoded.validation_errors:
                            errors = list(decoded.validation_errors)
                            final = None
                        else:
                            assert decoded.output is not None
                            final = self._final_output(task, decoded.output, receipt_path=files.receipt)
                            errors = list(task.validate_output(cast(Any, final)))
                    except ClaudeCodeProtocolError:
                        raise
                    except AnchorScanError:
                        raise
                    except AnchorSynthesisAcceptanceError as exc:
                        errors = [str(exc)]
                    if not errors:
                        assert final is not None
                        return AgentRunResult(
                            output=cast(OutputT, final),
                            identity=self.identity,
                            usage=usage,
                            attempts=attempt,
                        )
                    logger.warning(
                        "%s output rejected for %s (attempt %s): %s",
                        self.config.display_name,
                        task.task_id,
                        attempt,
                        "; ".join(errors),
                    )
            finally:
                cleanup_session_transcript(
                    files.session_id,
                    environment,
                    executable=executable,
                )

        raise ClaudeCodeValidationError(
            errors or ["model request limit exhausted during output repair"],
            attempts=max(1, completed_attempts),
            cli_name=self.config.display_name,
        )

    async def run(self, task: AgentTask[OutputT]) -> AgentRunResult[OutputT]:
        self._runtime_log.started(task.agent_name, {"task_id": task.task_id, "prompt": task.prompt})
        try:
            with trace_agent_observation(
                name=f"{task.agent_name} Agent",
                input={"task_id": task.task_id, "prompt": task.prompt, "instructions": task.instructions.shared},
                metadata={"phase": task.phase.value, "runtime": self.runtime_id, "model": self.identity.model_id},
                truncate=False,
            ) as observation:
                result = await self._run(task)
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


__all__ = ["ClaudeCodeRuntime"]
