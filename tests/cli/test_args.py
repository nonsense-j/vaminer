"""CLI argument parsing tests."""

from __future__ import annotations

import pytest

from src.miner.main import parse_args


def test_cli_normalizes_issue_and_case_inputs():
    assert parse_args(["CVE-1,CVE-2", " https://example.test/issue "]).issue_input == [
        "CVE-1",
        "CVE-2",
        "https://example.test/issue",
    ]

    suite_args = parse_args(
        [
            "--example-suite",
            "/tmp/examples",
            "--runtime",
            "claude-code",
            "--claude-model",
            "claude-sonnet-4-6",
        ]
    )
    assert suite_args.example_suite.as_posix() == "/tmp/examples"
    assert suite_args.issue_input == []
    assert suite_args.runtime == "claude-code"
    assert suite_args.claude_model == "claude-sonnet-4-6"


def test_cli_rejects_conflicting_or_empty_inputs():
    with pytest.raises(SystemExit):
        parse_args(["--example-suite", "/tmp/examples", "CVE-2099-0001"])

    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args([","])
