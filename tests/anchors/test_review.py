"""Tests for non-persisted post-generation Anchor coverage checks."""

from pathlib import Path


def test_anchor_review_markdown_is_not_part_of_workspace_output(tmp_path: Path):
    assert not (tmp_path / "anchor_review.md").exists()
