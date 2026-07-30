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


def test_example_suite_registry_rejects_same_name_digest_mismatch(tmp_path: Path):
    source = tmp_path / "CWE-2099-fixture"
    source.mkdir()
    (source / "bad.c").write_text("void f(void) { danger(1); }\n", encoding="utf-8")
    first = inspect_example_suite(source)

    base_dir = tmp_path / "ws"
    vas_id = Workspace.prepare_example_suite_vas_id(
        first.registry_key,
        content_digest=first.content_digest,
        base_dir=base_dir,
    )
    workspace = Workspace.from_id(vas_id, base_dir=base_dir)
    materialize_example_suite(first, workspace=workspace)
    Workspace.register_example_suite(
        first.registry_key,
        vas_id=vas_id,
        content_digest=first.content_digest,
        base_dir=base_dir,
    )
    assert vas_id == "VAS-0001"

    (source / "bad.c").write_text("void f(void) { danger(2); }\n", encoding="utf-8")
    changed = inspect_example_suite(source)
    with pytest.raises(ValueError, match="different digest"):
        Workspace.prepare_example_suite_vas_id(
            changed.registry_key,
            content_digest=changed.content_digest,
            base_dir=tmp_path / "ws",
        )


def test_example_suite_intake_rejects_symlinks_and_mixed_languages(tmp_path: Path):
    symlinked = tmp_path / "CWE-symlink"
    symlinked.mkdir()
    (tmp_path / "outside.c").write_text("int value;\n", encoding="utf-8")
    (symlinked / "bad.c").symlink_to(tmp_path / "outside.c")

    with pytest.raises(ValueError, match="symbolic links"):
        inspect_example_suite(symlinked)

    mixed = tmp_path / "CWE-mixed"
    mixed.mkdir()
    (mixed / "bad.c").write_text("int value;\n", encoding="utf-8")
    (mixed / "bad.py").write_text("danger()\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one supported source language"):
        inspect_example_suite(mixed)


def test_registered_suite_recovers_a_partial_snapshot_on_reuse(tmp_path: Path):
    source = tmp_path / "suite"
    source.mkdir()
    (source / "bad.c").write_text("void f(void) { danger(1); }\n", encoding="utf-8")
    inspection = inspect_example_suite(source)
    base_dir = tmp_path / "ws"
    vas_id = Workspace.prepare_example_suite_vas_id(
        inspection.registry_key,
        content_digest=inspection.content_digest,
        base_dir=base_dir,
    )
    workspace = Workspace.from_id(vas_id, base_dir=base_dir)
    materialize_example_suite(inspection, workspace=workspace)
    Workspace.register_example_suite(
        inspection.registry_key,
        vas_id=vas_id,
        content_digest=inspection.content_digest,
        base_dir=base_dir,
    )
    (workspace.example_suite_snapshot_dir / "bad.c").write_text("partial", encoding="utf-8")

    reused_id = Workspace.prepare_example_suite_vas_id(
        inspection.registry_key,
        content_digest=inspection.content_digest,
        base_dir=base_dir,
    )
    recovered = materialize_example_suite(
        inspection,
        workspace=Workspace.from_id(reused_id, base_dir=base_dir),
    )

    assert Path(recovered.snapshot_path, "bad.c").read_text(encoding="utf-8").startswith("void f")
