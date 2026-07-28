"""Pydantic models for the miner pipeline (VAS + intermediate models)."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IssueCategory(str, Enum):
    SECURITY = "SECURITY"
    CORRECTNESS = "CORRECTNESS"
    PERFORMANCE = "PERFORMANCE"
    MAINTAINABILITY = "MAINTAINABILITY"
    CODESTYLE = "CODESTYLE"


# =============================================================================
# Raw Info Models (tool outputs)
# =============================================================================


class CommitRawInfo(BaseModel):
    """Commit information for locating buggy/fixed versions."""

    commit_url: str | None = Field(None, description="GitHub commit URL")
    cur_sha: str = Field(..., description="The fixed commit SHA (current)")
    parent_sha: str = Field(..., description="The parent SHA of the fixed commit (buggy)")
    timestamp: str = Field(..., description="Commit timestamp (YYYY-MM-DDTHH:MM)")
    msg: str = Field(..., description="Commit message")


class IssueRawInfo(BaseModel):
    """Standardized issue information from any source (CVE or GitHub)."""

    raw_desc: str = Field(..., description="Direct description of the issue")
    repo_url: str | None = Field(None, description="Repository URL derived from issue/CVE")
    timestamp: str | None = Field(None, description="Issue creation timestamp (YYYY-MM-DDTHH:MM)")
    extra_notes: str | None = Field(None, description="Additional info (e.g., discussions)")
    commits: list[CommitRawInfo] = Field(default_factory=list, description="Related commits")
    references: list[str] = Field(default_factory=list, description="Other related URLs")


class RepoCheckout(BaseModel):
    """Result of cloning a repository."""

    repo_path: str
    buggy_branch: str = "buggy"
    fixed_branch: str | None = "fixed"


# =============================================================================
# VAS Models (final output)
# =============================================================================


class VASSource(BaseModel):
    issue_id: str = Field(..., description="CVE ID or GitHub issue URL")
    repo_url: str = Field(..., description="GitHub repository URL")
    buggy_commit: str = Field(..., description="Commit SHA of the buggy version")
    fixed_commit: str | None = Field(None, description="Commit SHA of the fixed version")
    root_cause_summary: str = Field(..., description="Concise summary of the code-level root cause pattern")


class QueryType(str, Enum):
    PATTERN = "pattern"
    RULE = "rule"


class AstGrepLanguage(str, Enum):
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    GO = "go"
    JAVA = "java"
    JAVASCRIPT = "javascript"
    JSX = "jsx"
    KOTLIN = "kotlin"
    PHP = "php"
    PYTHON = "python"
    RUBY = "ruby"
    RUST = "rust"
    SCALA = "scala"
    SWIFT = "swift"
    TSX = "tsx"
    TYPESCRIPT = "typescript"


class BuggyComponent(BaseModel):
    """One concrete repository location participating in the root cause."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(..., description="Repository-relative source path")
    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)
    role: str = Field(..., description="Concise role of this component in the defect")
    snippet: str = Field(..., min_length=1, description="Exact source excerpt from the buggy revision")

    @model_validator(mode="after")
    def validate_line_range(self) -> "BuggyComponent":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class RootCauseAnalysis(BaseModel):
    """Evidence-backed root-cause analysis with a persistent minimal-case manifest."""

    model_config = ConfigDict(extra="forbid")

    language: AstGrepLanguage = Field(..., description="Primary source language for cases and downstream anchors")
    root_cause_summary: str = Field(..., min_length=1, description="Concise code-level defect pattern")
    analysis: str = Field(..., min_length=1, description="Focused causal explanation for downstream rule generation")
    buggy_components: list[BuggyComponent] = Field(..., min_length=1)
    fixing_pattern: str = Field(
        ...,
        min_length=1,
        description="Evidence-backed invariant-restoring change, or an explicitly marked inference when no fix exists",
    )
    extracted_case_files: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Bare filenames directly under the cases tool root, using caseN and caseN_varM naming; "
            "never prefix them with cases/"
        ),
    )


class Anchor(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(
        ...,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Stable kebab-case anchor id, unique within the rule",
    )
    behavior_weight: int = Field(
        ...,
        ge=1,
        le=5,
        description="Rule importance of the immutable inspection behavior, from 1 to 5",
    )
    query_weight: int = Field(
        ...,
        ge=1,
        le=5,
        description="Candidate-ranking strength of one executable query match, from 1 to behavior_weight",
    )
    query_type: QueryType = Field(
        ...,
        alias="type",
        description=(
            "ast-grep query mode. Use pattern for a simple ast-grep pattern string; "
            "use rule for YAML beginning with rule: and omitting id/language."
        ),
    )
    query: str = Field(..., description="Raw ast-grep pattern or YAML rule body for this single anchor")
    behavior: str = Field(
        ...,
        description=(
            "One English declarative sentence abstracting the code behavior directly identified by the query "
            "for semantic retrieval. Do not include an inspection directive or an unsupported defect verdict."
        ),
    )
    inspect_hint: str = Field(
        ...,
        description=(
            "One English non-verdict inspection hint. Say matched/check/trace/inspect, "
            "but do not assert unsafe, vulnerable, buggy, unchecked, tainted, or exploited."
        ),
    )

    @model_validator(mode="after")
    def validate_weight_order(self) -> "Anchor":
        if self.query_weight > self.behavior_weight:
            raise ValueError("query_weight must be less than or equal to behavior_weight")
        return self


class Scenarios(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unsafe: list[str] = Field(
        ...,
        min_length=1,
        description="Independent complete issue-derived defect scenarios; matching any one is sufficient",
    )
    safe: list[str] = Field(
        ...,
        description="Independent complete safe scenarios; matching any one rules out the defect",
    )


class AnchorIntent(BaseModel):
    """Queryless inspection behavior and its hard synthesis recall contract."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Stable kebab-case id preserved by the synthesized anchor",
    )
    behavior_weight: int = Field(
        ...,
        ge=1,
        le=5,
        description="Rule importance of this discrete causal-chain inspection behavior, from 1 to 5",
    )
    behavior: str = Field(
        ...,
        description="Declarative defect-hotspot behavior that the synthesized query should identify",
    )
    inspect_hint: str = Field(
        ...,
        description="Non-verdict guidance for reasoning from the hotspot to the complete defect scenario",
    )
    required_cases: list[str] = Field(
        ...,
        min_length=1,
        description="Hard recall set that the synthesized query must cover",
    )


class AnchorSynthesisRequest(BaseModel):
    """Structured batch request passed from the rule generator to the synthesis orchestrator."""

    model_config = ConfigDict(extra="forbid")

    root_cause: RootCauseAnalysis = Field(..., description="Complete authoritative typed root-cause analysis")
    summary: str = Field(..., description="Normative VAS rule summary that the anchors support but do not prove")
    anchor_intents: list[AnchorIntent] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="Complete set of distinct queryless intents; return exactly one anchor per intent",
    )


class AnchorSynthesisRunRequest(BaseModel):
    """One isolated anchor intent passed to one AST-grep Synthesizer run."""

    model_config = ConfigDict(extra="forbid")

    root_cause: RootCauseAnalysis = Field(..., description="Complete authoritative typed root-cause analysis")
    summary: str = Field(..., description="Normative VAS rule summary that this anchor supports but does not prove")
    anchor_intent: AnchorIntent = Field(
        ...,
        description="The single immutable intent this run must compile and validate",
    )


class AnchorSynthesisRunResult(BaseModel):
    """One validated anchor produced without model-authored batch metadata."""

    model_config = ConfigDict(extra="forbid")

    anchor: Anchor = Field(..., description="The single public anchor compiled for this run's intent")
    adjustments: list[str] = Field(
        ...,
        description="Query generalization, precision refinement, or query-weight reductions; empty if none",
    )


class CaseCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Path relative to cases/")
    anchor_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Exact ids of the submitted anchors that match this case in the validation scan",
    )


class RepoEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_id: str = Field(..., description="Id of the anchor producing this repository match")
    file: str = Field(..., description="Repository-relative path of an RCA-site validation match")
    line: int = Field(..., ge=1, description="One-based start line of the RCA-site match")


class AnchorSynthesisResult(BaseModel):
    """Validated anchor batch with exact case coverage and repository evidence."""

    model_config = ConfigDict(extra="forbid")

    anchors: list[Anchor] = Field(
        ...,
        min_length=1,
        description="One validated public anchor for every approved intent",
    )
    case_coverage: list[CaseCoverage] = Field(
        ...,
        min_length=1,
        description="Exact case-to-anchor coverage produced by validation",
    )
    repo_evidence: list[RepoEvidence] = Field(
        ...,
        min_length=1,
        description="At least one actual RCA-declared buggy-repository site match for every anchor",
    )
    adjustments: list[str] = Field(
        ...,
        description="Query generalization, precision refinement, or query-weight reductions; empty if none",
    )


class VASFull(BaseModel):
    """Complete Variant Analysis Specification."""

    model_config = ConfigDict(extra="forbid")

    vas_id: str = Field(default="VAS-XXXX", description="VAS identifier placeholder")
    category: IssueCategory = Field(..., description="Issue category")
    language: AstGrepLanguage = Field(..., description="Single ast-grep language id selected from the enum")
    sources: list[VASSource] = Field(default_factory=list, description="Issue sources")
    summary: str = Field(
        ...,
        description=(
            "One English sentence stating a repo-agnostic normative software behavior requirement using must/should"
        ),
    )
    scenarios: Scenarios = Field(..., description="Independent unsafe and safe behavioral scenarios")
    anchors: list[Anchor] = Field(
        ..., min_length=1, description="Rule-sensitive anchors for deterministic candidate discovery"
    )


# =============================================================================
# Agents Output
# =============================================================================


class IssueCollectionInfo(BaseModel):
    """Resolved issue evidence and verified checkout metadata."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., description="CVE ID or GitHub issue URL")
    issue_summary: str = Field(..., description="A clear and repo-agnostic summary of the issue and impact")
    issue_details: str = Field(
        ...,
        description="Evidence-rich issue description retaining concrete components and code-pattern clues for RCA",
    )
    repo_url: str = Field(..., description="Repository URL")
    repo_path: str = Field(..., description="Local path of the verified checkout created by clone_repo")
    buggy_commit: str = Field(..., description="Commit SHA currently checked out on the buggy branch")
    fixed_commit: str | None = Field(
        None,
        description="Evidence-supported fixing commit SHA, or null when no fixing revision is established",
    )


class VASCoreInfo(BaseModel):
    """Validated VAS core assembled from an authoritative RCA and synthesized anchors."""

    model_config = ConfigDict(extra="forbid")

    category: IssueCategory = Field(..., description="Issue category")
    language: AstGrepLanguage = Field(..., description="Single ast-grep language id selected from the enum")
    root_cause_summary: str = Field(..., description="Concise summary of the code-level root cause pattern")
    summary: str = Field(
        ...,
        description=(
            "One English sentence stating a repo-agnostic normative software behavior requirement using must/should"
        ),
    )
    scenarios: Scenarios = Field(..., description="Independent unsafe and safe behavioral scenarios")
    anchors: list[Anchor] = Field(
        ..., min_length=1, description="Rule-sensitive anchors for deterministic candidate discovery"
    )
