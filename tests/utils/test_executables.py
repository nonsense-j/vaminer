from __future__ import annotations

from pathlib import Path

import pytest

from src.miner.utils import executables


def test_managed_executable_uses_active_python_scripts_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scripts = tmp_path / "bin"
    scripts.mkdir()
    filename = "rg.exe" if executables.sys.platform == "win32" else "rg"
    binary = scripts / filename
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(executables.sysconfig, "get_path", lambda name: str(scripts))

    assert executables.managed_executable("rg") == str(binary)


def test_managed_executable_never_falls_back_to_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scripts = tmp_path / "empty-bin"
    scripts.mkdir()
    monkeypatch.setattr(executables.sysconfig, "get_path", lambda name: str(scripts))

    with pytest.raises(executables.ManagedExecutableError, match=r"run `uv sync`"):
        executables.managed_executable("rg")
