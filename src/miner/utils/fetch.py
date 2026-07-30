"""Small, reusable web-page fetching helper."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from typing import TypedDict
from urllib.parse import urlparse

import httpx
from trafilatura import extract, extract_metadata, html2txt

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_CHARS = 50_000


class FetchError(RuntimeError):
    """Raised when a public page cannot be fetched or extracted."""


class FetchedPage(TypedDict):
    url: str
    title: str
    content: str


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FetchError("only absolute HTTP and HTTPS URLs are supported")

    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise FetchError("local URLs are not allowed")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise FetchError("private and non-public IP addresses are not allowed")


def _extract_html(html: str, url: str) -> tuple[str, str]:
    metadata = extract_metadata(html, default_url=url)
    content = extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_links=True,
    )
    return metadata.title or "", content or html2txt(html)


async def fetch_page(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> FetchedPage:
    """Fetch a public URL with HTTPX and extract HTML content with Trafilatura."""

    _validate_public_url(url)

    async def validate_redirect(request: httpx.Request) -> None:
        _validate_public_url(str(request.url))

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=10,
            timeout=httpx.Timeout(30, connect=5),
            event_hooks={"request": [validate_redirect]},
        ) as client:
            response = await client.get(
                url,
                headers={"Accept": "text/html, text/plain;q=0.9, application/json;q=0.8"},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FetchError(f"request failed: {exc}") from exc

    if len(response.content) > max_bytes:
        raise FetchError(f"response exceeds {max_bytes} bytes")

    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if media_type == "application/json":
        try:
            content = f"```json\n{json.dumps(response.json(), indent=2)}\n```"
        except ValueError:
            content = response.text
        title = ""
    elif not media_type or media_type in {"text/html", "application/xhtml+xml"}:
        title, content = await asyncio.to_thread(
            _extract_html,
            response.text,
            str(response.url),
        )
    elif media_type.startswith("text/") or media_type == "application/xml":
        title, content = "", response.text
    else:
        raise FetchError(f"unsupported content type {media_type!r}")

    content = content.strip()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[Content truncated]"
    return {"url": str(response.url), "title": title, "content": content}


__all__ = ["FetchError", "FetchedPage", "fetch_page"]
