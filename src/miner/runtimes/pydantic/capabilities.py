"""Reusable capabilities owned by the Pydantic AI adapter."""

from __future__ import annotations

import json

from pydantic_ai.capabilities import Capability, WebFetch, WebSearch
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import Tool
from pydantic_ai_harness.cache_stability import CacheStabilityMonitor
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    DeduplicateFileReads,
    SummarizingCompaction,
    TieredCompaction,
)

from ...tools.github import search_commit_by_tag, search_commit_by_time
from ...utils.fetch import FetchError, fetch_page
from .config import (
    MINER_COMPACTION_KEEP_TOKENS,
    MINER_COMPACTION_KEEP_TOOL_PAIRS,
    MINER_COMPACTION_MAX_TOKENS,
    MINER_COMPACTION_MIN_CLEAR_TOKENS,
)
from .context import MinerContext


async def _web_fetch(url: str) -> dict[str, str]:
    try:
        return await fetch_page(url)
    except FetchError as exc:
        raise ModelRetry(f"Failed to fetch {url}: {exc}") from exc


def file_read_key(call: ToolCallPart) -> str | None:
    """Return a stable identity for file reads that compaction may deduplicate."""
    if call.tool_name not in {"read_src_file", "read_case_artifact", "read_skill_resource"}:
        return None
    args = call.args_as_dict()
    path = args.get("path")
    if not isinstance(path, str):
        return None
    return json.dumps(
        [call.tool_name, path, args.get("start_line", 1), args.get("end_line")],
        separators=(",", ":"),
    )


def compaction_capability() -> TieredCompaction[MinerContext]:
    """Build the staged history-compaction policy shared by analysis agents."""
    return TieredCompaction(
        tiers=[
            DeduplicateFileReads(file_key=file_read_key),
            ClearToolResults(
                max_tokens=1,
                keep_pairs=MINER_COMPACTION_KEEP_TOOL_PAIRS,
                min_clear_tokens=MINER_COMPACTION_MIN_CLEAR_TOKENS,
            ),
            SummarizingCompaction(
                max_tokens=1,
                keep_tokens=MINER_COMPACTION_KEEP_TOKENS,
            ),
        ],
        target_tokens=MINER_COMPACTION_MAX_TOKENS,
    )


def commit_history_capability() -> Capability[MinerContext]:
    """Provide deferred, last-resort commit discovery tools."""
    return Capability(
        id="commit-history-search",
        description=(
            "Last-resort tag and time-range commit discovery. Load only when direct issue, advisory, "
            "pull-request, commit, and available web evidence did not identify a fixing revision."
        ),
        instructions=(
            "Use tag or time-range commit search only while the fixing revision remains unresolved. "
            "Choose the narrowest evidence-supported tag prefix or time range. Never use these tools "
            "only to reconfirm a fixing commit already supported by stronger evidence."
        ),
        tools=[search_commit_by_tag, search_commit_by_time],
        defer_loading=True,
    )


def web_search_capability() -> WebSearch[MinerContext]:
    """Provide deferred fallback web search."""
    return WebSearch(
        local=True,
        id="web-search",
        description=(
            "Fallback web search for unresolved advisory, pull-request, release, or commit evidence "
            "after specialized issue tools are exhausted."
        ),
        defer_loading=True,
    )


def web_fetch_capability() -> WebFetch[MinerContext]:
    """Provide deferred native-first web fetching with a local HTTPX fallback."""
    return WebFetch(
        local=Tool(
            _web_fetch,
            name="web_fetch",
            description="Fetch a public web page and return its readable text content.",
        ),
        id="web-fetch",
        description="Fetch and inspect a specific public web source identified during issue research.",
        defer_loading=True,
    )


def cache_stability_capability() -> CacheStabilityMonitor[MinerContext]:
    """Build the cache-stability monitor used by long-running analysis agents."""
    return CacheStabilityMonitor()


__all__ = [
    "cache_stability_capability",
    "commit_history_capability",
    "compaction_capability",
    "file_read_key",
    "web_fetch_capability",
    "web_search_capability",
]
