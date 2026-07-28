"""Reusable Pydantic AI capabilities for Miner agents."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic_ai.capabilities import AbstractCapability, Capability, WebFetch, WebSearch
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.toolsets import AgentToolset
from pydantic_ai_harness.cache_stability import CacheStabilityMonitor
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    DeduplicateFileReads,
    SummarizingCompaction,
    TieredCompaction,
)
from pydantic_ai_harness.overflowing_tool_output import (
    Band,
    LocalFileStore,
    OverflowingToolOutput,
    Spill,
    Truncate,
)

from ..configs import (
    MINER_COMPACTION_KEEP_TOKENS,
    MINER_COMPACTION_KEEP_TOOL_PAIRS,
    MINER_COMPACTION_MAX_TOKENS,
    MINER_COMPACTION_MIN_CLEAR_TOKENS,
    MINER_OVERFLOW_MAX_TOKENS,
    MINER_OVERFLOW_PREVIEW_CHARS,
    MINER_OVERFLOW_TTL_HOURS,
)
from ..tools.github import search_commit_by_tag, search_commit_by_time
from .context import MinerContext

AgentCapability = AbstractCapability[MinerContext]


def overflow_capability() -> OverflowingToolOutput[MinerContext]:
    """Bound oversized tool results and spill recoverable previews locally."""
    return OverflowingToolOutput(
        bands=[
            Band(
                over=MINER_OVERFLOW_MAX_TOKENS,
                action=Spill(preview_chars=MINER_OVERFLOW_PREVIEW_CHARS, then=Truncate()),
            )
        ],
        over_tokens=True,
        store=LocalFileStore(cleanup_after=timedelta(hours=MINER_OVERFLOW_TTL_HOURS)),
    )


def file_read_key(call: ToolCallPart) -> str | None:
    """Return a stable identity for file reads that compaction may deduplicate."""
    if call.tool_name not in {"repo_read_file", "cases_read_file", "skill_read_file"}:
        return None
    args = call.args_as_dict()
    path = args.get("path")
    if not isinstance(path, str):
        return None
    return json.dumps(
        [call.tool_name, path, args.get("offset", 0), args.get("limit")],
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
    """Provide deferred fetching of a specific public source."""
    return WebFetch(
        local=True,
        id="web-fetch",
        description="Fetch and inspect a specific public web source identified during issue research.",
        defer_loading=True,
    )


def cache_stability_capability() -> CacheStabilityMonitor[MinerContext]:
    """Build the cache-stability monitor used by long-running analysis agents."""
    return CacheStabilityMonitor()


def local_skill_capability(
    path: Path,
    *,
    defer_loading: bool,
    toolsets: Sequence[AgentToolset[MinerContext]] = (),
) -> Capability[MinerContext]:
    """Load a local SKILL.md and its related toolsets as one capability."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---") or path.suffix != ".md":
        raise ValueError(f"skill is missing YAML frontmatter: {path}")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter) or {}
    if not isinstance(metadata, dict):
        raise TypeError(f"skill frontmatter must be a mapping: {path}")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"skill name is missing: {path}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"skill description is missing: {path}")
    return Capability(
        id=name.strip(),
        description=description.strip(),
        instructions=body.strip(),
        toolsets=toolsets,
        defer_loading=defer_loading,
    )


__all__ = [
    "AgentCapability",
    "cache_stability_capability",
    "commit_history_capability",
    "compaction_capability",
    "file_read_key",
    "local_skill_capability",
    "overflow_capability",
    "web_fetch_capability",
    "web_search_capability",
]
