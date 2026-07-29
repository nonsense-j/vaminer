"""Load the scanner engine from the self-contained deployable skill."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ENGINE_PATH = Path(__file__).resolve().parents[2] / ".vaminer" / "skills" / "vas-scanner" / "scripts" / "engine.py"
_ENGINE_MODULE_NAME = "_vaminer_bundled_scanner_engine"


def _load_engine() -> ModuleType:
    existing = sys.modules.get(_ENGINE_MODULE_NAME)
    if existing is not None:
        return existing
    if not ENGINE_PATH.is_file():
        raise FileNotFoundError(f"bundled scanner engine not found: {ENGINE_PATH}")

    spec = importlib.util.spec_from_file_location(_ENGINE_MODULE_NAME, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load bundled scanner engine: {ENGINE_PATH}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[_ENGINE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ENGINE_MODULE_NAME, None)
        raise
    return module


_engine = _load_engine()

AnchorMatch = _engine.AnchorMatch
AnchorRunResult = _engine.AnchorRunResult
AnchorScanError = _engine.AnchorScanError
AnchorScanResult = _engine.AnchorScanResult
scan_anchors = _engine.scan_anchors

__all__ = [
    "ENGINE_PATH",
    "AnchorMatch",
    "AnchorRunResult",
    "AnchorScanError",
    "AnchorScanResult",
    "scan_anchors",
]
