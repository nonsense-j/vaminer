"""Shared semantic types for runtime tool input schemas."""

from typing import Annotated, Literal

from pydantic import Field

from .anchors import AnchorPlanRequest

SourceListPath = Annotated[
    str | None,
    Field(description="Existing source-root-relative directory; omit to list the whole root."),
]
SourceSearchPath = Annotated[
    str | None,
    Field(description="Existing source-root-relative file or directory; omit to search the whole root."),
]
SourceGlob = Annotated[
    str | None,
    Field(description="Ripgrep glob filter, such as `*.c` or `!vendor/**`."),
]
SourceListLimit = Annotated[
    int,
    Field(ge=1, le=500, description="Caps returned paths; narrow `path` or `glob` if truncated."),
]
SourceSearchPattern = Annotated[str, Field(description="Single-line text or expression to search for.")]
SourceSearchMode = Annotated[
    Literal["literal", "regex"],
    Field(description="`literal` preserves `pattern`; `regex` uses ripgrep regex syntax."),
]
SourceSearchLimit = Annotated[
    int,
    Field(ge=1, le=100, description="Caps returned matching lines; narrow the search if truncated."),
]
SourceFilePath = Annotated[str, Field(description="Source-root-relative file path.")]
ReadStartLine = Annotated[
    int,
    Field(ge=1, description="One-based first line; continue after the displayed end when truncated."),
]
ReadEndLine = Annotated[
    int | None,
    Field(description="Inclusive last line; omit to read at most 200 lines."),
]
ReadFullFile = Annotated[
    bool,
    Field(description="Return the whole file; incompatible with explicit line bounds."),
]

CaseArtifactPath = Annotated[
    str,
    Field(
        pattern=r"^case\d+(?:_var\d+)?\.[A-Za-z0-9]+$",
        description="Bare `caseN.ext` or `caseN_varM.ext` filename.",
    ),
]
CaseArtifactContent = Annotated[str, Field(description="Non-empty Case Artifact content.")]

CVEId = Annotated[
    str,
    Field(
        pattern=r"^CVE-[0-9]{4}-[0-9]{4,}$",
        description="`CVE-YYYY-NNNN` identifier with at least four sequence digits.",
    ),
]
GitHubIssueUrl = Annotated[str, Field(description="Full GitHub issue URL.")]
FetchExtraNotes = Annotated[
    bool,
    Field(description="Include issue comments in `extra_notes` when their evidence is needed."),
]
GitHubCommitUrl = Annotated[str, Field(description="Full GitHub commit URL.")]
RepositoryUrl = Annotated[str, Field(description="Repository URL.")]
BuggyCommitSha = Annotated[
    str,
    Field(pattern=r"^[A-Fa-f0-9]{7,64}$", description="Commit fetched into `buggy` and checked out."),
]
FixedCommitSha = Annotated[
    str | None,
    Field(description="Commit fetched into `fixed` for diff inspection."),
]
GitHubOwner = Annotated[str, Field(description="GitHub repository owner without slashes.")]
GitHubRepository = Annotated[str, Field(description="GitHub repository name without slashes.")]
GitTagPrefix = Annotated[
    str,
    Field(description="Narrowest evidence-supported tag prefix, such as `v2.7`."),
]
CommitRangeStart = Annotated[
    str,
    Field(description="Inclusive ISO timestamp with timezone, such as `2025-01-01T00:00:00Z`."),
]
CommitRangeEnd = Annotated[
    str,
    Field(description="Inclusive ISO timestamp with timezone; must not precede `since`."),
]
PatchPath = Annotated[
    str | None,
    Field(description="Repository-relative file or directory for a full patch; omit for a diffstat."),
]

AnchorPlanInput = Annotated[
    AnchorPlanRequest,
    Field(description="Complete desired Anchor set; reuse entries preserve prior results."),
]
SkillResourceLimit = Annotated[
    int,
    Field(ge=1, le=100, description="Caps returned skill-root-relative paths."),
]
SkillResourcePath = Annotated[str, Field(description="Skill-root-relative resource path.")]

AstGrepTarget = Annotated[
    Literal["src", "cases"],
    Field(description="`src` searches analyzed source; `cases` searches generated Case Artifacts."),
]
AstGrepLanguage = Annotated[str, Field(description="ast-grep language identifier.")]
AstGrepQueryType = Annotated[
    Literal["pattern", "rule"],
    Field(description="Selects raw-pattern or YAML-rule interpretation of `query`."),
]
AstGrepQuery = Annotated[str, Field(description="Raw pattern text or YAML rule body to execute.")]
AstGrepOutput = Annotated[
    Literal["count", "sample", "full"],
    Field(
        description=(
            "`count` returns totals; `sample` adds bounded file/range/snippet entries; "
            "`full` returns every entry plus metavariable captures."
        )
    ),
]
AstGrepSampleSize = Annotated[
    int,
    Field(ge=1, le=100, description="Caps `sample` entries; ignored by other output modes."),
]
AstGrepPattern = Annotated[str, Field(description="Raw ast-grep pattern to inspect.")]
AstGrepDebugQuery = Annotated[
    Literal["pattern", "ast", "cst", "sexp"],
    Field(
        description=(
            "`pattern` shows matcher form; `ast` named nodes; `cst` named and unnamed nodes; "
            "`sexp` an S-expression."
        )
    ),
]
