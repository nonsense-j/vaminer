"""Anchor intent and per-run synthesis models."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analysis import RootCauseAnalysis


class QueryType(StrEnum):
    PATTERN = "pattern"
    RULE = "rule"


class Anchor(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(
        ...,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Stable kebab-case anchor id",
    )
    behavior_weight: int = Field(
        ...,
        ge=1,
        le=5,
        description="Rule importance of the immutable inspection behavior",
    )
    query_weight: int = Field(
        ...,
        ge=1,
        le=5,
        description="Candidate-ranking strength of one executable query match",
    )
    query_type: QueryType = Field(
        ...,
        alias="type",
        description="ast-grep query mode",
    )
    query: str = Field(
        ...,
        description=(
            "Raw pattern or YAML rule body; an empty string disables this anchor "
            "when no trustworthy executable query can be produced"
        ),
    )
    behavior: str = Field(
        ...,
        description=(
            "One local, declarative, query-observable site behavior; excludes the "
            "surrounding root-cause chain, exploit conditions, and fixing guidance"
        ),
    )
    inspect_hint: str = Field(
        ...,
        description=(
            "Non-verdict guidance for investigating security-relevant relationships "
            "or conditions after the site is matched; not query semantics"
        ),
    )

    @model_validator(mode="after")
    def validate_weight_order(self) -> "Anchor":
        if self.query_weight > self.behavior_weight:
            raise ValueError("query_weight must be less than or equal to behavior_weight")
        return self


class AnchorIntent(BaseModel):
    """Queryless inspection behavior and its synthesis targets."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    behavior_weight: int = Field(..., ge=1, le=5)
    behavior: str = Field(
        ...,
        description=(
            "One independent, local, query-observable rule-sensitive site behavior; "
            "must not describe another anchor, the full root-cause chain, exploit "
            "conditions, or a fix"
        ),
    )
    inspect_hint: str = Field(
        ...,
        description=(
            "Non-verdict post-match investigation guidance; may describe relevant "
            "relationships or unsafe conditions but does not define query matches"
        ),
    )
    required_cases: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Generated case files that contain this local behavior at a structurally "
            "matchable site; not every case must be assigned to every intent"
        ),
    )


class AnchorSynthesisRequest(BaseModel):
    """Complete synthesis request for every anchor intent."""

    model_config = ConfigDict(extra="forbid")

    root_cause: RootCauseAnalysis
    summary: str
    anchor_intents: list[AnchorIntent] = Field(..., min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_unique_intent_ids(self) -> "AnchorSynthesisRequest":
        ids = [intent.id for intent in self.anchor_intents]
        if len(ids) != len(set(ids)):
            raise ValueError("anchor intent ids must be unique")
        return self


class AnchorSynthesisRunRequest(BaseModel):
    """One target anchor plus the complete read-only plan passed to a Synthesizer."""

    model_config = ConfigDict(extra="forbid")

    root_cause: RootCauseAnalysis
    summary: str
    anchor_plan: list[AnchorIntent] = Field(
        ...,
        min_length=1,
        max_length=8,
        description=(
            "Complete ordered anchor plan; sibling intents provide behavior-boundary "
            "and overlap context only"
        ),
    )
    target_anchor_id: str = Field(
        ...,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Id of the one anchor intent to synthesize in this run",
    )

    @model_validator(mode="after")
    def validate_target_anchor(self) -> "AnchorSynthesisRunRequest":
        ids = [intent.id for intent in self.anchor_plan]
        if len(ids) != len(set(ids)):
            raise ValueError("anchor plan ids must be unique")
        if self.target_anchor_id not in ids:
            raise ValueError("target_anchor_id must identify an intent in anchor_plan")
        return self


class AnchorSynthesisRunResult(BaseModel):
    """One structurally valid anchor returned by an isolated Synthesizer."""

    model_config = ConfigDict(extra="forbid")

    anchor: Anchor
    adjustments: list[str]
    plan_suggestion: str = Field(
        ...,
        description=(
            "Conservative advisory note about deleting, merging, or revising plan "
            "intents; normally an empty string"
        ),
    )
