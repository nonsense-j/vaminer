"""Resolve command-line tools installed in the active Python environment."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import sysconfig


class ManagedExecutableError(RuntimeError):
    """Raised when a uv-managed executable is missing or unusable."""


def managed_executable(name: str) -> str:
    """Return an executable installed beside the active Python interpreter."""

    filename = f"{name}.exe" if sys.platform == "win32" else name
    scripts_dir = Path(sysconfig.get_path("scripts"))
    executable = scripts_dir / filename
    if not executable.is_file() or (sys.platform != "win32" and not os.access(executable, os.X_OK)):
        raise ManagedExecutableError(
            f"{name} is missing from the Miner environment at {executable}; run `uv sync`"
        )
    return str(executable)


__all__ = ["ManagedExecutableError", "managed_executable"]
