"""Tests for the generic web-page fetcher."""

from __future__ import annotations

import httpx
import pytest

from src.miner.utils.fetch import FetchError, fetch_page


async def test_fetch_page_uses_httpx_and_extracts_html(
    monkeypatch: pytest.MonkeyPatch,
):
    observed: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, **kwargs):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"""
                    <html>
                      <head><title>Security advisory</title></head>
                      <body><main><h1>CVE-2021-46143</h1><p>Relevant evidence.</p></main></body>
                    </html>
                """,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("src.miner.utils.fetch.httpx.AsyncClient", Client)

    result = await fetch_page("https://feedly.com/cve/CVE-2021-46143")

    assert observed["follow_redirects"] is True
    assert "proxy" not in observed
    assert result["title"] == "CVE-2021-46143"
    assert "Relevant evidence." in result["content"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/private",
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
    ],
)
async def test_fetch_page_rejects_non_public_urls(url: str):
    with pytest.raises(FetchError):
        await fetch_page(url)
