"""Issue evidence and verified checkout data."""

from pydantic import BaseModel, ConfigDict, Field


class CommitRawInfo(BaseModel):
    """Commit information for locating buggy and fixed revisions."""

    commit_url: str | None = Field(None, description="GitHub commit URL")
    cur_sha: str = Field(..., description="The fixed commit SHA (current, partial)")
    parent_sha: str = Field(..., description="The parent SHA (partial) of the fixed commit (buggy)")
    timestamp: str = Field(..., description="Commit timestamp (YYYY-MM-DDTHH:MM)")
    msg: str = Field(..., description="Commit message")


class IssueRawInfo(BaseModel):
    """Normalized issue information from any evidence source."""

    raw_desc: str = Field(..., description="Direct description of the issue")
    repo_url: str | None = Field(None, description="Repository URL derived from issue/CVE")
    timestamp: str | None = Field(None, description="Issue creation timestamp (YYYY-MM-DDTHH:MM)")
    extra_notes: str | None = Field(None, description="Additional information such as discussions")
    commits: list[CommitRawInfo] = Field(default_factory=list, description="Related commits")
    references: list[str] = Field(default_factory=list, description="Other related URLs")


class RepoCheckout(BaseModel):
    """Result of preparing a repository checkout."""

    repo_path: str
    buggy_branch: str = "buggy"
    fixed_branch: str | None = "fixed"


class IssueCollectionInfo(BaseModel):
    """Resolved issue evidence and verified checkout metadata."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., description="CVE ID or GitHub issue URL")
    issue_summary: str = Field(..., description="Clear repository-independent issue summary")
    issue_details: str = Field(
        ...,
        description="Evidence-rich issue description retaining concrete code-pattern clues",
    )
    repo_url: str = Field(..., description="Repository URL")
    repo_path: str = Field(..., description="Local path of the verified checkout")
    buggy_commit: str = Field(..., description="Commit SHA (partial) currently checked out on the buggy branch")
    fixed_commit: str | None = Field(
        None,
        description="Evidence-supported fixing commit SHA (partial), or null when unavailable",
    )
