"""Configuration owned by the Pydantic AI adapter."""

from __future__ import annotations

import os

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")

MINER_FS_MAX_READ_LINES = 200
MINER_FS_MAX_SEARCH_RESULTS = 200
MINER_FS_MAX_FIND_RESULTS = 200

MINER_OVERFLOW_MAX_TOKENS = 4_000
MINER_OVERFLOW_PREVIEW_CHARS = 1_200
MINER_OVERFLOW_TTL_HOURS = 24

MINER_COMPACTION_MAX_TOKENS = 40_000
MINER_COMPACTION_KEEP_TOKENS = 16_000
MINER_COMPACTION_KEEP_TOOL_PAIRS = 6
MINER_COMPACTION_MIN_CLEAR_TOKENS = 8_000


def configure_web_proxy(*, http_proxy: str | None, https_proxy: str | None) -> None:
    """Bridge standard proxy variables to the Pydantic web-search adapter."""

    proxy = https_proxy or http_proxy
    if proxy:
        os.environ.setdefault("DDGS_PROXY", proxy)


configure_web_proxy(
    http_proxy=os.getenv("HTTP_PROXY"),
    https_proxy=os.getenv("HTTPS_PROXY"),
)
