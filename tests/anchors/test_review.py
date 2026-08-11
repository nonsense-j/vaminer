"""Tests for deterministic degraded-anchor review output."""

from __future__ import annotations

from pathlib import Path

from src.miner.anchors import review as review_module
from src.miner.anchors.review import review_anchors
from tests.support.factories import root_cause, vas_core


def test_anchor_review_prominently_lists_and_logs_disabled_anchors(
    tmp_path: Path,
    monkeypatch,
):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text("void trigger(void) {}\n", encoding="utf-8")
    (cases_dir / "case1.c").write_text("void case1(void) {}\n", encoding="utf-8")
    core = vas_core(root_cause()).model_copy(deep=True)
    core.anchors[0].query = ""
    logged: list[str] = []
    monkeypatch.setattr(
        review_module.logger,
        "warning",
        lambda message, *args: logged.append(message % args),
    )

    markdown = review_anchors(
        "VAS-0001",
        core,
        source_root,
        cases_dir,
    )

    assert "## Validation Warnings" in markdown
    assert "danger-call" in markdown
    assert "case1.c" in markdown
    assert any("anchor 'danger-call' is disabled" in message for message in logged)
