"""Tests for provider-friendly Pydantic JSON contracts."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError
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


def test_descriptive_schema_removes_unsupported_validation_keywords():
    schema = descriptive_json_schema(AnchorSynthesisRunRequest)
    keys = _all_keys(schema)

    assert not {
        "maximum",
        "minimum",
        "minItems",
        "minLength",
        "pattern",
    } & keys
    assert "target_anchor_id" in schema["properties"]


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


def test_descriptive_schema_rejects_recursive_models():
    class RecursiveModel(BaseModel):
        child: RecursiveModel | None = None

    with pytest.raises(ValueError, match="recursive JSON Schema reference"):
        descriptive_json_schema(RecursiveModel)


def test_descriptive_schema_rejects_unresolved_references(monkeypatch):
    class BrokenModel(BaseModel):
        value: str

    monkeypatch.setattr(
        BrokenModel,
        "model_json_schema",
        classmethod(
            lambda cls, *args, **kwargs: {
                "$ref": "#/$defs/Missing",
                "$defs": {},
            }
        ),
    )

    with pytest.raises(ValueError, match="unresolved JSON Schema reference"):
        descriptive_json_schema(BrokenModel)
