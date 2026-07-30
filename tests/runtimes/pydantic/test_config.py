"""Tests for Pydantic runtime environment configuration."""

from __future__ import annotations

import os

from src.miner.runtimes.pydantic.config import configure_web_proxy


def test_standard_proxy_configures_local_web_search(monkeypatch):
    monkeypatch.delenv("DDGS_PROXY", raising=False)

    configure_web_proxy(
        http_proxy="http://127.0.0.1:8080",
        https_proxy="http://127.0.0.1:8443",
    )
    assert os.environ["DDGS_PROXY"] == "http://127.0.0.1:8443"

    monkeypatch.delenv("DDGS_PROXY")
    configure_web_proxy(
        http_proxy="http://127.0.0.1:8080",
        https_proxy=None,
    )
    assert os.environ["DDGS_PROXY"] == "http://127.0.0.1:8080"

    monkeypatch.setenv("DDGS_PROXY", "socks5://127.0.0.1:1080")
    configure_web_proxy(
        http_proxy="http://127.0.0.1:8080",
        https_proxy=None,
    )
    assert os.environ["DDGS_PROXY"] == "socks5://127.0.0.1:1080"
