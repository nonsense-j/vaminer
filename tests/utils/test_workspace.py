"""Tests for the minimal model workspace and external output layout."""

import json
from pathlib import Path

import pytest

from src.miner.utils.workspace import SourceRegistry, Workspace, compute_source_sha


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


def test_source_registry_keeps_issue_and_example_ids_typed(tmp_path: Path):
    registry = SourceRegistry(tmp_path)
    digest = "a" * 64

    registry.register("VAS-0001", "issue", "CVE-2099-0001")
    registry.register(
        "VAS-0002",
        "example_suite",
        "CVE-2099-0001",
        content_digest=digest,
    )

    assert registry.lookup("issue", "CVE-2099-0001") == "VAS-0001"
    assert (
        registry.lookup(
            "example_suite",
            "CVE-2099-0001",
            content_digest=digest,
        )
        == "VAS-0002"
    )
    assert json.loads((tmp_path / "source_registry.json").read_text(encoding="utf-8")) == {
        "VAS-0001": [{"type": "issue", "issue_id": "CVE-2099-0001"}],
        "VAS-0002": [
            {
                "type": "example_suite",
                "exp_id": "CVE-2099-0001",
                "content_digest": digest,
            }
        ],
    }

    with pytest.raises(ValueError, match="different digest"):
        registry.lookup(
            "example_suite",
            "CVE-2099-0001",
            content_digest="b" * 64,
        )


def test_example_suite_exp_id_uses_one_path_form_for_vas_mapping(tmp_path: Path):
    registry = SourceRegistry(tmp_path)
    digest = "a" * 64
    registry.register(
        "VAS-0001",
        "example_suite",
        r"group\CWE-2099",
        content_digest=digest,
    )

    assert Workspace.prepare_example_suite_vas_id(
        "group/CWE-2099",
        content_digest=digest,
        base_dir=tmp_path,
    ) == "VAS-0001"
    assert json.loads((tmp_path / "source_registry.json").read_text(encoding="utf-8")) == {
        "VAS-0001": [
            {
                "type": "example_suite",
                "exp_id": "group/CWE-2099",
                "content_digest": digest,
            }
        ]
    }


def test_register_example_suite_migrates_a_legacy_windows_exp_id(tmp_path: Path):
    digest = "a" * 64
    registry_path = tmp_path / "source_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "VAS-0001": [
                    {
                        "type": "example_suite",
                        "exp_id": r"group\CWE-2099",
                        "content_digest": digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = SourceRegistry(tmp_path)

    assert registry.lookup(
        "example_suite",
        "group/CWE-2099",
        content_digest=digest,
    ) == "VAS-0001"
    registry.register(
        "VAS-0001",
        "example_suite",
        "group/CWE-2099",
        content_digest=digest,
    )

    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["VAS-0001"][0]["exp_id"] == "group/CWE-2099"
