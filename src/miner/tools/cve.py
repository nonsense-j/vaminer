"""CVE fetcher tools."""

import re
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

from ..models.issue import CommitRawInfo, IssueRawInfo
from ..models.tool import CVEId
from .errors import ToolInputError, ToolUnavailableError
from .github import _fetch_commit_info, _headers


def fetch_cve(cve_id: CVEId) -> IssueRawInfo:
    """Fetch a CVE description, references, repository, and linked commits."""
    if re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", cve_id) is None:
        raise ToolInputError("cve_id must match CVE-YYYY-NNNN (at least four digits after the year)")
    failures: list[str] = []

    def fetch(provider: Callable[[str], tuple[str, list[str]] | None]) -> tuple[str, list[str]] | None:
        try:
            return provider(cve_id)
        except httpx.LocalProtocolError:
            raise
        except (httpx.HTTPError, ToolUnavailableError) as exc:
            failures.append(f"{provider.__name__}: {exc}")
            return None

    nvd_data = fetch(_fetch_from_nvd)
    gh_data = fetch(_fetch_from_gh_advisory)

    description: str | None = None
    all_refs: set[str] = set()

    if gh_data:
        description = gh_data[0]
        all_refs.update(gh_data[1])
    if nvd_data:
        description = description or nvd_data[0]
        all_refs.update(nvd_data[1])
    if not description:
        cve_search = fetch(_fetch_from_cve_search)
        if cve_search:
            description = cve_search[0]
            all_refs.update(cve_search[1])

    if not description:
        if failures:
            raise ToolUnavailableError("CVE lookup incomplete: " + "; ".join(failures))
        return IssueRawInfo(raw_desc=f"CVE not found: {cve_id}", commits=[], references=[])

    commits: list[CommitRawInfo] = []
    final_refs: list[str] = []
    repo_url: str | None = None

    for ref in all_refs:
        parsed = _parse_github_reference_url(ref)
        if parsed:
            owner, repo, action, ref_id = parsed
            if repo_url is None:
                repo_url = f"https://github.com/{owner}/{repo}"
            if action == "commit":
                try:
                    commits.append(_fetch_commit_info(owner, repo, ref_id))
                except httpx.LocalProtocolError:
                    raise
                except httpx.HTTPError:
                    # Preserve the reference when optional commit enrichment is unavailable.
                    final_refs.append(ref)
            else:
                final_refs.append(ref)
        else:
            final_refs.append(ref)

    commits.sort(key=lambda c: c.timestamp)
    return IssueRawInfo(
        raw_desc=description,
        repo_url=repo_url,
        commits=commits,
        references=final_refs,
    )


def _fetch_from_nvd(cve_id: str) -> tuple[str, list[str]] | None:
    url = f"https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cve_id}"
    with httpx.Client(timeout=30) as client:
        data = client.get(url).raise_for_status().json()
    vulns = data["vulnerabilities"]
    if not vulns:
        return None
    cve_data = vulns[0].get("cve", {})
    desc = next(
        (d["value"] for d in cve_data.get("descriptions", []) if d["lang"] == "en"),
        "",
    )
    refs = [r["url"] for r in cve_data.get("references", [])]
    return desc, refs


def _parse_github_reference_url(url: str) -> tuple[str, str, str, str] | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4:
        return None

    owner, repo, action, ref_id = parts
    if action not in {"pull", "commit", "issue", "issues"}:
        return None
    if not owner or not repo or not ref_id:
        return None
    if action == "commit" and not all(ch in "0123456789abcdefABCDEF" for ch in ref_id):
        return None

    return owner, repo, action, ref_id


def _fetch_from_gh_advisory(cve_id: str) -> tuple[str, list[str]] | None:
    query = """
    query($cve_id: String!) {
      securityAdvisories(identifier: {type: CVE, value: $cve_id}, first: 1) {
        nodes { description references { url } }
      }
    }
    """
    with httpx.Client(timeout=30) as client:
        data = (
            client.post(
                "https://api.github.com/graphql",
                json={"query": query, "variables": {"cve_id": cve_id}},
                headers=_headers(),
            )
            .raise_for_status()
            .json()
        )
    if data.get("errors"):
        raise ToolUnavailableError(f"GitHub advisory GraphQL errors: {data['errors']}")
    nodes = data["data"]["securityAdvisories"]["nodes"]
    if not nodes or not nodes[0]:
        return None
    advisory = nodes[0]
    return advisory.get("description", ""), [r["url"] for r in advisory.get("references", [])]


def _fetch_from_cve_search(cve_id: str) -> tuple[str, list[str]] | None:
    with httpx.Client(timeout=30) as client:
        data = client.get(f"https://cve.circl.lu/api/cve/{cve_id}").raise_for_status().json()
    if not data:
        return None
    items = [item for val in data["containers"].values() for item in (val if isinstance(val, list) else [val])]
    desc, refs = "", set()
    for item in items:
        d = next(
            (d["value"] for d in item.get("descriptions", []) if d["lang"] == "en"),
            "",
        )
        desc = d if d else desc
        refs.update(r["url"] for r in item.get("references", []))
    return desc, list(refs)
