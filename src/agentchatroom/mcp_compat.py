from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import ToolManager


def _single_schema_type(schema: dict[str, Any]) -> str | None:
    direct = schema.get("type")
    if isinstance(direct, str):
        return direct
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if not isinstance(variants, list):
            continue
        concrete = [
            variant
            for variant in variants
            if isinstance(variant, dict) and variant.get("type") != "null"
        ]
        types = {variant.get("type") for variant in concrete}
        if len(concrete) == 1 and len(types) == 1:
            value = next(iter(types))
            return value if isinstance(value, str) else None
    return None


def _parse_json_string(value: str, *, field: str, expected: str) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ToolError(
            f"MCP argument {field!r} must be a JSON {expected} literal"
        ) from error
    return parsed


def _resolve_schema(
    schema: dict[str, Any], root_schema: dict[str, Any] | None
) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or root_schema is None:
        return schema
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        return schema
    definitions = root_schema.get("$defs")
    target_name = reference[len(prefix) :]
    target = definitions.get(target_name) if isinstance(definitions, dict) else None
    if not isinstance(target, dict):
        return schema
    return {**target, **{key: item for key, item in schema.items() if key != "$ref"}}


def coerce_schema_value(
    value: Any,
    schema: dict[str, Any],
    *,
    field: str,
    root_schema: dict[str, Any] | None = None,
) -> Any:
    schema = _resolve_schema(schema, root_schema)
    expected = _single_schema_type(schema)
    converted = value

    if isinstance(value, str):
        if expected == "array" and not value.strip():
            converted = []
        elif expected == "boolean":
            normalized = value.strip()
            if normalized not in {"true", "false"}:
                raise ToolError(
                    f"MCP argument {field!r} must be the boolean literal true or false"
                )
            converted = normalized == "true"
        elif expected == "integer":
            converted = _parse_json_string(value, field=field, expected="integer")
            if isinstance(converted, bool) or not isinstance(converted, int):
                raise ToolError(f"MCP argument {field!r} must be an integer")
        elif expected == "number":
            converted = _parse_json_string(value, field=field, expected="number")
            if isinstance(converted, bool) or not isinstance(converted, (int, float)):
                raise ToolError(f"MCP argument {field!r} must be a number")
        elif expected == "array":
            converted = _parse_json_string(value, field=field, expected="array")
            if not isinstance(converted, list):
                raise ToolError(f"MCP argument {field!r} must be an array")
        elif expected == "object":
            converted = _parse_json_string(value, field=field, expected="object")
            if not isinstance(converted, dict):
                raise ToolError(f"MCP argument {field!r} must be an object")

    if expected == "array" and isinstance(converted, dict):
        # Some MCP clients wrap a single array item as {"item": value}.
        # Accept that shape only at this schema-directed array boundary.
        if set(converted) == {"item"}:
            converted = [converted["item"]]

    if expected == "array" and isinstance(converted, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [
                coerce_schema_value(
                    item,
                    item_schema,
                    field=f"{field}[{index}]",
                    root_schema=root_schema,
                )
                for index, item in enumerate(converted)
            ]
    if expected == "object" and isinstance(converted, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return {
                key: coerce_schema_value(
                    item,
                    properties[key],
                    field=f"{field}.{key}",
                    root_schema=root_schema,
                )
                if key in properties and isinstance(properties[key], dict)
                else item
                for key, item in converted.items()
            }
    return converted


def normalize_mcp_arguments(
    arguments: dict[str, Any], input_schema: dict[str, Any]
) -> dict[str, Any]:
    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        return dict(arguments)
    return {
        name: coerce_schema_value(value, schema, field=name, root_schema=input_schema)
        if isinstance(schema := properties.get(name), dict)
        else value
        for name, value in arguments.items()
    }


class CompatibleToolManager(ToolManager):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        tool = self.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        normalized = normalize_mcp_arguments(arguments, tool.parameters)
        return await tool.run(
            normalized,
            context=context,
            convert_result=convert_result,
        )
