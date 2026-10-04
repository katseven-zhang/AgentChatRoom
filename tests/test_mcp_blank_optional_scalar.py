"""A client that clears an optional MCP scalar must not break the whole call.

Some MCP clients send "" for an optional argument instead of omitting it. The
compatibility layer used to accept "" only for arrays, so every other cleared
scalar failed with a bare ToolError before the tool ran.
"""

from __future__ import annotations

import pytest

from agentchatroom import mcp_server
from agentchatroom.errors import DomainError
from agentchatroom.mcp_compat import coerce_schema_value, normalize_mcp_arguments

# (tool, argument) pairs named on the card plus the rest of the write surface.
CLEARED_OPTIONAL_SCALARS = [
    ("task_create", "priority"),
    ("task_update", "priority"),
    ("task_update", "progress_percent"),
    ("task_claim", "reclaim"),
    ("task_claim", "takeover"),
    ("message_post", "requires_ack"),
    ("message_post", "priority"),
    ("lease_acquire", "ttl_seconds"),
    ("audit_query", "limit"),
    ("audit_query", "after"),
    ("audit_query", "before"),
]

WRITE_TOOLS = [
    "task_create",
    "task_update",
    "task_claim",
    "task_acknowledge",
    "task_release",
    "task_handoff",
    "message_post",
    "review_submit",
    "work_report",
    "integration_submit",
    "knowledge_candidate_submit",
    "lease_acquire",
    "audit_query",
    "room_join",
    "session_heartbeat",
    "task_intake_submit",
]


def _tool(name: str):
    tool = mcp_server.mcp._tool_manager.get_tool(name)
    assert tool is not None, name
    return tool


def _scalar_types(schema):
    direct = schema.get("type")
    if isinstance(direct, str):
        return [direct]
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            return [item.get("type") for item in variants if isinstance(item, dict)]
    return []


@pytest.mark.parametrize(("tool_name", "argument"), CLEARED_OPTIONAL_SCALARS)
def test_clearing_an_optional_scalar_is_the_same_as_not_sending_it(tool_name, argument):
    tool = _tool(tool_name)
    arguments = {argument: ""}

    assert argument not in normalize_mcp_arguments(arguments, tool.parameters)
    assert normalize_mcp_arguments({}, tool.parameters) == normalize_mcp_arguments(
        {argument: "   "}, tool.parameters
    )


@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_no_write_tool_fails_on_a_cleared_optional_scalar(tool_name):
    tool = _tool(tool_name)
    required = set(tool.parameters.get("required", []))

    for name, schema in tool.parameters["properties"].items():
        if name in required or not isinstance(schema, dict):
            continue
        types = _scalar_types(schema)
        if not {"integer", "number", "boolean", "object"} & set(types):
            continue
        normalized = normalize_mcp_arguments({name: ""}, tool.parameters)
        assert name not in normalized or normalized[name] == ""


def test_nullable_scalar_without_a_default_still_becomes_none():
    schema = {
        "type": "object",
        "properties": {
            "level": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": ["level"],
    }

    assert normalize_mcp_arguments({"level": ""}, schema) == {"level": None}
    assert coerce_schema_value("", {"type": ["number", "null"]}, field="ratio") is None


def test_cleared_required_scalar_reports_a_structured_action():
    schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "required": ["limit"],
    }

    with pytest.raises(DomainError) as captured:
        normalize_mcp_arguments({"limit": ""}, schema)

    error = captured.value
    assert error.code == "invalid_mcp_argument"
    assert error.status_code == 400
    assert error.details["required_action"]
    assert error.details["field"] == "limit"
    assert error.as_dict()["error"]["details"]["required_action"]


def test_nested_cleared_scalar_keeps_the_empty_argument_error():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"exit_code": {"type": "integer"}},
            "required": ["exit_code"],
        },
    }

    with pytest.raises(DomainError) as captured:
        coerce_schema_value([{"exit_code": ""}], schema, field="tests")

    assert captured.value.details["field"] == "tests[0].exit_code"
    assert "required_action" in captured.value.details


def test_blank_arrays_and_strings_keep_their_existing_meaning():
    tool = _tool("message_post")
    mentions_schema = tool.parameters["properties"]["mentions"]
    assert coerce_schema_value("  ", mentions_schema, field="mentions") == []
    assert normalize_mcp_arguments({"mentions": ""}, tool.parameters) == {
        "mentions": []
    }

    assert normalize_mcp_arguments({"body": ""}, tool.parameters) == {"body": ""}

    # An empty string is a value for a string argument, nullable or not.
    nullable_text = {"type": "object", "properties": {"note": {
        "anyOf": [{"type": "string"}, {"type": "null"}]}}}
    assert normalize_mcp_arguments({"note": ""}, nullable_text) == {"note": ""}


def test_malformed_non_blank_values_still_raise_tool_error():
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        coerce_schema_value("0.0", {"type": "integer"}, field="exit_code")
    with pytest.raises(ToolError):
        coerce_schema_value("maybe", {"type": "boolean"}, field="requires_ack")
