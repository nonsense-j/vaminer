"""Tests for the minimal model workspace and external output layout."""

from pathlib import Path

from src.miner.utils.workspace import Workspace, safe_input_id


def test_workspace_keeps_only_src_and_cases_and_externalizes_outputs(tmp_path: Path):
    workspace_root = tmp_path / "vas_ws" / "miner" / "VAS-0001"
    (workspace_root / "cache").mkdir(parents=True)
    (workspace_root / "artifacts").mkdir()
    (workspace_root / "analysis.md").write_text("legacy\n", encoding="utf-8")

    workspace = Workspace.from_id(
        "VAS-0001",
        base_dir=tmp_path / "vas_ws" / "miner",
        input_id="CVE-2099-0001",
        output_root=tmp_path / "output",
        trace_id="0123456789abcdef0123456789abcdef",
    )

    assert {path.name for path in workspace.root.iterdir()} == {"src", "cases"}
    assert workspace.cache_dir == (
        tmp_path
        / "output"
        / "miner"
        / "VAS-0001"
        / "CVE-2099-0001"
        / "caches"
    )
    assert workspace.artifact_root == tmp_path / "output" / "artifacts"


def test_safe_input_id_preserves_cves_and_disambiguates_urls():
    assert safe_input_id("CVE-2099-0001") == "CVE-2099-0001"
    assert safe_input_id("https://example.test/issues/1").startswith(
        "https-example.test-issues-1--"
    )
