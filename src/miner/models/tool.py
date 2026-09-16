"""Shared semantic types for runtime tool input schemas."""

from typing import Annotated, Literal

from pydantic import Field

from .anchors import AnchorPlanRequest


# list_src_files, search_src_files
SourceGlob = Annotated[
    str | None,
    Field(description="Ripgrep glob filter, such as `*.c` or `!vendor/**`."),
]

# list_src_files
SourceListPath = Annotated[
    str | None,
    Field(description="Existing source-root-relative directory; omit to list the whole root."),
]
SourceListLimit = Annotated[
    int,
    Field(ge=1, le=500, description="Caps returned paths; narrow `path` or `glob` if truncated."),
]

# search_src_files
SourceSearchPath = Annotated[
    str | None,
    Field(description="Existing source-root-relative file or directory; omit to search the whole root."),
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

# read_src_file
SourceFilePath = Annotated[str, Field(description="Source-root-relative file path.")]
ReadFullFile = Annotated[
    bool,
    Field(description="Return the whole file; incompatible with explicit line bounds."),
]

# read_src_file, read_case_artifact, read_skill_resource
ReadStartLine = Annotated[
    int,
    Field(ge=1, description="One-based first line; continue after the displayed end when truncated."),
]
ReadEndLine = Annotated[
    int | None,
    Field(description="Inclusive last line; omit to read at most 200 lines."),
]

# read_case_artifact, write_case_artifact
CaseArtifactPath = Annotated[
    str,
    Field(
        pattern=r"^case\d+(?:_var\d+)?\.[A-Za-z0-9]+$",
        description="Bare `caseN.ext` or `caseN_varM.ext` filename.",
    ),
]

# write_case_artifact
CaseArtifactContent = Annotated[str, Field(description="Non-empty Case Artifact content.")]

# fetch_cve
CVEId = Annotated[
    str,
    Field(
        pattern=r"^CVE-[0-9]{4}-[0-9]{4,}$",
        description="`CVE-YYYY-NNNN` identifier with at least four sequence digits.",
    ),
]

# fetch_github_issue
GitHubIssueUrl = Annotated[str, Field(description="Full GitHub issue URL.")]
FetchExtraNotes = Annotated[
    bool,
    Field(description="Include issue comments in `extra_notes` when their evidence is needed."),
]

# parse_commit
GitHubCommitUrl = Annotated[str, Field(description="Full GitHub commit URL.")]

# clone_repo
RepositoryUrl = Annotated[str, Field(description="Repository URL.")]
BuggyCommitSha = Annotated[
    str,
    Field(pattern=r"^[A-Fa-f0-9]{7,64}$", description="Commit fetched into `buggy` and checked out."),
]
FixedCommitSha = Annotated[
    str | None,
    Field(description="Commit fetched into `fixed` for diff inspection."),
]

# search_commit_by_tag, search_commit_by_time
GitHubOwner = Annotated[str, Field(description="GitHub repository owner without slashes.")]
GitHubRepository = Annotated[str, Field(description="GitHub repository name without slashes.")]

# search_commit_by_tag
GitTagPrefix = Annotated[
    str,
    Field(description="Narrowest evidence-supported tag prefix, such as `v2.7`."),
]

# search_commit_by_time
CommitRangeStart = Annotated[
    str,
    Field(description="Inclusive ISO timestamp with timezone, such as `2025-01-01T00:00:00Z`."),
]
CommitRangeEnd = Annotated[
    str,
    Field(description="Inclusive ISO timestamp with timezone; must not precede `since`."),
]

# read_patch_diff
PatchPath = Annotated[
    str | None,
    Field(description="Repository-relative file or directory for a full patch; omit for a diffstat."),
]

# synthesize_anchor_plan
AnchorPlanInput = Annotated[
    AnchorPlanRequest,
    Field(description="Complete desired Anchor set; reuse entries preserve prior results."),
]

# list_skill_resources
SkillResourceLimit = Annotated[
    int,
    Field(ge=1, le=100, description="Caps returned skill-root-relative paths."),
]

# read_skill_resource
SkillResourcePath = Annotated[str, Field(description="Skill-root-relative resource path.")]

# run_ast_grep_query
AstGrepTarget = Annotated[
    Literal["src", "cases"],
    Field(description="`src` searches analyzed source; `cases` searches generated Case Artifacts."),
]
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

# run_ast_grep_query, debug_ast_grep_pattern
AstGrepLanguage = Annotated[str, Field(description="ast-grep language identifier.")]

# debug_ast_grep_pattern
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
