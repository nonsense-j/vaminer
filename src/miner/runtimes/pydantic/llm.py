"""Pydantic AI LLM construction."""

from __future__ import annotations

from functools import cache

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile, OpenAIJsonSchemaTransformer
from pydantic_ai.providers.openai import OpenAIProvider

from .config import (
    DEEPSEEK_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
)


@cache
def get_llm() -> OpenAIChatModel:
    """Return the single shared Chat Completions model for this process."""
    if not LLM_PROVIDER:
        raise RuntimeError("LLM_PROVIDER is required")
    if not LLM_MODEL:
        raise RuntimeError("LLM_MODEL is required")

    if LLM_PROVIDER == "openai":
        return OpenAIChatModel(LLM_MODEL, provider=OpenAIProvider(api_key=OPENAI_API_KEY))
    if LLM_PROVIDER == "deepseek":
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY is required for deepseek")
        if (not LLM_MODEL.startswith("deepseek-v4")) and LLM_MODEL != "deepseek-flash":
            raise RuntimeError(f"Use the latest deepseek model: deepseek-flash or deepseek-v4-pro")
        return OpenAIChatModel(
            LLM_MODEL,
            provider=OpenAIProvider(base_url="https://api.deepseek.com", api_key=DEEPSEEK_API_KEY),
            profile=OpenAIModelProfile(
                json_schema_transformer=OpenAIJsonSchemaTransformer,
                supports_json_object_output=True,
                openai_chat_thinking_field="reasoning_content",
                openai_chat_send_back_thinking_parts="field",
                openai_supports_tool_choice_required=False,
            ),
        )
    if LLM_PROVIDER == "openai-compatible":
        if not OPENAI_BASE_URL:
            raise RuntimeError("OPENAI_BASE_URL is required for openai-compatible")
        return OpenAIChatModel(
            LLM_MODEL,
            provider=OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY),
            profile=OpenAIModelProfile(
                openai_supports_tool_choice_required=False,
                openai_supports_strict_tool_definition=False,
                openai_chat_supports_multiple_system_messages=False,
            ),
        )
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER}")
