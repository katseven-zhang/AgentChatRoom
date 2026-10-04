"""The Bridge may only auto-scope tools the server actually replays.

A retry after a lost response is the only thing that makes a Bridge write
duplicate, and the retry reuses one prepared argument set. A tool missing from
the auto-idempotency list therefore re-issued its write on every reconnect.
"""

from __future__ import annotations

import pytest

from agentchatroom import mcp_server
from agentchatroom.mcp_bridge import (
    AUTO_IDEMPOTENCY_EXCLUDED_TOOL_NAMES,
    AUTO_IDEMPOTENCY_TOOL_NAMES,
    BRIDGE_INSTANCE_ID,
    prepare_tool_arguments,
)

NEWLY_COVERED = [
    "task_intake_submit",
    "task_intake_acknowledge",
    "task_intake_reassign",
    "task_define_from_intake",
    "project_document_upsert",
    "member_create",
    "member_update",
    "member_revoke",
]


def _tools_advertising_request_id() -> set[str]:
    manager = mcp_server.mcp._tool_manager
    return {
        tool.name
        for tool in manager.list_tools()
        if "request_id" in (tool.parameters.get("properties") or {})
    }


def test_every_replayable_tool_is_auto_scoped():
    unscoped = _tools_advertising_request_id() - set(AUTO_IDEMPOTENCY_TOOL_NAMES)

    assert unscoped == set(AUTO_IDEMPOTENCY_EXCLUDED_TOOL_NAMES)
    assert "session_heartbeat" in unscoped, "presence must keep replaying live"


def test_no_auto_scoped_name_lacks_server_side_support():
    # Injecting request_id into a tool that does not take it fails the call.
    assert set(AUTO_IDEMPOTENCY_TOOL_NAMES) <= _tools_advertising_request_id()


@pytest.mark.parametrize("tool_name", NEWLY_COVERED)
def test_newly_covered_writes_get_a_bridge_scoped_identity(tool_name):
    prepared = prepare_tool_arguments(tool_name, {"project_id": "p1"})

    assert prepared["request_id"].startswith(f"bridge:{BRIDGE_INSTANCE_ID}:")
    assert prepared["project_id"] == "p1"


@pytest.mark.parametrize("tool_name", NEWLY_COVERED)
def test_a_retry_of_one_logical_call_reuses_the_prepared_identity(tool_name):
    # The retry loop forwards the same prepared mapping, never a fresh one.
    prepared = prepare_tool_arguments(tool_name, {})
    assert prepare_tool_arguments(tool_name, prepared)["request_id"] == (
        prepared["request_id"]
    )


def test_caller_supplied_identity_is_kept_and_scoped():
    prepared = prepare_tool_arguments("task_intake_submit", {"request_id": "abc"})

    assert prepared["request_id"] == f"bridge:{BRIDGE_INSTANCE_ID}:abc"


@pytest.mark.parametrize("tool_name", ["room_sync", "task_get", "session_heartbeat"])
def test_reads_and_presence_are_not_touched(tool_name):
    assert prepare_tool_arguments(tool_name, {"project_id": "p1"}) == {
        "project_id": "p1"
    }


def _as_management(session) -> dict:
    """Credentials plus explicit management authority (#252)."""
    return {
        "session_id": session["agent"]["id"],
        "token": session["token"],
        "management_authorized": True,
    }


def test_document_upsert_replays_instead_of_bumping_a_second_version(
    service, project
):
    manager = service.join_room(
        project["id"],
        software_key="bridge-mgr",
        name="BridgeMgr",
        client="pytest",
        model="unknown",
    )
    call = dict(
        doc_key="bridge-replay",
        kind="reference",
        title="Replay",
        content="body",
        **_as_management(manager),
    )
    first = service.upsert_project_document(
        project["id"], request_id="req-doc-1", **call
    )
    replay = service.upsert_project_document(
        project["id"], request_id="req-doc-1", **call
    )
    another = service.upsert_project_document(
        project["id"], request_id="req-doc-2", **{**call, "content": "changed"}
    )

    assert replay["idempotent_replay"] is True
    assert replay["document"] == first["document"]
    assert another["document"]["current_version"] == first["document"]["current_version"] + 1


def test_intake_submit_replays_instead_of_queueing_twice(service, project, joined_agents):
    owner, _ = joined_agents
    call = dict(
        raw_description="Bridge replay probe",
        target_member_id=owner["agent"]["member_id"],
        created_by_session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    first = service.submit_task_intake(
        project["id"], request_id="req-intake-1", **call
    )
    replay = service.submit_task_intake(
        project["id"], request_id="req-intake-1", **call
    )
    listed = service.list_task_intakes(project["id"])

    assert replay["intake"]["id"] == first["intake"]["id"]
    assert len([item for item in listed if item["id"] == first["intake"]["id"]]) == 1
