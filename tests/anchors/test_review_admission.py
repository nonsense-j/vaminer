"""Admission-threshold coverage for the deterministic Anchor review."""

from pathlib import Path

from src.miner.anchors.review import (
    render_file_priority_table,
    render_hotspot_annotated_view,
)
from src.miner.anchors.scanner import AnchorMatch, AnchorRunResult, AnchorScanResult


def _result(root: Path) -> AnchorScanResult:
    low = {
        "id": "low",
        "query_weight": 2,
        "behavior_weight": 2,
        "behavior": "Low-weight navigation behavior.",
        "inspect_hint": "Inspect low-weight behavior.",
    }
    high = {
        "id": "high",
        "query_weight": 3,
        "behavior_weight": 3,
        "behavior": "Admitting behavior.",
        "inspect_hint": "Inspect admitting behavior.",
    }
    return AnchorScanResult(
        root=root,
        anchor_results=[
            AnchorRunResult(
                anchor=low,
                matches=[
                    AnchorMatch(
                        anchor_id="low",
                        query_weight=2,
                        behavior=low["behavior"],
                        inspect_hint=low["inspect_hint"],
                        file="low.c",
                        start_line=1,
                        end_line=1,
                    )
                ],
            ),
            AnchorRunResult(
                anchor=high,
                matches=[
                    AnchorMatch(
                        anchor_id="high",
                        query_weight=3,
                        behavior=high["behavior"],
                        inspect_hint=high["inspect_hint"],
                        file="high.c",
                        start_line=1,
                        end_line=1,
                    )
                ],
            ),
        ],
    )


def test_review_views_use_query_weight_three_for_file_admission(tmp_path: Path):
    (tmp_path / "low.c").write_text("low();\n", encoding="utf-8")
    (tmp_path / "high.c").write_text("high();\n", encoding="utf-8")
    scan = _result(tmp_path)

    hotspot = render_hotspot_annotated_view(scan)
    priority = render_file_priority_table(
        scan,
        {"low": "A1", "high": "A2"},
        "R",
    )

    assert "high.c" in hotspot
    assert "low.c" not in hotspot
    assert "high.c" in priority
    assert "low.c" not in priority
