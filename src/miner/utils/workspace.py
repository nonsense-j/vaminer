"""Workspace persistence — one workspace per VAS rule."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .config import MINER_OUTPUT_DIR, VAS_RULES_DIR, VAS_WORKSPACE_DIR
from ..models.vas import VASFull
from .log import logger


def _next_vas_id(registry: SourceRegistry) -> str:
    ids = set(registry._load()) | {
        path.name
        for path in registry.path.parent.glob("VAS-*")
        if path.is_dir()
    }
    last_num = max(
        (int(v.split("-")[1]) for v in ids if re.fullmatch(r"VAS-\d{4,}", v)),
        default=0,
    )
    return f"VAS-{last_num + 1:04d}"


def atomic_write_json(path: Path, value: Any) -> None:
    """Durably replace one generated JSON file without exposing a partial write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def safe_input_id(source_id: str) -> str:
    """Return a readable, collision-resistant directory name for one source id."""

    source_id = source_id.strip()
    if not source_id:
        raise ValueError("source id must be non-empty")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_id).strip("-.")
    if safe == source_id and len(safe) <= 120:
        return safe
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
    prefix = (safe or "input")[:96].rstrip("-.")
    return f"{prefix}--{digest}"


class SourceRegistry:
    """Maps source identifiers (CVE IDs, URLs) to VAS IDs.

    Stored as `source_registry.json` at the vas_ws root.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self.path = (base_dir or VAS_WORKSPACE_DIR) / "source_registry.json"

    def _load(self) -> dict[str, list[str]]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, data: dict[str, list[str]]) -> None:
        atomic_write_json(self.path, data)

    def lookup(self, source: str) -> str | None:
        for vas_id, sources in self._load().items():
            if source in sources:
                return vas_id
        return None

    def register(self, vas_id: str, source: str) -> None:
        data = self._load()
        data.setdefault(vas_id, [])
        if source not in data[vas_id]:
            data[vas_id].append(source)
        self._save(data)


class ExampleSuiteRegistry:
    """Maps example-suite keys to VAS IDs and immutable content digests."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.path = (base_dir or VAS_WORKSPACE_DIR) / "example_suite_registry.json"

    def _load(self) -> dict[str, dict[str, str]]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        atomic_write_json(self.path, data)

    def lookup(self, registry_key: str) -> dict[str, str] | None:
        return self._load().get(registry_key)

    def register(self, registry_key: str, *, vas_id: str, content_digest: str) -> None:
        data = self._load()
        data[registry_key] = {
            "vas_id": vas_id,
            "content_digest": content_digest,
        }
        self._save(data)


class Workspace:
    """Isolated workspace for a single issue analysis.

    Layout:
        vas_ws/
            source_registry.json
            VAS-XXXX/
                cases/                       # Extracted root-cause cases and simple variants
                src/                         # Cloned repo or example-suite snapshot
        output/
            miner/VAS-XXXX/<input-id>/       # Caches and review output
            logs/miner/VAS-XXXX/<input-id>/  # Per-trace workflow logs
            artifacts/<runtime>/<input-id>/  # Per-trace runtime diagnostics
        src/.vaminer/skills/vas-scanner/rules/
            VAS-XXXX.json                    # Final VAS specification
    """

    def __init__(
        self,
        root: Path,
        vas_id: str,
        *,
        input_id: str | None = None,
        output_root: Path | None = None,
        trace_id: str | None = None,
        rules_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.vas_id = vas_id
        self.input_id = safe_input_id(input_id or vas_id)
        self.output_root = (output_root or MINER_OUTPUT_DIR).expanduser().resolve()
        self.trace_id = trace_id
        self.rules_dir = (rules_dir or VAS_RULES_DIR).resolve()

    @classmethod
    def get_vas_id(cls, source: str, base_dir: Path | None = None) -> str:
        base_dir = base_dir or VAS_WORKSPACE_DIR
        registry = SourceRegistry(base_dir)
        existing_id = registry.lookup(source)
        if existing_id:
            logger.info("Source '%s' already registered as %s", source, existing_id)
            return existing_id
        ws = cls._create_new(base_dir, registry)
        registry.register(ws.vas_id, source)
        return ws.vas_id

    @classmethod
    def prepare_example_suite_vas_id(
        cls,
        registry_key: str,
        *,
        content_digest: str,
        base_dir: Path | None = None,
    ) -> str:
        """Resolve a suite workspace without publishing registry state.

        The caller publishes and verifies the snapshot first, then calls
        :meth:`register_example_suite`. This ordering lets an interrupted copy
        be retried without leaving a newly registered partial snapshot.
        """

        base_dir = base_dir or VAS_WORKSPACE_DIR
        source_registry = SourceRegistry(base_dir)
        suite_registry = ExampleSuiteRegistry(base_dir)
        existing = suite_registry.lookup(registry_key)
        if existing is not None:
            existing_digest = existing.get("content_digest")
            if existing_digest != content_digest:
                raise ValueError(
                    f"example suite {registry_key!r} is already registered with a different digest: "
                    f"{existing_digest} != {content_digest}"
                )
            vas_id = existing["vas_id"]
            logger.info("Example suite '%s' already registered as %s", registry_key, vas_id)
            return vas_id

        # Recover the authoritative workspace id if one registry write from an
        # earlier run completed and the other did not.
        source_vas_id = source_registry.lookup(registry_key)
        if source_vas_id is not None:
            return source_vas_id
        return _next_vas_id(source_registry)

    @classmethod
    def register_example_suite(
        cls,
        registry_key: str,
        *,
        vas_id: str,
        content_digest: str,
        base_dir: Path | None = None,
    ) -> None:
        """Atomically replace both generated registry files after publication."""

        base_dir = base_dir or VAS_WORKSPACE_DIR
        workspace = cls.from_id(vas_id, base_dir=base_dir)
        if not workspace.example_suite_snapshot_dir.is_dir():
            raise ValueError("cannot register an example suite before its snapshot is published")
        suite_registry = ExampleSuiteRegistry(base_dir)
        existing = suite_registry.lookup(registry_key)
        if existing is not None and existing.get("content_digest") != content_digest:
            raise ValueError(
                f"example suite {registry_key!r} is already registered with a different digest"
            )
        SourceRegistry(base_dir).register(vas_id, registry_key)
        suite_registry.register(
            registry_key,
            vas_id=vas_id,
            content_digest=content_digest,
        )

    @classmethod
    def _ensure_structure(cls, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for path in root.iterdir():
            if path.name in {"cases", "src"}:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        (root / "cases").mkdir(exist_ok=True)
        (root / "src").mkdir(exist_ok=True)

    @classmethod
    def _create_new(
        cls,
        base_dir: Path,
        registry: SourceRegistry | None = None,
        *,
        rules_dir: Path | None = None,
    ) -> Workspace:
        if registry is None:
            registry = SourceRegistry(base_dir)
        vas_id = _next_vas_id(registry)
        root = base_dir / vas_id
        cls._ensure_structure(root)
        return cls(root, vas_id, rules_dir=rules_dir)

    @classmethod
    def from_id(
        cls,
        vas_id: str,
        base_dir: Path | None = None,
        *,
        input_id: str | None = None,
        output_root: Path | None = None,
        trace_id: str | None = None,
        rules_dir: Path | None = None,
    ) -> Workspace:
        base_dir = base_dir or VAS_WORKSPACE_DIR
        root = base_dir / vas_id
        cls._ensure_structure(root)
        return cls(
            root,
            vas_id,
            input_id=input_id,
            output_root=output_root,
            trace_id=trace_id,
            rules_dir=rules_dir,
        )

    @property
    def rule_path(self) -> Path:
        return self.rules_dir / f"{self.vas_id}.json"

    @property
    def run_output_dir(self) -> Path:
        return self.output_root / "miner" / self.vas_id / self.input_id

    @property
    def cache_dir(self) -> Path:
        return self.run_output_dir / "caches"

    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def anchor_review_path(self) -> Path:
        self.run_output_dir.mkdir(parents=True, exist_ok=True)
        return self.run_output_dir / "anchor_review.md"

    @property
    def artifact_root(self) -> Path:
        return self.output_root / "artifacts"

    @property
    def example_suite_snapshot_dir(self) -> Path:
        return self.root / "src" / "input_snapshot"

    def save_rule(self, vas: VASFull) -> Path:
        self.rule_path.parent.mkdir(parents=True, exist_ok=True)
        self.rule_path.write_text(vas.model_dump_json(indent=2, by_alias=True))
        return self.rule_path

    def clear_cases(self) -> None:
        """Reset the generated cases directory before a fresh RCA run."""
        if self.cases_dir.exists():
            shutil.rmtree(self.cases_dir)
        self.cases_dir.mkdir(parents=True, exist_ok=True)
