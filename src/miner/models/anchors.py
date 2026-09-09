"""Anchor intent and per-run synthesis models."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QueryType(StrEnum):
    PATTERN = "pattern"
    RULE = "rule"


class AstGrepExperienceOutcome(StrEnum):
    SUCCESS = "success"
    PITFALL = "pitfall"


MAX_SYNTHESIS_EXPERIENCES = 3
_EXPERIENCE_SEPARATOR = re.compile(r"[^\w]+")


class AstGrepExperience(BaseModel):
    """One reusable, evidence-backed ast-grep query-writing lesson."""

    model_config = ConfigDict(extra="forbid")

    outcome: AstGrepExperienceOutcome
    lesson: str = Field(..., min_length=8, max_length=240)

    @field_validator("lesson", mode="before")
    @classmethod
    def normalize_lesson(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @property
    def identity(self) -> str:
        """Ignore casing, Markdown punctuation, and spacing when deduplicating lessons."""

        return _EXPERIENCE_SEPARATOR.sub(" ", self.lesson.casefold()).strip()

    @model_validator(mode="after")
    def validate_meaningful_lesson(self) -> "AstGrepExperience":
        if not self.identity:
            raise ValueError("experience lesson must contain query-writing text")
        return self


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


class AnchorPlan(BaseModel):
    """Complete queryless plan submitted by the Rule Generator."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1)
    intents: list[AnchorIntent] = Field(..., min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_unique_intent_ids(self) -> "AnchorPlan":
        ids = [intent.id for intent in self.intents]
        if len(ids) != len(set(ids)):
            raise ValueError("anchor intent ids must be unique")
        return self


class AnchorSynthesisDelta(BaseModel):
    """Query fields and reusable ast-grep lessons returned by one Synthesizer."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target_anchor_id: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    query_type: QueryType = Field(..., alias="type")
    query: str
    query_weight: int = Field(..., ge=1, le=5)
    adjustments: list[str]
    experiences: list[AstGrepExperience] = Field(
        default_factory=list,
        max_length=MAX_SYNTHESIS_EXPERIENCES,
        description=(
            "At most three materially new, concise, project-independent ast-grep "
            "query-writing lessons; empty by default and never repeats existing guidance"
        ),
    )
    plan_suggestion: str = Field(
        ...,
        description=(
            "Conservative advisory note about deleting, merging, or revising plan "
            "intents; normally an empty string"
        ),
    )

    @model_validator(mode="after")
    def validate_unique_experiences(self) -> "AnchorSynthesisDelta":
        identities = [experience.identity for experience in self.experiences]
        if len(identities) != len(set(identities)):
            raise ValueError("experiences must not contain duplicate query-writing lessons")
        return self


class AnchorSynthesisResult(BaseModel):
    """Host-assembled Anchor plus advisory synthesis notes."""

    model_config = ConfigDict(extra="forbid")

    anchor: Anchor
    adjustments: list[str]
    experiences: list[AstGrepExperience] = Field(
        default_factory=list,
        max_length=MAX_SYNTHESIS_EXPERIENCES,
    )
    plan_suggestion: str
