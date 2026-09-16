from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter
from pydantic_ai import Tool
from pydantic_ai.tools import GenerateToolJsonSchema

from src.miner.agent import AgentPhase
from src.miner.mining.tasks import PHASE_DEFINITIONS
from src.miner.models import RuleGenerationDraft
from src.miner.models.base import InlineJsonSchemaModel
from src.miner.models.tool import AnchorPlanInput


def _find_keyword(node: Any, keyword: str) -> bool:
    if isinstance(node, dict):
        return keyword in node or any(
            _find_keyword(value, keyword) for value in node.values()
        )
    if isinstance(node, list):
        return any(_find_keyword(item, keyword) for item in node)
    return False


@pytest.mark.parametrize("phase", list(AgentPhase))
def test_phase_output_json_schemas_inline_all_references(phase: AgentPhase):
    output_type = PHASE_DEFINITIONS[phase].output_type

    schemas = (
        output_type.model_json_schema(mode="validation", by_alias=True),
        TypeAdapter(output_type).json_schema(
            mode="validation",
            by_alias=True,
            schema_generator=GenerateToolJsonSchema,
        ),
    )

    for schema in schemas:
        assert not _find_keyword(schema, "$ref")
        assert not _find_keyword(schema, "$defs")
        assert not _find_keyword(schema, "definitions")


def test_rule_generation_draft_inlines_scenarios_schema():
    schema = RuleGenerationDraft.model_json_schema(mode="validation", by_alias=True)

    scenarios = schema["properties"]["scenarios"]
    assert scenarios["type"] == "object"
    assert scenarios["additionalProperties"] is False
    assert scenarios["required"] == ["unsafe", "safe"]
    assert scenarios["properties"]["unsafe"]["items"] == {"type": "string"}
    assert scenarios["properties"]["safe"]["items"] == {"type": "string"}


def test_anchor_plan_tool_schema_keeps_plan_wrapper_and_inlines_all_references():
    async def synthesize_anchor_plan(plan: AnchorPlanInput):
        return plan

    tool = Tool(synthesize_anchor_plan)
    schema = tool.function_schema.json_schema

    assert schema["required"] == ["plan"]
    plan_schema = schema["properties"]["plan"]
    assert plan_schema["type"] == "object"
    assert plan_schema["additionalProperties"] is False
    assert plan_schema["required"] == ["summary", "intents"]
    intent_schema, reuse_schema = plan_schema["properties"]["intents"]["items"]["anyOf"]
    assert intent_schema["required"] == [
        "id",
        "behavior_weight",
        "behavior",
        "inspect_hint",
        "required_cases",
    ]
    assert "draft_query" not in intent_schema["required"]
    assert reuse_schema["required"] == ["reuse_anchor_id"]
    assert not _find_keyword(schema, "$ref")
    assert not _find_keyword(schema, "$defs")
    assert not _find_keyword(schema, "definitions")

    validated = tool.function_schema.validator.validate_python(
        {
            "plan": {
                "summary": "Copies must preserve bounds.",
                "intents": [
                    {
                        "id": "copy-site",
                        "behavior_weight": 4,
                        "behavior": "Copy a value.",
                        "inspect_hint": "Inspect the bound.",
                        "required_cases": ["case1.c"],
                    }
                ],
            }
        }
    )
    assert validated["plan"].intents[0].id == "copy-site"

    revised = tool.function_schema.validator.validate_python(
        {
            "plan": {
                "summary": "Copies must preserve bounds.",
                "intents": [
                    {"reuse_anchor_id": "copy-site"},
                    {
                        "id": "length-site",
                        "behavior_weight": 3,
                        "behavior": "Derive a copy length.",
                        "inspect_hint": "Inspect its range.",
                        "required_cases": ["case1.c"],
                        "draft_query": "$LEN = size($SRC)",
                    },
                ],
            }
        }
    )
    assert revised["plan"].intents[0].reuse_anchor_id == "copy-site"


def test_inline_schema_model_expands_multi_level_refs_and_preserves_siblings():
    class Leaf(BaseModel):
        value: str

    class Branch(BaseModel):
        leaf: Leaf

    class Contract(InlineJsonSchemaModel):
        branch: Branch = Field(description="Nested contract")

    schema = Contract.model_json_schema()

    branch = schema["properties"]["branch"]
    assert branch["description"] == "Nested contract"
    assert branch["properties"]["leaf"]["properties"]["value"]["type"] == "string"
    assert not _find_keyword(schema, "$ref")
    assert not _find_keyword(schema, "$defs")
