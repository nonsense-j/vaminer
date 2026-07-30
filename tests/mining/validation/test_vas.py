"""Tests for final VAS acceptance and portable source metadata."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.miner.models import VASFull
from tests.support.factories import root_cause, vas_core


def test_vas_accepts_only_portable_example_suite_source_shape():
    rca = root_cause()
    core = vas_core(rca)
    base = {
        "vas_id": "VAS-0001",
        "category": "SECURITY",
        "language": "c",
        "summary": core.summary,
        "scenarios": core.scenarios.model_dump(mode="json"),
        "anchors": [
            item.model_dump(mode="json", by_alias=True)
            for item in core.anchors
        ],
    }
    suite = VASFull.model_validate(
        {
            **base,
            "sources": [
                {
                    "type": "example_suite",
                    "registry_key": "example-suite:CWE-2099-fixture",
                    "suite_name": "CWE-2099-fixture",
                    "content_digest": "a" * 64,
                    "snapshot_ref": "input_snapshot",
                    "files": [
                        {
                            "path": "bad.c",
                            "size": 10,
                            "sha256": "b" * 64,
                            "source": True,
                        }
                    ],
                    "root_cause_summary": rca.root_cause_summary,
                }
            ],
        }
    )
    assert suite.sources[0].type == "example_suite"
    assert "/" not in suite.sources[0].snapshot_ref

    with pytest.raises(ValidationError):
        VASFull.model_validate(
            {
                **base,
                "sources": [
                    {
                        "type": "case_bundle",
                        "snapshot_path": "/tmp/input_snapshot",
                    }
                ],
            }
        )
