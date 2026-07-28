"""Workspace manager — one workspace per vas rule (VAS-XXXX)."""

import json
import shutil
from pathlib import Path

from ..configs import VAS_RULES_DIR, VAS_WORKSPACE_DIR
from .logger import logger


def _next_vas_id(registry: "SourceRegistry") -> str:
    ids = registry._load().keys()
    last_num = max((int(v.split("-")[1]) for v in ids), default=0)
    return f"VAS-{last_num + 1:04d}"


class SourceRegistry:
    """Maps source identifiers (CVE IDs, URLs) to VAS IDs.

    Stored as `source_registry.json` at the vas_ws root.
    """

    def __init__(self, base_dir: Path | None = None):
        self.path = (base_dir or VAS_WORKSPACE_DIR) / "source_registry.json"

    def _load(self) -> dict[str, list[str]]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, data: dict[str, list[str]]) -> None:
        self.path.write_text(json.dumps(data, indent=2))

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


class Workspace:
    """Isolated workspace for a single issue analysis.

    Layout:
        vas_ws/
            source_registry.json
            VAS-XXXX/
                cache/                       # Agent output cache files
                cases/                       # Extracted root-cause cases and simple variants
                src/                         # Cloned repo (buggy + fixed branches)
                analysis.md                  # Root cause analysis for the mined issue
                anchor_review.md             # Anchor case coverage and repository matches
        src/.vaminer/skills/vas-scanner/rules/
            VAS-XXXX.json                    # Final VAS specification
    """

    def __init__(self, root: Path, vas_id: str):
        self.root = root
        self.vas_id = vas_id

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
    def _ensure_structure(cls, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "cache").mkdir(exist_ok=True)
        (root / "cases").mkdir(exist_ok=True)
        (root / "src").mkdir(exist_ok=True)

    @classmethod
    def _create_new(cls, base_dir: Path, registry: SourceRegistry | None = None) -> "Workspace":
        if registry is None:
            registry = SourceRegistry(base_dir)
        vas_id = _next_vas_id(registry)
        root = base_dir / vas_id
        cls._ensure_structure(root)
        return cls(root, vas_id)

    @classmethod
    def from_id(cls, vas_id: str, base_dir: Path | None = None) -> "Workspace":
        base_dir = base_dir or VAS_WORKSPACE_DIR
        root = base_dir / vas_id
        cls._ensure_structure(root)
        return cls(root, vas_id)

    @property
    def rule_path(self) -> Path:
        return VAS_RULES_DIR / f"{self.vas_id}.json"

    @property
    def analysis_path(self) -> Path:
        return self.root / "analysis.md"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def anchor_review_path(self) -> Path:
        return self.root / "anchor_review.md"

    def save_rule(self, vas) -> Path:
        self.rule_path.parent.mkdir(parents=True, exist_ok=True)
        self.rule_path.write_text(vas.model_dump_json(indent=2, by_alias=True))
        return self.rule_path

    def save_analysis(self, analysis: str) -> Path:
        self.analysis_path.write_text(analysis)
        return self.analysis_path

    def clear_cases(self) -> None:
        """Reset the generated cases directory before a fresh RCA run."""
        if self.cases_dir.exists():
            shutil.rmtree(self.cases_dir)
        self.cases_dir.mkdir(parents=True, exist_ok=True)
