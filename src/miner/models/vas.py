"""Variant Analysis Specification models and source provenance."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .analysis import AstGrepLanguage
from .anchors import Anchor


class IssueCategory(StrEnum):
    SECURITY = "SECURITY"
    CORRECTNESS = "CORRECTNESS"
    PERFORMANCE = "PERFORMANCE"
    MAINTAINABILITY = "MAINTAINABILITY"
    CODESTYLE = "CODESTYLE"


class IssueVASSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["issue"] = "issue"
    issue_id: str
    repo_url: str
    buggy_commit: str
    fixed_commit: str | None = None
    root_cause_summary: str


class ExampleSuiteFileMetadata(BaseModel):
    """Portable metadata for one file accepted into an Example suite snapshot."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size: int = Field(..., ge=0)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source: bool


class ExampleSuiteVASSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["example_suite"] = "example_suite"
    registry_key: str
    suite_name: str
    content_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    snapshot_ref: str
    files: list[ExampleSuiteFileMetadata] = Field(..., min_length=1)
    root_cause_summary: str


VASSource = IssueVASSource | ExampleSuiteVASSource


class Scenarios(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unsafe: list[str] = Field(..., min_length=1)
    safe: list[str]


class VASFull(BaseModel):
    """Complete Variant Analysis Specification."""

    model_config = ConfigDict(extra="forbid")

    vas_id: str = "VAS-XXXX"
    category: IssueCategory
    language: AstGrepLanguage
    sources: list[VASSource] = Field(default_factory=list)
    summary: str
    scenarios: Scenarios
    anchors: list[Anchor] = Field(..., min_length=1)


class VASCoreInfo(BaseModel):
    """Validated VAS core assembled from an RCA and synthesized anchors."""

    model_config = ConfigDict(extra="forbid")

    category: IssueCategory
    language: AstGrepLanguage
    root_cause_summary: str
    summary: str
    scenarios: Scenarios
    anchors: list[Anchor] = Field(..., min_length=1)
