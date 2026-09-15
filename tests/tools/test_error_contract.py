"""Adversarial schema-valid arguments and failures that must stay fatal."""

import errno
import json
import shutil
import subprocess

import httpx
import pytest

from src.miner.tools import ast_grep, cases, cve, github, repo, skills, src
from src.miner.tools.errors import ToolInputError, ToolUnavailableError, tool_error_feedback
from src.miner.utils import fetch


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / "a.c").write_text("int f(int x) { return -x; }\n")
    (tmp_path / "case1.c").write_text("original\n")
    (tmp_path / "SKILL.md").write_text("instructions\n")
    return tmp_path


@pytest.mark.parametrize("value", ["\x00", "\ud800", "\udfff"])
@pytest.mark.parametrize("operation", ["source", "search", "glob", "skill", "case", "query", "language", "diff"])
def test_unrepresentable_text_is_input_feedback(evidence, value, operation):
    operations = {
        "source": lambda: src.read_src_file(evidence, value),
        "search": lambda: src.search_src_files(evidence, value),
        "glob": lambda: src.list_src_files(evidence, glob=value),
        "skill": lambda: skills.read_skill_resource({"test": evidence}, "test", value),
        "case": lambda: cases.write_case_artifact(evidence, "case1.c", value),
        "query": lambda: ast_grep.run_ast_grep(evidence, language="c", query_type="pattern", query=value),
        "language": lambda: ast_grep.run_ast_grep(evidence, language=value, query_type="pattern", query="x"),
        "diff": lambda: repo.read_patch_diff_from_repo(evidence, value),
    }
    with pytest.raises(ToolInputError):
        operations[operation]()
    assert (evidence / "case1.c").read_text() == "original\n"


@pytest.mark.parametrize("content", ["", " ", "x" * (cases.MAX_CASE_ARTIFACT_BYTES + 1)])
def test_rejected_case_write_keeps_existing_content(evidence, content):
    with pytest.raises(ToolInputError):
        cases.write_case_artifact(evidence, "case1.c", content)
    assert (evidence / "case1.c").read_text() == "original\n"
    assert not list(evidence.glob(".case-artifact-*"))


def test_case_filename_directory_is_correctable(evidence):
    (evidence / "case2.c").mkdir()
    with pytest.raises(ToolInputError, match="directory"):
        cases.write_case_artifact(evidence, "case2.c", "code")


def test_oversized_path_is_feedback_but_permission_failure_is_fatal(evidence):
    with pytest.raises(OSError) as error:
        src.read_src_file(evidence, "x" * 5000)
    assert "shorter" in tool_error_feedback(error.value)
    assert tool_error_feedback(PermissionError(errno.EACCES, "denied")) is None
    assert tool_error_feedback(FileNotFoundError("missing executable")) is None


def test_native_cli_argument_and_encoding_boundaries(evidence):
    assert "a.c:1:" in src.search_src_files(evidence, "-x")
    with pytest.raises(ToolInputError, match="single line"):
        src.search_src_files(evidence, "first\nsecond")
    with pytest.raises(ToolInputError, match="glob"):
        src.list_src_files(evidence, glob="[")
    with pytest.raises(ToolInputError, match="regex"):
        src.search_src_files(evidence, "[", mode="regex")
    (evidence / "bytes.c").write_bytes(b"int invalid_\xff;\n")
    assert "bytes.c:1:int invalid_" in src.search_src_files(evidence, "invalid_")
    if shutil.which("ast-grep") is None:
        pytest.skip("ast-grep is required")
    assert "matches: 1" in ast_grep.run_ast_grep(evidence, language="c", query_type="pattern", query="-x")
    with pytest.raises(ast_grep.AstGrepQueryError, match="not supported"):
        ast_grep.run_ast_grep(evidence, language="-invalid", query_type="pattern", query="x")


@pytest.mark.parametrize("payload", ["not JSON", "[]", '{"type":"match","data":{}}'])
def test_rg_protocol_errors_cannot_become_no_matches(evidence, monkeypatch, payload):
    monkeypatch.setattr(src.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, payload, ""))
    with pytest.raises(RuntimeError, match="rg returned"):
        src.search_src_files(evidence, "x")


def test_ast_missing_binary_and_malformed_match_are_fatal(evidence, monkeypatch):
    monkeypatch.setattr(ast_grep.shutil, "which", lambda name: None)
    with pytest.raises(ast_grep.AstGrepRunnerError, match="not found") as missing:
        ast_grep.run_ast_grep(evidence, language="c", query_type="pattern", query="x")
    assert tool_error_feedback(missing.value) is None
    monkeypatch.setattr(ast_grep.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, json.dumps([{"file": "a.c", "text": "x", "range": {}}]), "diagnostic\n",
    ))
    with pytest.raises(ast_grep.AstGrepRunnerError, match="coordinate") as malformed:
        ast_grep.run_ast_grep(evidence, language="c", query_type="pattern", query="x", executable="ast-grep")
    assert malformed.value.stderr == "diagnostic\n"
    assert tool_error_feedback(malformed.value) is None


@pytest.mark.parametrize(("operation", "args"), [
    (github.fetch_github_issue, ("https://github.com/o/r/issues/1/trailing",)),
    (github.fetch_github_issue, ("https://github.com/../r/issues/1",)),
    (github.parse_commit, ("https://github.com/o/r/commit/abc\x00",)),
    (github.parse_commit, ("https://github.com/\ud800/r/commit/abcdef1",)),
    (github.search_commit_by_tag, ("o", "r", "\ud800")),
    (github.search_commit_by_tag, ("../outside", "r", "v1")),
    (github.search_commit_by_time, ("o", "r", "2026-01-01", "2026-02-01")),
    (github.search_commit_by_time, ("o", "r", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")),
    (cve.fetch_cve, ("CVE-not-an-id",)),
])
def test_invalid_evidence_arguments_never_reach_network(monkeypatch, operation, args):
    def no_network(*args, **kwargs):
        pytest.fail("invalid arguments reached HTTP")
    monkeypatch.setattr(httpx, "Client", no_network)
    with pytest.raises(ToolInputError):
        operation(*args)


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), ValueError("bad JSON"), httpx.LocalProtocolError("bad header")])
def test_commit_lookup_does_not_fabricate_metadata(error):
    class Client:
        def get(self, *args, **kwargs):
            raise error
    with pytest.raises(type(error)):
        github._fetch_commit_info("o", "r", "abcdef1", client=Client())


@pytest.mark.parametrize("error", [ValueError("bug"), TypeError("bug"), httpx.LocalProtocolError("bad header")])
def test_cve_provider_bugs_are_not_not_found(monkeypatch, error):
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            raise error
        post = get
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: Client())
    with pytest.raises(type(error)):
        cve.fetch_cve("CVE-2026-1234")


def test_cve_unavailability_falls_back_and_preserves_commit_reference(monkeypatch):
    def offline(*args):
        raise httpx.ConnectError("offline")
    ref = "https://github.com/o/r/commit/abcdef1"
    monkeypatch.setattr(cve, "_fetch_from_nvd", offline)
    monkeypatch.setattr(cve, "_fetch_from_gh_advisory", offline)
    monkeypatch.setattr(cve, "_fetch_from_cve_search", lambda cve_id: ("description", [ref]))
    monkeypatch.setattr(cve, "_fetch_commit_info", offline)
    result = cve.fetch_cve("CVE-2026-1234")
    assert result.raw_desc == "description"
    assert result.references == [ref]
    assert result.commits == []
    monkeypatch.setattr(cve, "_fetch_from_cve_search", offline)
    with pytest.raises(ToolUnavailableError, match="lookup incomplete"):
        cve.fetch_cve("CVE-2026-1234")


@pytest.mark.parametrize("revision", ["", "main", "-abc123", "a" * 65, "abc\x00"])
def test_bad_revision_does_not_remove_checkout(tmp_path, revision):
    checkout = tmp_path / "src" / "o" / "r"
    checkout.mkdir(parents=True)
    marker = checkout / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(ToolInputError, match="commit SHA"):
        repo.clone_repository(tmp_path, "https://github.com/o/r", revision)
    assert marker.read_text() == "keep"


def test_html_without_metadata_is_readable(monkeypatch):
    monkeypatch.setattr(fetch, "extract_metadata", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "extract", lambda *a, **k: "text")
    assert fetch._extract_html("<p>text</p>", "https://example.org") == ("", "text")


@pytest.mark.parametrize("url", ["https://[broken", "https://example.org:wrong/", "https://example.org:99999/"])
def test_malformed_web_urls_are_feedback(url):
    with pytest.raises(fetch.FetchError):
        fetch._validate_public_url(url)
