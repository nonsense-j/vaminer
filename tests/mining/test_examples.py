"""Tests for deterministic, recoverable example-suite intake."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.miner.mining.examples import (
    inspect_example_suite,
    materialize_example_suite,
)
from src.miner.tools.src import list_src_files, read_src_file, search_src_files
from src.miner.utils.workspace import Workspace


def test_example_suite_intake_materializes_verified_snapshot(tmp_path: Path):
    source = tmp_path / "CWE-2099-fixture"
    source.mkdir()
    (source / "bad.c").write_text("void f(void) { danger(1); }\n", encoding="utf-8")
    (source / "manifest.json").write_text('{"note": "untrusted"}\n', encoding="utf-8")
    workspace = Workspace(
        tmp_path / "ws" / "VAS-0001",
        "VAS-0001",
        output_root=tmp_path / "output",
    )
    Workspace._ensure_structure(workspace.root)

    inspection = inspect_example_suite(source)
    intake = materialize_example_suite(inspection, workspace=workspace)

    assert inspection.exp_id == "CWE-2099-fixture"
    assert inspection.file_paths == ["bad.c", "manifest.json"]
    assert inspection.manifest_path == "manifest.json"
    assert Path(intake.snapshot_path, "bad.c").read_text(encoding="utf-8") == "void f(void) { danger(1); }\n"
    assert intake.snapshot_ref == "src/input_snapshot"
    assert {path.name for path in workspace.root.iterdir()} == {"src", "cases"}

    src_root = Path(intake.snapshot_path)
    assert list_src_files(src_root) == "bad.c\nmanifest.json"
    assert "bad.c:1:void f(void) { danger(1); }" in search_src_files(src_root, "danger", path="bad.c")
    assert read_src_file(src_root, "bad.c", end_line=1) == (
        "==> bad.c | lines 1-1 of 1 <==\nvoid f(void) { danger(1); }"
    )
    with pytest.raises(ValueError, match="repeats the bound Src Root"):
        list_src_files(src_root, path="src/input_snapshot")


def test_example_suite_accepts_nested_many_and_mixed_language_files(tmp_path: Path):
    source = tmp_path / "CVE-2099-0001"
    nested = source / "variants" / "deep"
    nested.mkdir(parents=True)
    (source / "bad.c").write_text("danger();\n", encoding="utf-8")
    (nested / "bad.py").write_text("danger()\n", encoding="utf-8")
    for index in range(205):
        (nested / f"case-{index}.txt").write_text("context\n", encoding="utf-8")

    inspection = inspect_example_suite(source)

    assert inspection.exp_id == "CVE-2099-0001"
    assert len(inspection.file_paths) == 207


def test_example_suite_requires_a_nonempty_directory_with_source_code(tmp_path: Path):
    empty = tmp_path / "CVE-empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="does not contain any regular files"):
        inspect_example_suite(empty)

    without_source = tmp_path / "CVE-without-source"
    without_source.mkdir()
    (without_source / "README.md").write_text("description\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not contain a recognizable source code file"):
        inspect_example_suite(without_source)
