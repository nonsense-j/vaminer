"""Deterministic, recoverable intake for local example-suite workflows."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..utils.models import AstGrepLanguage, ExampleSuiteFileMetadata
from ..utils.workspace import Workspace, atomic_write_json

MAX_EXAMPLE_SUITE_FILES = 200
MAX_EXAMPLE_SUITE_FILE_BYTES = 512 * 1024
MAX_EXAMPLE_SUITE_TOTAL_BYTES = 8 * 1024 * 1024

_LANGUAGE_SUFFIXES: dict[AstGrepLanguage, set[str]] = {
    AstGrepLanguage.C: {".c"},
    AstGrepLanguage.CPP: {".cc", ".cpp", ".cxx"},
    AstGrepLanguage.CSHARP: {".cs"},
    AstGrepLanguage.GO: {".go"},
    AstGrepLanguage.JAVA: {".java"},
    AstGrepLanguage.JAVASCRIPT: {".js", ".mjs", ".cjs"},
    AstGrepLanguage.JSX: {".jsx"},
    AstGrepLanguage.KOTLIN: {".kt", ".kts"},
    AstGrepLanguage.PHP: {".php"},
    AstGrepLanguage.PYTHON: {".py"},
    AstGrepLanguage.RUBY: {".rb"},
    AstGrepLanguage.RUST: {".rs"},
    AstGrepLanguage.SCALA: {".scala"},
    AstGrepLanguage.SWIFT: {".swift"},
    AstGrepLanguage.TSX: {".tsx"},
    AstGrepLanguage.TYPESCRIPT: {".ts"},
}
_SUFFIX_LANGUAGE = {
    suffix: language
    for language, suffixes in _LANGUAGE_SUFFIXES.items()
    for suffix in suffixes
}
_MANIFEST_NAMES = ("manifest.json", "manifest.yaml", "manifest.yml")


class ExampleSuiteInspection(BaseModel):
    """Validated metadata over an input directory before workspace materialization."""

    model_config = ConfigDict(extra="forbid")

    registry_key: str = Field(..., description="Stable registry key derived from the directory name")
    suite_name: str
    source_path: str
    content_digest: str
    language: AstGrepLanguage
    file_count: int
    total_bytes: int
    source_files: list[str]
    files: list[ExampleSuiteFileMetadata]
    manifest_path: str | None = None


class ExampleSuiteIntake(ExampleSuiteInspection):
    """Workspace-local snapshot metadata used by agent phases."""

    snapshot_path: str
    snapshot_ref: str


def _inspect_example_suite(root: Path, *, suite_name: str, source_path: Path) -> ExampleSuiteInspection:
    """Inspect a resolved directory while retaining the original suite identity."""

    paths: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
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
    if len(paths) > MAX_EXAMPLE_SUITE_FILES:
        raise ValueError(
            f"example suite contains too many files: {len(paths)} > {MAX_EXAMPLE_SUITE_FILES}"
        )

    total_bytes = 0
    source_files: list[str] = []
    languages: set[AstGrepLanguage] = set()
    digest = hashlib.sha256()
    file_metadata: list[ExampleSuiteFileMetadata] = []
    for path in paths:
        relative = path.resolve().relative_to(root).as_posix()
        size = path.stat().st_size
        if size > MAX_EXAMPLE_SUITE_FILE_BYTES:
            raise ValueError(
                f"example suite file exceeds {MAX_EXAMPLE_SUITE_FILE_BYTES} bytes: {relative}"
            )
        total_bytes += size
        if total_bytes > MAX_EXAMPLE_SUITE_TOTAL_BYTES:
            raise ValueError(
                f"example suite exceeds {MAX_EXAMPLE_SUITE_TOTAL_BYTES} total bytes: {total_bytes}"
            )
        suffix = path.suffix.lower()
        language = _SUFFIX_LANGUAGE.get(suffix)
        if language is not None:
            languages.add(language)
            source_files.append(relative)
        content = path.read_bytes()
        file_metadata.append(
            ExampleSuiteFileMetadata(
                path=relative,
                size=size,
                sha256=hashlib.sha256(content).hexdigest(),
                source=language is not None,
            )
        )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")

    if not source_files:
        raise ValueError("example suite does not contain a supported ast-grep source language")
    if len(languages) != 1:
        names = ", ".join(sorted(language.value for language in languages))
        raise ValueError(f"example suite must contain exactly one supported source language; found: {names}")

    return ExampleSuiteInspection(
        registry_key=f"example-suite:{suite_name}",
        suite_name=suite_name,
        source_path=source_path.as_posix(),
        content_digest=digest.hexdigest(),
        language=next(iter(languages)),
        file_count=len(paths),
        total_bytes=total_bytes,
        source_files=sorted(source_files),
        files=file_metadata,
        manifest_path=next((path for path in _MANIFEST_NAMES if (root / path).is_file()), None),
    )


def inspect_example_suite(example_suite: Path) -> ExampleSuiteInspection:
    """Validate an example suite and compute a stable path-and-content digest."""

    raw_root = example_suite.expanduser()
    if raw_root.is_symlink():
        raise ValueError(f"example suite must not be a symbolic link: {example_suite}")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValueError(f"example suite is not an existing directory: {example_suite}")
    if not root.name:
        raise ValueError(f"example suite must have a stable basename: {example_suite}")
    return _inspect_example_suite(root, suite_name=root.name, source_path=root)


def _same_snapshot(
    inspection: ExampleSuiteInspection,
    snapshot: Path,
) -> bool:
    try:
        copied = _inspect_example_suite(
            snapshot.resolve(),
            suite_name=inspection.suite_name,
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
    atomic_write_json(
        workspace.example_suite_metadata_path,
        intake.model_dump(mode="json"),
    )
    return intake


__all__ = [
    "MAX_EXAMPLE_SUITE_FILES",
    "MAX_EXAMPLE_SUITE_FILE_BYTES",
    "MAX_EXAMPLE_SUITE_TOTAL_BYTES",
    "ExampleSuiteInspection",
    "ExampleSuiteIntake",
    "inspect_example_suite",
    "materialize_example_suite",
]
