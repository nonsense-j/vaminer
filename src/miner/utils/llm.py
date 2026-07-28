"""Shared Pydantic AI model construction."""

from __future__ import annotations

from functools import cache

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider

from ..configs import DEEPSEEK_API_KEY, LLM_MODEL, LLM_PROVIDER, OPENAI_API_KEY, OPENAI_BASE_URL


@cache
def get_llm() -> OpenAIChatModel:
    """Return the single shared Chat Completions model for this process."""
    if not LLM_PROVIDER:
        raise RuntimeError("LLM_PROVIDER is required")
    if not LLM_MODEL:
        raise RuntimeError("LLM_MODEL is required")

    if LLM_PROVIDER == "deepseek":
        return OpenAIChatModel(LLM_MODEL, provider=DeepSeekProvider(api_key=DEEPSEEK_API_KEY))
    if LLM_PROVIDER == "openai":
        return OpenAIChatModel(LLM_MODEL, provider=OpenAIProvider(api_key=OPENAI_API_KEY))
    if LLM_PROVIDER == "openai-compatible":
        if not OPENAI_BASE_URL:
            raise RuntimeError("OPENAI_BASE_URL is required for openai-compatible")
        return OpenAIChatModel(
            LLM_MODEL,
            provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY),
            profile=OpenAIModelProfile(
                openai_supports_strict_tool_definition=False,
                openai_chat_supports_multiple_system_messages=False,
            ),
        )
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER}")
