from __future__ import annotations

import json
from functools import partial
from typing import Any

import anyio
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import ToolManager

from .errors import DomainError
from .services import is_strict_int

# A client that clears an optional argument sends "". These are the argument
# kinds where "" has no valid literal meaning, so the argument is dropped and
# the tool's own default applies instead.
_BLANK_OMITTABLE_TYPES = {"integer", "number", "boolean", "object"}


def _single_schema_type(schema: dict[str, Any]) -> str | None:
    direct = schema.get("type")
    if isinstance(direct, str):
        return direct
    if isinstance(direct, list):
        types = {item for item in direct if isinstance(item, str) and item != "null"}
        if len(types) == 1:
            return next(iter(types))
        return None
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


def _permits_null(schema: dict[str, Any]) -> bool:
    direct = schema.get("type")
    if isinstance(direct, list):
        return "null" in direct
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(
            isinstance(variant, dict) and variant.get("type") == "null"
            for variant in variants
        ):
            return True
    return False


def _unwrap_nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not _permits_null(schema):
        return schema
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if not isinstance(variants, list) or len(variants) != 2:
            continue
        concrete = [
            item for item in variants
            if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(concrete) == 1 and any(
            isinstance(item, dict) and item.get("type") == "null"
            for item in variants
        ):
            return {
                **concrete[0],
                **{name: value for name, value in schema.items() if name != key},
            }
    return schema


def coerce_schema_value(
    value: Any,
    schema: dict[str, Any],
    *,
    field: str,
    root_schema: dict[str, Any] | None = None,
) -> Any:
    schema = _resolve_schema(schema, root_schema)
    nullable = _permits_null(schema)
    schema = _unwrap_nullable_schema(schema)
    schema = _resolve_schema(schema, root_schema)
    expected = _single_schema_type(schema)
    converted = value

    if isinstance(value, str) and not value.strip():
        if expected == "array":
            converted = []
        elif expected in (None, "string"):
            # An empty string is a legal string argument; only the kinds that
            # have no "" literal are cleared here.
            pass
        elif nullable:
            converted = None
        else:
            raise DomainError(
                "invalid_mcp_argument",
                f"MCP argument {field!r} was empty but takes no null value",
                status_code=400,
                details={
                    "field": field,
                    "expected": expected or "a non-empty value",
                    "required_action": (
                        f"pass a JSON {expected or 'value'} literal for {field!r}, "
                        "or omit the argument entirely when it is optional"
                    ),
                },
            )
    elif isinstance(value, str):
        if expected == "boolean":
            normalized = value.strip()
            if normalized not in {"true", "false"}:
                raise ToolError(
                    f"MCP argument {field!r} must be the boolean literal true or false"
                )
            converted = normalized == "true"
        elif expected == "integer":
            converted = _parse_json_string(value, field=field, expected="integer")
            if not is_strict_int(converted):
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


def _is_cleared_optional_argument(
    name: str, schema: dict[str, Any], root_schema: dict[str, Any]
) -> bool:
    """A tool argument the caller may leave unset and that has no null value.

    "" sent for such an argument is the client clearing its own choice, not a
    malformed literal, so the argument is dropped and the tool default applies.
    """
    required = root_schema.get("required")
    if isinstance(required, list) and name in required:
        return False
    resolved = _resolve_schema(schema, root_schema)
    nullable = _permits_null(resolved)
    unwrapped = _resolve_schema(_unwrap_nullable_schema(resolved), root_schema)
    if _single_schema_type(unwrapped) not in _BLANK_OMITTABLE_TYPES:
        return False
    return nullable or "default" in resolved or "default" in unwrapped


def normalize_mcp_arguments(
    arguments: dict[str, Any], input_schema: dict[str, Any]
) -> dict[str, Any]:
    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        return dict(arguments)
    normalized: dict[str, Any] = {}
    for name, value in arguments.items():
        schema = properties.get(name)
        if not isinstance(schema, dict):
            normalized[name] = value
            continue
        if (
            isinstance(value, str)
            and not value.strip()
            and _is_cleared_optional_argument(name, schema, input_schema)
        ):
            continue
        normalized[name] = coerce_schema_value(
            value, schema, field=name, root_schema=input_schema
        )
    return normalized


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
        if tool.is_async or not _can_offload(tool):
            return await tool.run(
                normalized,
                context=context,
                convert_result=convert_result,
            )
        # The SDK invokes a synchronous tool function inline, which parks the
        # whole event loop for the tool's duration: REST, SSE and every other
        # MCP client stall behind one work_report's git subprocesses. The
        # domain call is unchanged; only where it runs differs.
        return await _run_sync_tool_off_loop(tool, normalized, context, convert_result)


def _can_offload(tool: Any) -> bool:
    metadata = getattr(tool, "fn_metadata", None)
    return (
        metadata is not None
        and callable(getattr(tool, "fn", None))
        and hasattr(metadata, "pre_parse_json")
        and hasattr(metadata, "arg_model")
    )


async def _run_sync_tool_off_loop(
    tool: Any, normalized: dict[str, Any], context: Any, convert_result: bool
) -> Any:
    """Run a synchronous MCP tool in a worker thread.

    Argument validation and result conversion deliberately reuse the SDK's own
    implementation so the offloaded path cannot drift from the inline one.
    """
    metadata = tool.fn_metadata
    parsed = metadata.arg_model.model_validate(metadata.pre_parse_json(normalized))
    kwargs = parsed.model_dump_one_level()
    if getattr(tool, "context_kwarg", None) is not None:
        kwargs[tool.context_kwarg] = context
    result = await anyio.to_thread.run_sync(partial(tool.fn, **kwargs))
    if convert_result:
        result = metadata.convert_result(result)
    return result
