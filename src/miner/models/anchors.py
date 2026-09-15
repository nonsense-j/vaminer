"""Anchor intent and per-run synthesis models."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .base import InlineJsonSchemaModel


class QueryType(StrEnum):
    PATTERN = "pattern"
    RULE = "rule"


class AstGrepExperienceMode(StrEnum):
    ADD = "ADD"
    REPLACE = "REPLACE"


MAX_SYNTHESIS_EXPERIENCES = 3


_EXPERIENCE_ID = re.compile(
    r"^(?P<scope>ALL|[a-z][a-z0-9]*)-(?P<number>[1-9][0-9]*)$",
    re.IGNORECASE,
)


class AstGrepExperience(BaseModel):
    """One reusable, evidence-backed ast-grep query-writing lesson."""

    model_config = ConfigDict(extra="forbid")

    mode: AstGrepExperienceMode
    lesson_id: str
    lesson: str = Field(
        ...,
        min_length=8,
        description=(
            "A concise, generic, project-independent ast-grep query-writing "
            "lesson expressed in 1-2 sentences"
        ),
    )

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("lesson_id", mode="before")
    @classmethod
    def normalize_lesson_id(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        match = _EXPERIENCE_ID.fullmatch(value.strip())
        if match is None:
            raise ValueError("lesson_id must use the form ALL-N or LANGUAGE-N")
        return f"{match.group('scope').upper()}-{int(match.group('number'))}"

    @field_validator("lesson", mode="before")
    @classmethod
    def normalize_lesson(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @property
    def identity(self) -> str:
        """Return the stable ID used to update this lesson."""

        return self.lesson_id.casefold()

    @property
    def scope(self) -> str:
        return self.lesson_id.rsplit("-", 1)[0]


def _normalize_synthesis_experiences(
    experiences: list[AstGrepExperience],
) -> list[AstGrepExperience]:
    """Keep the first three ID-addressed lessons from one final Synthesizer output."""

    selected: list[AstGrepExperience] = []
    positions: dict[str, int] = {}
    for experience in experiences:
        identity = experience.identity
        if identity in positions:
            selected[positions[identity]] = experience
        else:
            positions[identity] = len(selected)
            selected.append(experience)
    return selected[:MAX_SYNTHESIS_EXPERIENCES]


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
    """Inspection behavior and its synthesis targets, with an optional query draft."""

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
            "matchable site; every declared case must be assigned to at least one "
            "intent, but not every case must be assigned to every intent"
        ),
    )
    draft_query: str | None = Field(
        default=None,
        description=(
            "Optional unvalidated raw ast-grep pattern or YAML rule draft for this intent. "
            "When replanning, copy, adapt, or combine prior queries as a starting point; "
            "the Synthesizer refines and validates the final query"
        ),
    )


class AnchorReuse(BaseModel):
    """Keep one Anchor from the latest successful synthesis batch unchanged."""

    model_config = ConfigDict(extra="forbid")

    reuse_anchor_id: str = Field(
        ...,
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="ID from the latest successful batch whose intent and query should be reused unchanged",
    )


class AnchorPlanRequest(InlineJsonSchemaModel):
    """Complete desired Anchor set, with explicit generation or reuse for each entry."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1)
    intents: list[AnchorIntent | AnchorReuse] = Field(
        ...,
        min_length=1,
        description=(
            "The complete ordered Anchor set to keep. A full AnchorIntent always runs synthesis, "
            "optionally starting from draft_query; a reuse_anchor_id entry preserves an existing "
            "Anchor unchanged. Omitted Anchors are removed. Together the entries must cover every declared case."
        ),
    )

    @model_validator(mode="after")
    def validate_unique_anchor_ids(self) -> "AnchorPlanRequest":
        ids = [item.reuse_anchor_id if isinstance(item, AnchorReuse) else item.id for item in self.intents]
        if len(ids) != len(set(ids)):
            raise ValueError("anchor ids must be unique across generated and reused entries")
        return self


class AnchorPlan(BaseModel):
    """Complete canonical plan after the host has resolved any reuse references."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1)
    intents: list[AnchorIntent] = Field(
        ...,
        min_length=1,
        description=(
            "All independent local behaviors needed for complete coverage of the "
            "declared Case Artifacts; there is no fixed intent-count limit"
        ),
    )

    @model_validator(mode="after")
    def validate_unique_intent_ids(self) -> "AnchorPlan":
        ids = [intent.id for intent in self.intents]
        if len(ids) != len(set(ids)):
            raise ValueError("anchor intent ids must be unique")
        return self


class AnchorSynthesisDelta(InlineJsonSchemaModel):
    """Query fields and reusable ast-grep lessons returned by one Synthesizer."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    anchor_id: str = Field(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    query_type: QueryType = Field(..., alias="type")
    query: str
    query_weight: int = Field(..., ge=1, le=5)
    adjustments: list[str]
    experiences: list[AstGrepExperience] = Field(
        default_factory=list,
        description=(
            "Optional ADD or REPLACE operations for concise, generic, "
            "project-independent ast-grep query-writing lessons; each lesson is "
            "1-2 sentences, empty by default, and capped at three lessons per "
            "Synthesizer output"
        ),
    )
    plan_suggestion: str = Field(
        ...,
        description=(
            "Conservative advisory note about deleting, merging, or revising plan "
            "intents; normally an empty string"
        ),
    )

    @field_validator("experiences", mode="after")
    @classmethod
    def normalize_experiences(
        cls,
        experiences: list[AstGrepExperience],
    ) -> list[AstGrepExperience]:
        return _normalize_synthesis_experiences(experiences)


class AnchorSynthesisResult(BaseModel):
    """Host-assembled Anchor plus advisory synthesis notes."""

    model_config = ConfigDict(extra="forbid")

    anchor: Anchor
    adjustments: list[str]
    experiences: list[AstGrepExperience] = Field(
        default_factory=list,
        description="Up to three deduplicated experience operations from the Synthesizer",
    )
    plan_suggestion: str

    @field_validator("experiences", mode="after")
    @classmethod
    def normalize_experiences(
        cls,
        experiences: list[AstGrepExperience],
    ) -> list[AstGrepExperience]:
        return _normalize_synthesis_experiences(experiences)
