"""Miner paths, environment loading, and tracing setup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langfuse import Langfuse, get_client

# =============================================================================
# Project paths
# =============================================================================

PROJECT_ROOT = Path(__file__).parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)

BASE_SRC_DIR = PROJECT_ROOT / "src"
MINER_SRC_DIR = BASE_SRC_DIR / "miner"
VAMINER_DIR = BASE_SRC_DIR / ".vaminer"
VAS_RULES_DIR = Path(
    os.getenv("VAMINER_RULES_DIR") or VAMINER_DIR / "skills" / "vas-scanner" / "rules"
).expanduser().resolve()
VAS_RULES_DIR.mkdir(parents=True, exist_ok=True)
MINER_LOG_DIR = PROJECT_ROOT / "logs"
MINER_LOG_DIR.mkdir(parents=True, exist_ok=True)
VAS_WORKSPACE_DIR = Path(
    os.getenv("VAMINER_WORKSPACE_DIR") or PROJECT_ROOT.parent / "vas_ws" / "miner"
).expanduser().resolve()
VAS_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _configure_ddgs_proxy(*, http_proxy: str | None, https_proxy: str | None) -> None:
    """Bridge standard proxy variables to the local web-search client."""
    proxy = https_proxy or http_proxy
    if proxy:
        os.environ.setdefault("DDGS_PROXY", proxy)


# =============================================================================
# External configuration
# =============================================================================

GITHUB_MIRROR_ENABLED = True
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HTTP_PROXY = os.getenv("HTTP_PROXY")
HTTPS_PROXY = os.getenv("HTTPS_PROXY")
NO_PROXY = os.getenv("NO_PROXY")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL")

# Agent Runtime selection. Per-phase routing is supplied by the CLI; this is
# the process-wide default used when no phase override exists.
MINER_AGENT_RUNTIME = (os.getenv("MINER_AGENT_RUNTIME") or "pydantic-ai").strip().lower()

# Claude Code CLI adapter. Authentication secrets stay in Claude settings or
# the process environment and are never copied into Miner configuration.
CLAUDE_CODE_COMMAND = (os.getenv("CLAUDE_CODE_COMMAND") or "claude").strip()
CLAUDE_CODE_MODEL = (os.getenv("CLAUDE_CODE_MODEL") or "").strip() or None
CLAUDE_CODE_SETTINGS = (os.getenv("CLAUDE_CODE_SETTINGS") or "").strip() or None
CLAUDE_CODE_SETTING_SOURCES = (os.getenv("CLAUDE_CODE_SETTING_SOURCES") or "none").strip()
CLAUDE_CODE_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_CODE_TIMEOUT_SECONDS") or "1800")
CLAUDE_CODE_MAX_BUDGET_USD = float(os.getenv("CLAUDE_CODE_MAX_BUDGET_USD") or "5")
CLAUDE_CODE_MAX_OUTPUT_BYTES = int(os.getenv("CLAUDE_CODE_MAX_OUTPUT_BYTES") or str(16 * 1024 * 1024))

_configure_ddgs_proxy(http_proxy=HTTP_PROXY, https_proxy=HTTPS_PROXY)

# =============================================================================
# Miner behavior
# =============================================================================

def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


MINER_MAX_REQUESTS_PER_AGENT = _positive_int_env("MINER_MAX_REQUESTS_PER_AGENT", 100)

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

# ast-grep runner execution limits
MINER_AST_GREP_TIMEOUT_SECONDS = 60
MINER_AST_GREP_SAMPLE_SIZE = 20
MINER_AST_GREP_MAX_SAMPLE_SIZE = 100

# ast-grep runner parallelism limits
MINER_AST_GREP_MAX_PARALLEL_RUNS = 5
MINER_AST_GREP_MAX_REQUESTS_PER_ANCHOR = _positive_int_env(
    "MINER_AST_GREP_MAX_REQUESTS_PER_ANCHOR",
    32,
)


_TRACING_CONFIGURED = False
_TRACING_ATTEMPTED = False
_TRACING_CLIENT: Langfuse | None = None


def configure_tracing() -> Langfuse | None:
    """Enable Pydantic AI OpenTelemetry spans and return the active Langfuse client."""
    global _TRACING_ATTEMPTED, _TRACING_CLIENT, _TRACING_CONFIGURED
    if _TRACING_CONFIGURED:
        return _TRACING_CLIENT
    if _TRACING_ATTEMPTED:
        return None
    _TRACING_ATTEMPTED = True
    try:
        from pydantic_ai import Agent

        langfuse = get_client()
        if not langfuse.auth_check():
            return None
        Agent.instrument_all()
        _TRACING_CLIENT = langfuse
        _TRACING_CONFIGURED = True
        return langfuse
    except Exception:  # noqa: BLE001 - optional tracing must not block mining.
        # Tracing is optional and must never prevent local mining or tests.
        return None


@contextmanager
def trace_pipeline(
    *,
    issue_input: str,
    vas_id: str,
) -> Iterator[Any | None]:
    """Create one active root observation for the complete issue pipeline."""
    langfuse = configure_tracing()
    if langfuse is None:
        yield None
        return

    with langfuse.start_as_current_observation(
        name=f"Miner Workflow for {vas_id}",
        as_type="chain",
        input={"issue_input": issue_input},
        metadata={"vas_id": vas_id},
    ) as pipeline_span:
        yield pipeline_span


def flush_tracing() -> None:
    """Flush pending telemetry for the short-lived CLI process."""
    if _TRACING_CLIENT is None:
        return
    try:
        _TRACING_CLIENT.flush()
    except Exception:  # noqa: BLE001 - optional tracing must not block shutdown.
        return
