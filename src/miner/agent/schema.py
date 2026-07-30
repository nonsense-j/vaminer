"""Provider-friendly JSON Schema rendering for Pydantic output contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel

_COMPOSITION_KEYS = frozenset({"allOf", "anyOf", "oneOf"})
_DIRECT_KEYS = frozenset(
    {
        "additionalProperties",
        "const",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "title",
        "type",
    }
)
_DESCRIPTIVE_CONSTRAINTS = {
    "default": "Default value: {value}.",
    "exclusiveMaximum": "Value must be less than {value}.",
    "exclusiveMinimum": "Value must be greater than {value}.",
    "format": "Expected format: {value}.",
    "maxItems": "Maximum item count: {value}.",
    "maxLength": "Maximum string length: {value}.",
    "maximum": "Value must be less than or equal to {value}.",
    "minItems": "Minimum item count: {value}.",
    "minLength": "Minimum string length: {value}.",
    "minimum": "Value must be greater than or equal to {value}.",
    "multipleOf": "Value must be a multiple of {value}.",
    "pattern": "Value must match this regular expression: {value}.",
    "uniqueItems": "Items must be unique: {value}.",
}
_IGNORED_ANNOTATIONS = frozenset(
    {
        "$id",
        "$schema",
        "deprecated",
        "examples",
        "readOnly",
        "writeOnly",
    }
)


def _json_pointer(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"only local JSON Schema references are supported: {reference!r}")
    current: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"unresolved JSON Schema reference: {reference!r}")
        current = current[part]
    if not isinstance(current, dict):
        raise ValueError(f"JSON Schema reference does not resolve to an object: {reference!r}")
    return current


def _resolve_references(
    node: Any,
    *,
    root: dict[str, Any],
    stack: tuple[str, ...] = (),
) -> Any:
    if isinstance(node, list):
        return [_resolve_references(item, root=root, stack=stack) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        reference = node["$ref"]
        if not isinstance(reference, str):
            raise ValueError("JSON Schema $ref must be a string")
        if reference in stack:
            chain = " -> ".join((*stack, reference))
            raise ValueError(f"recursive JSON Schema reference is unsupported: {chain}")
        resolved = _resolve_references(
            deepcopy(_json_pointer(root, reference)),
            root=root,
            stack=(*stack, reference),
        )
        assert isinstance(resolved, dict)
        siblings = {
            key: value
            for key, value in node.items()
            if key != "$ref"
        }
        if siblings:
            resolved.update(
                _resolve_references(siblings, root=root, stack=stack)
            )
        return resolved

    return {
        key: _resolve_references(value, root=root, stack=stack)
        for key, value in node.items()
        if key not in {"$defs", "definitions"}
    }


def _description_fragment(template: str, value: Any) -> str:
    rendered = repr(value) if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else str(value)
    return template.format(value=rendered)


def _normalize_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalize_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    normalized: dict[str, Any] = {}
    description_parts: list[str] = []
    existing_description = node.get("description")
    if isinstance(existing_description, str) and existing_description.strip():
        description_parts.append(existing_description.strip())

    for key, value in node.items():
        if key == "description":
            continue
        if key == "properties":
            if not isinstance(value, dict):
                raise ValueError("JSON Schema properties must be an object")
            normalized[key] = {
                name: _normalize_schema(schema)
                for name, schema in value.items()
            }
        elif key in _COMPOSITION_KEYS:
            if not isinstance(value, list):
                raise ValueError(f"JSON Schema {key} must be an array")
            normalized[key] = [_normalize_schema(item) for item in value]
        elif key == "items":
            normalized[key] = _normalize_schema(value)
        elif key == "additionalProperties" and isinstance(value, dict):
            normalized[key] = _normalize_schema(value)
        elif key in _DIRECT_KEYS:
            normalized[key] = deepcopy(value)
        elif key in _DESCRIPTIVE_CONSTRAINTS:
            description_parts.append(
                _description_fragment(_DESCRIPTIVE_CONSTRAINTS[key], value)
            )
        elif key in _IGNORED_ANNOTATIONS:
            continue
        else:
            raise ValueError(f"unsupported JSON Schema keyword: {key!r}")

    if normalized.get("type") == "object" and "properties" in normalized:
        normalized.setdefault("additionalProperties", False)
    if description_parts:
        normalized["description"] = " ".join(description_parts)
    return normalized


def descriptive_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return an inlined, descriptive schema suitable for Claude JSON contracts."""
    raw = model.model_json_schema(mode="validation", by_alias=True)
    resolved = _resolve_references(raw, root=raw)
    normalized = _normalize_schema(resolved)
    if not isinstance(normalized, dict):
        raise ValueError("model JSON Schema root must be an object")
    return normalized


__all__ = ["descriptive_json_schema"]
