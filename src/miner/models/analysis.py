"""Root Cause Analysis models."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AstGrepLanguage(StrEnum):
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


class GroundingPolicy(StrEnum):
    """Deterministic source acceptance selected by an Input Adapter."""

    REPOSITORY_EVIDENCE = "repo_evidence"
    BAD_SPAN_COVERAGE = "bad_span_coverage"


class BuggyComponent(BaseModel):
    """One concrete source location participating in the root cause."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(
        ...,
        description="Path relative to the analyzed repository or snapshot under workspace src/",
    )
    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)
    role: str = Field(..., description="Concise role of this location in the defect")
    snippet: str = Field(..., min_length=1, description="Exact source excerpt from the buggy revision")

    @model_validator(mode="after")
    def validate_line_range(self) -> "BuggyComponent":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class RootCauseAnalysis(BaseModel):
    """Evidence-backed root-cause analysis with a persistent case manifest."""

    model_config = ConfigDict(extra="forbid")

    language: AstGrepLanguage = Field(..., description="Primary source language")
    root_cause_summary: str = Field(..., min_length=1, description="Concise code-level defect pattern")
    analysis: str = Field(..., min_length=1, description="Focused causal explanation")
    buggy_components: list[BuggyComponent] = Field(..., min_length=1)
    fixing_pattern: str = Field(
        ...,
        min_length=1,
        description="Evidence-backed invariant-restoring change, or an explicitly marked inference",
    )
    extracted_case_files: list[str] = Field(
        ...,
        min_length=1,
        description="Bare caseN or caseN_varM filenames directly under the cases root",
    )
