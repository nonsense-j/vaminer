"""Tests for provider-friendly Pydantic JSON contracts."""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from src.miner.agent import descriptive_json_schema
from src.miner.models import (
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
)
from tests.support.factories import BEHAVIOR, INSPECT_HINT, root_cause


def _all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for nested in value.values()
            for key in _all_keys(nested)
        }
    if isinstance(value, list):
        return {
            key
            for nested in value
            for key in _all_keys(nested)
        }
    return set()


def test_descriptive_schema_inlines_references_and_preserves_aliases():
    schema = descriptive_json_schema(AnchorSynthesisRunResult)
    keys = _all_keys(schema)
    anchor_properties = schema["properties"]["anchor"]["properties"]

    assert "$ref" not in keys
    assert "$defs" not in keys
    assert "type" in anchor_properties
    assert "query_type" not in anchor_properties
    assert schema["properties"]["plan_suggestion"]["type"] == "string"
    assert "plan_suggestion" in schema["required"]
    assert schema["additionalProperties"] is False


def test_anchor_synthesis_requests_require_unique_plan_ids_and_a_known_target():
    intent = {
        "id": "danger-call",
        "behavior_weight": 5,
        "behavior": BEHAVIOR,
        "inspect_hint": INSPECT_HINT,
        "required_cases": ["case1.c"],
    }
    common = {
        "root_cause": root_cause().model_dump(mode="json"),
        "summary": "Dangerous operations require their guard.",
    }

    with pytest.raises(ValidationError, match="anchor intent ids must be unique"):
        AnchorSynthesisRequest.model_validate(
            {
                **common,
                "anchor_intents": [intent, intent],
            }
        )

    with pytest.raises(ValidationError, match="target_anchor_id must identify"):
        AnchorSynthesisRunRequest.model_validate(
            {
                **common,
                "anchor_plan": [intent],
                "target_anchor_id": "missing-anchor",
            }
        )
