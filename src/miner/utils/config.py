"""Miner paths and general behavior configuration."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# =============================================================================
# Project paths
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)

BASE_SRC_DIR = PROJECT_ROOT / "src"
MINER_SRC_DIR = BASE_SRC_DIR / "miner"
VAMINER_DIR = BASE_SRC_DIR / ".vaminer"
VAS_RULES_DIR = (VAMINER_DIR / "skills" / "vas-scanner" / "rules").resolve()
MINER_OUTPUT_DIR = (
    Path(os.getenv("VAMINER_OUTPUT_DIR") or PROJECT_ROOT / "output").expanduser().resolve()
)
MINER_LOG_DIR = MINER_OUTPUT_DIR / "logs" / "miner"
VAS_WORKSPACE_DIR = (
    Path(os.getenv("VAMINER_WORKSPACE_DIR") or PROJECT_ROOT.parent / "vas_ws" / "miner").expanduser().resolve()
)


# =============================================================================
# External configuration
# =============================================================================

GITHUB_MIRROR_ENABLED = os.getenv("GITHUB_MIRROR_ENABLED", "false").strip().lower() in ("1", "true", "yes")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Agent Runtime selection. Per-phase routing is supplied by the CLI; this is
# the process-wide default used when no phase override exists.
MINER_AGENT_RUNTIME = (os.getenv("MINER_AGENT_RUNTIME") or "pydantic-ai").strip().lower()

# =============================================================================
# Miner behavior
# =============================================================================


def _pos_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


MINER_MAX_TURNS_ISSUE_COLLECTION = _pos_env("MINER_MAX_TURNS_ISSUE_COLLECTION", 40)
MINER_MAX_TURNS_ROOT_CAUSE = _pos_env("MINER_MAX_TURNS_ROOT_CAUSE", 40)
MINER_MAX_TURNS_RULE_GENERATION = _pos_env("MINER_MAX_TURNS_RULE_GENERATION", 100)

# ast-grep runner execution limits
MINER_AST_GREP_TIMEOUT_SECONDS = 60
MINER_AST_GREP_SAMPLE_SIZE = 20
MINER_AST_GREP_MAX_SAMPLE_SIZE = 100

# ast-grep runner parallelism limits
MINER_AST_GREP_MAX_PARALLEL_RUNS = 5
MINER_MAX_TURNS_PER_ANCHOR = _pos_env("MINER_MAX_TURNS_PER_ANCHOR", 30)
