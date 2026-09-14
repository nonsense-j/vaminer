"""Shared behavior for model-facing structured-output contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema


def _inline_local_references(
    node: Any,
    *,
    handler: GetJsonSchemaHandler,
    stack: tuple[str, ...] = (),
) -> Any:
    if isinstance(node, list):
        return [
            _inline_local_references(item, handler=handler, stack=stack)
            for item in node
        ]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        reference = node["$ref"]
        if not isinstance(reference, str):
            raise ValueError("JSON Schema $ref must be a string")
        if reference in stack:
            chain = " -> ".join((*stack, reference))
            raise ValueError(f"recursive JSON Schema reference is unsupported: {chain}")

        resolved = handler.resolve_ref_schema({"$ref": reference})
        inlined = _inline_local_references(
            deepcopy(resolved),
            handler=handler,
            stack=(*stack, reference),
        )
        assert isinstance(inlined, dict)
        siblings = {
            key: _inline_local_references(value, handler=handler, stack=stack)
            for key, value in node.items()
            if key != "$ref"
        }
        inlined.update(siblings)
        return inlined

    return {
        key: _inline_local_references(value, handler=handler, stack=stack)
        for key, value in node.items()
        if key not in {"$defs", "definitions"}
    }


class InlineJsonSchemaModel(BaseModel):
    """Base for top-level contracts whose provider schema must not contain refs."""

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        inlined = _inline_local_references(schema, handler=handler)
        if not isinstance(inlined, dict):  # pragma: no cover - model schemas are objects.
            raise ValueError("model JSON Schema root must be an object")
        return inlined


__all__ = ["InlineJsonSchemaModel"]
