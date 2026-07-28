"""Shared mutable context for one miner Agent run."""

from dataclasses import dataclass
from pathlib import Path

from ..utils.models import (
    AnchorSynthesisRequest,
    AnchorSynthesisResult,
    AnchorSynthesisRunRequest,
    RootCauseAnalysis,
)


@dataclass
class MinerContext:
    """Filesystem and cross-Agent state available through Pydantic AI dependencies."""

    workspace_root: Path
    repo_path: Path | None = None
    cases_dir: Path | None = None
    root_cause: RootCauseAnalysis | None = None
    anchor_synthesis_request: AnchorSynthesisRequest | None = None
    anchor_synthesis_run_request: AnchorSynthesisRunRequest | None = None
    anchor_synthesis: AnchorSynthesisResult | None = None
