"""Minimal dependency context owned by the Pydantic AI Adapter."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MinerContext:
    workspace_root: Path
