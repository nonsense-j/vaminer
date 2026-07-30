"""Mutable dependency context owned by the Pydantic AI adapter."""

from dataclasses import dataclass, field
from pathlib import Path

from ...models.analysis import AnalysisSubject, RootCauseAnalysis


@dataclass
class MinerContext:
    """Filesystem and cross-Agent state available through Pydantic AI dependencies."""

    workspace_root: Path
    source_root: Path | None = None
    repo_path: Path | None = None
    cases_dir: Path | None = None
    root_cause: RootCauseAnalysis | None = None
    analysis_subject: AnalysisSubject | None = None
    subagent_events: list[dict[str, str]] = field(default_factory=list)
