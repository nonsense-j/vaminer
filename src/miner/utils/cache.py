"""Typed workspace cache loading, validation, and persistence."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ..models.analysis import AnalysisSubject, RootCauseAnalysis
from ..models.issue import IssueCollectionInfo
from ..models.vas import VASCoreInfo
from .log import logger
from ..mining.validation.analysis import validate_issue_checkout, validate_root_cause_analysis
from ..mining.validation.anchors import validate_anchors


class AgentCache:
    """Workspace-local cache for one typed agent result."""

    def __init__(
        self,
        agent_name: str,
        cache_dir: Path,
        suffix: str = "json",
        *,
        runtime: str | None = None,
        model: str | None = None,
    ) -> None:
        self.agent_name = self._part(agent_name)
        identity = [self.agent_name]
        if runtime is not None:
            identity.append(self._part(runtime))
        if model is not None:
            identity.append(self._part(model))
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = cache_dir / f"{'.'.join(identity)}.{suffix}"

    @staticmethod
    def _part(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9._-]+", "_", value.strip().lower())
        return normalized.strip("._-") or "unknown"

    def get(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else None

    def set(self, result: str | BaseModel) -> None:
        if isinstance(result, BaseModel):
            text = result.model_dump_json(indent=2, by_alias=True)
        else:
            text = str(result)
            if self.path.suffix == ".json":
                with suppress(json.JSONDecodeError):
                    text = json.dumps(json.loads(text), indent=2)
        self.path.write_text(text, encoding="utf-8")


def load_collection_cache(
    cache: AgentCache,
    *,
    workspace_root: Path,
) -> IssueCollectionInfo | None:
    """Load issue collection data only when its checkout is still valid."""
    raw = cache.get()
    if raw is None:
        return None
    try:
        collection = IssueCollectionInfo.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning("Issue collection cache is invalid and will be regenerated: %s", exc)
        return None
    errors = validate_issue_checkout(collection, workspace_root=workspace_root)
    if errors:
        logger.warning("Issue collection cache failed checkout validation: %s", "; ".join(errors))
        return None
    return collection


def load_root_cause_cache(
    cache: AgentCache,
    *,
    source_root: Path,
    cases_dir: Path,
) -> RootCauseAnalysis | None:
    """Load root cause data only when its cases and source evidence remain valid."""
    raw = cache.get()
    if raw is None:
        return None
    try:
        analysis = RootCauseAnalysis.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning("RCA cache is invalid and will be regenerated: %s", exc)
        return None
    errors = validate_root_cause_analysis(
        analysis,
        source_root=source_root,
        cases_dir=cases_dir,
    )
    if errors:
        logger.warning("RCA cache failed case validation: %s", "; ".join(errors))
        return None
    return analysis


def load_rule_cache(
    cache: AgentCache,
    *,
    repo_path: Path | None = None,
    source_root: Path | None = None,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    analysis_subject: AnalysisSubject,
) -> VASCoreInfo | None:
    """Load a generated rule only when its anchors still pass validation."""
    raw = cache.get()
    if raw is None:
        return None
    try:
        core = VASCoreInfo.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning("Rule cache is invalid and will be regenerated: %s", exc)
        return None
    errors = validate_anchors(
        core,
        repo_path=repo_path,
        source_root=source_root,
        cases_dir=cases_dir,
        root_cause=root_cause,
        analysis_subject=analysis_subject,
    )
    if errors:
        logger.warning("Rule cache failed anchor validation: %s", "; ".join(errors))
        return None
    return core
