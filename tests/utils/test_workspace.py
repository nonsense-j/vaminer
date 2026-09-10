"""Tests for the minimal model workspace and external output layout."""

from pathlib import Path

from src.miner.utils.workspace import Workspace, compute_source_sha


def test_workspace_keeps_only_src_and_cases_and_externalizes_cache(tmp_path: Path):
    workspace_root = tmp_path / "vas_ws" / "miner" / "VAS-0001"
    (workspace_root / "cache").mkdir(parents=True)
    (workspace_root / "artifacts").mkdir()
    (workspace_root / "analysis.md").write_text("legacy\n", encoding="utf-8")

    workspace = Workspace.from_id(
        "VAS-0001",
        base_dir=tmp_path / "vas_ws" / "miner",
        source_sha=compute_source_sha("issue", "CVE-2099-0001"),
        output_root=tmp_path / "output",
        trace_id="0123456789abcdef0123456789abcdef",
    )

    assert {path.name for path in workspace.root.iterdir()} == {"src", "cases"}
    assert workspace.cache_dir == (
        tmp_path
        / "output"
        / "miner"
        / "VAS-0001"
        / compute_source_sha("issue", "CVE-2099-0001")
        / "caches"
    )
    assert workspace.log_dir == (
        tmp_path
        / "output"
        / "miner"
        / "VAS-0001"
        / compute_source_sha("issue", "CVE-2099-0001")
        / "logs"
    )
    assert not hasattr(workspace, "artifact_root")


def test_source_sha_is_typed_and_12_hex_characters():
    issue = compute_source_sha("issue", "CVE-2099-0001")
    example = compute_source_sha("example_suite", "CVE-2099-0001")
    assert len(issue) == len(example) == 12
    assert issue != example
    assert all(character in "0123456789abcdef" for character in issue + example)
