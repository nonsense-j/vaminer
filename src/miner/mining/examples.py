"""Deterministic, recoverable intake for local Example suite workflows."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..utils.workspace import Workspace

_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hh",
        ".hpp",
        ".hxx",
        ".cs",
        ".go",
        ".java",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".kt",
        ".kts",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".swift",
        ".tsx",
        ".ts",
    }
)
_MANIFEST_NAMES = ("manifest.json", "manifest.yaml", "manifest.yml")


def example_suite_exp_id(basename: str) -> str:
    """Return the canonical Example Suite id derived from its directory name."""

    return basename


class ExampleSuiteInspection(BaseModel):
    """Observed metadata over an input directory before workspace materialization."""

    model_config = ConfigDict(extra="forbid")

    exp_id: str = Field(
        ...,
        description="Canonical Example Suite id derived from the directory name",
    )
    source_path: str
    content_digest: str
    file_paths: list[str]
    manifest_path: str | None = None


class ExampleSuiteIntake(ExampleSuiteInspection):
    """Workspace-local snapshot metadata used by agent phases."""

    snapshot_path: str
    snapshot_ref: str


def _inspect_example_suite(root: Path, *, exp_id: str, source_path: Path) -> ExampleSuiteInspection:
    """Inspect a resolved directory while retaining its canonical Example Suite id."""

    paths: list[Path] = []
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        if path.is_symlink():
            raise ValueError(f"example suite must not contain symbolic links: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"example suite contains an unsupported filesystem entry: {path}")
        relative = path.resolve().relative_to(root)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"example suite path is not a safe relative file path: {path}")
        paths.append(path)

    if not paths:
        raise ValueError("example suite does not contain any regular files")
    has_source = False
    digest = hashlib.sha256()
    file_paths: list[str] = []
    for path in paths:
        relative = path.resolve().relative_to(root).as_posix()
        size = path.stat().st_size
        is_source = path.suffix.lower() in _SOURCE_SUFFIXES
        if is_source:
            has_source = True
        file_paths.append(relative)
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")

    if not has_source:
        raise ValueError("example suite does not contain a recognizable source code file")
    return ExampleSuiteInspection(
        exp_id=exp_id,
        source_path=source_path.as_posix(),
        content_digest=digest.hexdigest(),
        file_paths=file_paths,
        manifest_path=next((path for path in _MANIFEST_NAMES if (root / path).is_file()), None),
    )


def inspect_example_suite(example_suite: Path) -> ExampleSuiteInspection:
    """Inspect an example suite and compute a stable path-and-content digest."""

    raw_root = example_suite.expanduser()
    if raw_root.is_symlink():
        raise ValueError(f"example suite must not be a symbolic link: {example_suite}")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValueError(f"example suite is not an existing directory: {example_suite}")
    if not root.name:
        raise ValueError(f"example suite must have a stable basename: {example_suite}")
    return _inspect_example_suite(
        root,
        exp_id=example_suite_exp_id(root.name),
        source_path=root,
    )


def _same_snapshot(
    inspection: ExampleSuiteInspection,
    snapshot: Path,
) -> bool:
    try:
        copied = _inspect_example_suite(
            snapshot.resolve(),
            exp_id=inspection.exp_id,
            source_path=Path(inspection.source_path),
        )
    except (OSError, ValueError):
        return False
    return copied == inspection


def materialize_example_suite(
    inspection: ExampleSuiteInspection,
    *,
    workspace: Workspace,
) -> ExampleSuiteIntake:
    """Stage, verify, and atomically publish one immutable suite snapshot."""

    source = Path(inspection.source_path)
    current = inspect_example_suite(source)
    if current != inspection:
        raise ValueError("example suite changed before snapshot materialization")

    snapshot = workspace.example_suite_snapshot_dir
    if not _same_snapshot(inspection, snapshot):
        staging_parent = Path(
            tempfile.mkdtemp(prefix=".example-suite-staging-", dir=workspace.root)
        )
        staging_snapshot = staging_parent / "snapshot"
        backup: Path | None = None
        try:
            shutil.copytree(source, staging_snapshot, symlinks=False)
            if not _same_snapshot(inspection, staging_snapshot):
                raise ValueError("example suite changed while its staged snapshot was copied")
            if snapshot.exists():
                backup = workspace.root / f".example-suite-replaced-{uuid.uuid4().hex}"
                os.replace(snapshot, backup)
            os.replace(staging_snapshot, snapshot)
            if not _same_snapshot(inspection, snapshot):
                raise ValueError("published example-suite snapshot failed reinspection")
        except Exception:
            if backup is not None and backup.exists() and not snapshot.exists():
                os.replace(backup, snapshot)
            raise
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)

    intake = ExampleSuiteIntake(
        **inspection.model_dump(mode="json"),
        snapshot_path=snapshot.resolve().as_posix(),
        snapshot_ref=snapshot.relative_to(workspace.root).as_posix(),
    )
    return intake


__all__ = [
    "ExampleSuiteInspection",
    "ExampleSuiteIntake",
    "example_suite_exp_id",
    "inspect_example_suite",
    "materialize_example_suite",
]
