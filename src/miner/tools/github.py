"""GitHub fetcher tools."""

import re
from urllib.parse import quote

import httpx

from ..configs import GITHUB_TOKEN
from ..utils.models import CommitRawInfo, IssueRawInfo


def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _is_github_commit_url(url: str) -> bool:
    return bool(re.match(r"https?://github\.com/[^/]+/[^/]+/commit/[a-f0-9]+", url))


def _parse_commit_url(url: str) -> tuple[str, str, str] | None:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/commit/([a-f0-9]+)", url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _fetch_commit_info(
    owner: str,
    repo: str,
    revision: str,
    *,
    client: httpx.Client | None = None,
) -> CommitRawInfo:
    owned_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        encoded_revision = quote(revision, safe="")
        data = (
            client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{encoded_revision}",
                headers=_headers(),
            )
            .raise_for_status()
            .json()
        )
        parent_sha = data["parents"][0]["sha"] if data.get("parents") else ""
        return CommitRawInfo(
            commit_url=f"https://github.com/{owner}/{repo}/commit/{data['sha']}",
            cur_sha=data["sha"],
            parent_sha=parent_sha,
            timestamp=data["commit"]["committer"]["date"][:16],
            msg=data["commit"]["message"],
        )
    except Exception as exc:  # noqa: BLE001 - external failures are returned to the agent.
        return CommitRawInfo(commit_url=None, cur_sha=revision, parent_sha="", timestamp="", msg=f"Error: {exc}")
    finally:
        if owned_client:
            client.close()


def fetch_github_issue(issue_url: str, fetch_extra_notes: bool = False) -> IssueRawInfo:
    """Fetch a GitHub issue details. Result includes issue title, body, linked commits, and reference URLs.

    Args:
        issue_url: GitHub issue URL (e.g., https://github.com/owner/repo/issues/123)
        fetch_extra_notes: Whether to fetch issue comments as extra notes.
    """
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)", issue_url)
    if not m:
        return IssueRawInfo(raw_desc=f"Invalid issue URL: {issue_url}")

    owner, repo, issue_number = m.group(1), m.group(2), int(m.group(3))
    with httpx.Client(timeout=30) as client:
        issue = (
            client.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
                headers=_headers(),
            )
            .raise_for_status()
            .json()
        )

        raw_desc = f"{issue['title']}\n\n{issue.get('body') or ''}"
        timestamp = issue.get("created_at", "")[:16] or None

        extra_notes = None
        if fetch_extra_notes and issue.get("comments_url"):
            resp = client.get(issue["comments_url"], headers=_headers())
            if resp.status_code == 200:
                extra_notes = "\n---\n".join(c["body"] for c in resp.json())

        # Search commits mentioning this issue
        search_resp = client.get(
            f"https://api.github.com/search/commits?q=repo:{owner}/{repo}+{issue_number}",
            headers=_headers(),
        )
        commits: list[CommitRawInfo] = []
        if search_resp.status_code == 200:
            for item in search_resp.json().get("items", []):
                parent_sha = item["parents"][0]["sha"] if item.get("parents") else ""
                commits.append(
                    CommitRawInfo(
                        commit_url=item.get("html_url", f"https://github.com/{owner}/{repo}/commit/{item['sha']}"),
                        cur_sha=item["sha"],
                        parent_sha=parent_sha,
                        timestamp=item["commit"]["committer"]["date"][:16],
                        msg=item["commit"]["message"],
                    )
                )
        commits.sort(key=lambda c: c.timestamp)

        references = [
            r for r in re.findall(r"https?://\S+", raw_desc) if not _is_github_commit_url(r) and r != issue_url
        ]

    return IssueRawInfo(
        raw_desc=raw_desc,
        repo_url=f"https://github.com/{owner}/{repo}",
        timestamp=timestamp,
        extra_notes=extra_notes,
        commits=commits,
        references=references,
    )


def parse_commit(commit_url: str) -> CommitRawInfo:
    """Parse a GitHub commit URL and fetch commit metadata (SHA, parent, timestamp, message).

    Args:
        commit_url: Full GitHub commit URL (e.g., https://github.com/owner/repo/commit/abc123)
    """
    parsed = _parse_commit_url(commit_url)
    if not parsed:
        return CommitRawInfo(commit_url=commit_url, cur_sha="", parent_sha="", timestamp="", msg="Invalid URL")
    return _fetch_commit_info(*parsed)


def search_commit_by_tag(owner: str, repo: str, tag_prefix: str) -> list[CommitRawInfo] | str:
    """Last-resort search for commits by a non-empty tag prefix.

    Args:
        owner: Repository owner
        repo: Repository name
        tag_prefix: Tag prefix to search (e.g., 'v2.7')
    """
    tag_prefix = tag_prefix.strip()
    if not tag_prefix:
        return "tag_prefix must be non-empty; provide the narrowest evidence-supported prefix."

    encoded_ref = quote(f"tags/{tag_prefix}", safe="/")
    with httpx.Client(timeout=30) as client:
        references = (
            client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/matching-refs/{encoded_ref}",
                headers=_headers(),
            )
            .raise_for_status()
            .json()
        )
        tag_names = [
            item["ref"].removeprefix("refs/tags/")
            for item in references
            if isinstance(item, dict)
            and isinstance(item.get("ref"), str)
            and item["ref"].startswith("refs/tags/")
            and item["ref"].removeprefix("refs/tags/").startswith(tag_prefix)
        ]
        commits = [_fetch_commit_info(owner, repo, tag_name, client=client) for tag_name in tag_names]
    commits = [commit for commit in commits if commit.commit_url is not None]

    if not commits:
        return f"No tags found matching prefix '{tag_prefix}' in {owner}/{repo}."

    commits.sort(key=lambda c: c.timestamp)
    return commits


def search_commit_by_time(owner: str, repo: str, since: str, until: str) -> list[CommitRawInfo] | str:
    """Last-resort search for commits within a narrow time range.

    Args:
        owner: Repository owner
        repo: Repository name
        since: Start time (ISO format: YYYY-MM-DDTHH:MM:SSZ)
        until: End time (ISO format: YYYY-MM-DDTHH:MM:SSZ)
    """
    with httpx.Client(timeout=30) as client:
        data = (
            client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                params={"since": since, "until": until, "per_page": 100},
                headers=_headers(),
            )
            .raise_for_status()
            .json()
        )

    if not data:
        return f"No commits found in {owner}/{repo} between {since} and {until}"
    if len(data) >= 100:
        return (
            f"At least 100 commits exist in {owner}/{repo} between {since} and {until}; "
            "narrow the time range before selecting a revision."
        )

    commits = [
        CommitRawInfo(
            commit_url=f"https://github.com/{owner}/{repo}/commit/{item['sha']}",
            cur_sha=item["sha"],
            parent_sha=item["parents"][0]["sha"] if item.get("parents") else "",
            timestamp=item["commit"]["committer"]["date"][:16],
            msg=item["commit"]["message"],
        )
        for item in data
    ]
    commits.sort(key=lambda c: c.timestamp)
    return commits
