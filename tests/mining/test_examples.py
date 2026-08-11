"""Tests for deterministic, recoverable example-suite intake."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.miner.mining.examples import (
    inspect_example_suite,
    materialize_example_suite,
)
from src.miner.utils.workspace import Workspace


def test_example_suite_intake_detects_language_and_materializes_snapshot(tmp_path: Path):
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

    assert inspection.registry_key == "example-suite:CWE-2099-fixture"
    assert inspection.language == "c"
    assert inspection.source_files == ["bad.c"]
    assert inspection.manifest_path == "manifest.json"
    assert Path(intake.snapshot_path, "bad.c").read_text(encoding="utf-8") == "void f(void) { danger(1); }\n"
    assert intake.snapshot_ref == "src/input_snapshot"
    assert {path.name for path in workspace.root.iterdir()} == {"src", "cases"}


def test_example_suite_intake_rejects_symlinks(tmp_path: Path):
    symlinked = tmp_path / "CWE-symlink"
    symlinked.mkdir()
    (tmp_path / "outside.c").write_text("int value;\n", encoding="utf-8")
    (symlinked / "bad.c").symlink_to(tmp_path / "outside.c")

    with pytest.raises(ValueError, match="symbolic links"):
        inspect_example_suite(symlinked)
