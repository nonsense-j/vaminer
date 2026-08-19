"""Pydantic AI configuration and live Agent preflight checks."""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ToolOutput

from ..runtimes.pydantic import config as runtime_config
from ..runtimes.pydantic.llm import get_llm
from ..runtimes.pydantic.telemetry import instrument_tracing
from .models import CheckResult
from .progress import ProgressCallback, start_heartbeat, stop_heartbeat


class _PydanticProbeOutput(BaseModel):
    greeting: Literal["hello from vaminer"]
    tool_sentinel: Literal["vaminer-preflight-ok"]


def check_pydantic_config() -> CheckResult:
    provider = runtime_config.LLM_PROVIDER
    model = runtime_config.LLM_MODEL
    if not provider:
        return CheckResult.failed("pydantic.config", "LLM_PROVIDER is required")
    if not model:
        return CheckResult.failed("pydantic.config", "LLM_MODEL is required")
    if provider == "deepseek" and not runtime_config.DEEPSEEK_API_KEY:
        return CheckResult.failed("pydantic.config", "DEEPSEEK_API_KEY is required for the DeepSeek provider")
    if provider == "openai" and not runtime_config.OPENAI_API_KEY:
        return CheckResult.failed("pydantic.config", "OPENAI_API_KEY is required for the OpenAI provider")
    if provider == "openai-compatible" and not runtime_config.OPENAI_BASE_URL:
        return CheckResult.failed("pydantic.config", "OPENAI_BASE_URL is required for an OpenAI-compatible provider")
    try:
        resolved = get_llm()
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "pydantic.config",
            "Pydantic AI model construction failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return CheckResult.passed(
        "pydantic.config",
        f"Pydantic AI model is configured for {provider}/{model}",
        detail=type(resolved).__name__,
    )


async def check_pydantic_live(
    *,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
) -> CheckResult:
    started = time.monotonic()
    tool_calls = 0

    def preflight_echo() -> str:
        """Return the fixed VAMiner preflight sentinel."""

        nonlocal tool_calls
        tool_calls += 1
        return "vaminer-preflight-ok"

    try:
        instrument_tracing()
        agent = Agent(
            model=get_llm(),
            name="VAMiner Preflight Agent",
            instructions="Call preflight_echo exactly once, then return the required structured output.",
            tools=[preflight_echo],
            output_type=ToolOutput(_PydanticProbeOutput, name="preflight_result", strict=False),
            retries=1,
        )
        heartbeat = start_heartbeat(
            progress,
            message="Pydantic AI Agent request is running",
            timeout_seconds=timeout_seconds,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await agent.run(
                    "Say hello through the structured output and include the exact sentinel returned by preflight_echo."
                )
        finally:
            await stop_heartbeat(heartbeat)
        if result.output != _PydanticProbeOutput(
            greeting="hello from vaminer",
            tool_sentinel="vaminer-preflight-ok",
        ):
            return CheckResult.failed("pydantic.agent-live", "Pydantic AI returned an unexpected probe output")
        if tool_calls != 1:
            return CheckResult.failed(
                "pydantic.agent-live",
                "Pydantic AI did not call the preflight tool exactly once",
                detail=f"tool_calls={tool_calls}",
            )
    except Exception as exc:  # noqa: BLE001
        return CheckResult.failed(
            "pydantic.agent-live",
            "Pydantic AI live Agent probe failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    return CheckResult.passed(
        "pydantic.agent-live",
        "Pydantic AI authenticated, called a typed tool, and returned structured output",
        duration_ms=round((time.monotonic() - started) * 1000),
    )


__all__ = ["check_pydantic_config", "check_pydantic_live"]
