"""Rich command-line observability for the Pydantic AI adapter."""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import Hooks, ValidatedToolArgs
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import ToolDefinition
from rich.console import Console

from ...utils.log import RuntimeLog
from .context import MinerContext


def _agent_name(ctx: RunContext[Any]) -> str:
    if ctx.agent is None:
        return "Unknown Agent"
    return ctx.agent.name or "Unnamed Agent"


def make_cli_hooks(
    *,
    runtime_log: RuntimeLog | None = None,
    console: Console | None = None,
    emit_console: bool = True,
    width: int = 120,
    body_limit: int = 1_000,
    lines_limit: int = 15,
) -> Hooks[MinerContext]:
    """Build eager, observe-only hooks for console and active run-file diagnostics."""
    runtime_log = runtime_log or RuntimeLog(
        console=console,
        emit_console=emit_console,
        width=width,
        body_limit=body_limit,
        lines_limit=lines_limit,
    )
    hooks = Hooks[MinerContext](defer_loading=False)

    @hooks.on.before_run
    async def before_run(ctx: RunContext[MinerContext]) -> None:
        runtime_log.started(_agent_name(ctx), {"run_id": ctx.run_id, "prompt": ctx.prompt})

    @hooks.on.after_model_request
    async def after_model_request(
        ctx: RunContext[MinerContext],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del request_context
        agent_name = _agent_name(ctx)
        for part in response.parts:
            if isinstance(part, ThinkingPart) and part.content:
                runtime_log.event(agent_name, "thinking", part.content)
            elif isinstance(part, TextPart):
                runtime_log.event(agent_name, "message", part.content or "(empty)")
        return response

    @hooks.on.model_request_error
    async def model_request_error(
        ctx: RunContext[MinerContext],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        del request_context
        agent_name = _agent_name(ctx)
        runtime_log.event(agent_name, "error", error, detail="model request")
        raise error

    @hooks.on.before_tool_execute
    async def before_tool_execute(
        ctx: RunContext[MinerContext],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        del tool_def
        agent_name = _agent_name(ctx)
        tool_id = f"{call.tool_name}:{call.tool_call_id}"
        runtime_log.event(agent_name, "tool.call", args, detail=tool_id)
        return args

    @hooks.on.after_tool_execute
    async def after_tool_execute(
        ctx: RunContext[MinerContext],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        result: Any,
    ) -> Any:
        del tool_def, args
        agent_name = _agent_name(ctx)
        tool_id = f"{call.tool_name}:{call.tool_call_id}"
        runtime_log.event(agent_name, "tool.result", result, detail=tool_id)
        return result

    @hooks.on.tool_execute_error
    async def tool_execute_error(
        ctx: RunContext[MinerContext],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        error: Exception,
    ) -> Any:
        del tool_def, args
        agent_name = _agent_name(ctx)
        tool_id = f"{call.tool_name}:{call.tool_call_id}"
        runtime_log.event(agent_name, "error", error, detail=tool_id)
        raise error

    @hooks.on.after_run
    async def after_run(
        ctx: RunContext[MinerContext],
        *,
        result: AgentRunResult[Any],
    ) -> AgentRunResult[Any]:
        agent_name = _agent_name(ctx)
        runtime_log.finished(
            agent_name,
            {
                "run_id": result.run_id,
                "output": result.output,
                "usage": result.usage,
            },
        )
        return result

    @hooks.on.run_error
    async def run_error(
        ctx: RunContext[MinerContext],
        *,
        error: BaseException,
    ) -> AgentRunResult[Any]:
        agent_name = _agent_name(ctx)
        runtime_log.failed(agent_name, error)
        raise error

    return hooks
