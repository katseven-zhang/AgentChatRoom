from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from agentchatroom import mcp_bridge, mcp_server
from agentchatroom.api import create_app
from agentchatroom.config import Settings
from agentchatroom.database import Database
from agentchatroom.mcp_bridge import (
    BridgeSettings,
    ReconnectingUpstream,
    prepare_tool_arguments,
)
from agentchatroom.mcp_compat import coerce_schema_value
from agentchatroom.mcp_compat import coerce_schema_value


def test_mcp_exposes_standard_tool_set():
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert names == {
        "room_join",
        "room_sync",
        "session_heartbeat",
        "session_leave",
        "message_post",
        "message_acknowledge",
        "task_list",
        "task_get",
        "audit_query",
        "member_list",
        "member_create",
        "member_update",
        "member_revoke",
        "task_create",
        "task_claim",
        "task_assign",
        "task_acknowledge",
        "task_handoff",
        "task_handoff_acknowledge",
        "task_update",
        "lease_acquire",
        "lease_release",
        "work_report",
        "review_submit",
        "integration_submit",
        "knowledge_candidate_submit",
        "knowledge_review",
        "knowledge_supersede",
        "knowledge_archive",
        "knowledge_get",
        "knowledge_list",
    }


def test_bridge_adds_request_id_only_to_retryable_cached_writes():
    original = {"project_id": "project_1", "body": "hello"}
    forwarded = prepare_tool_arguments("message_post", original)

    assert original == {"project_id": "project_1", "body": "hello"}
    assert forwarded["request_id"].startswith("request_")
    assert prepare_tool_arguments("message_post", forwarded) == forwarded
    assert prepare_tool_arguments("room_sync", original) == original
    assert prepare_tool_arguments("session_heartbeat", original) == original
    explicit_heartbeat = {**original, "request_id": "heartbeat-retry-1"}
    assert (
        prepare_tool_arguments("session_heartbeat", explicit_heartbeat)
        == explicit_heartbeat
    )


def test_mcp_compatibility_unwraps_single_item_array_wrapper():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "exit_code": {"type": "integer"},
            },
        },
    }

    assert coerce_schema_value(
        {"item": {"command": "pytest", "exit_code": "0"}},
        schema,
        field="tests",
    ) == [{"command": "pytest", "exit_code": 0}]


def test_mcp_evidence_tools_publish_nested_scalar_schema():
    work_report = mcp_server.mcp._tool_manager.get_tool("work_report")
    integration = mcp_server.mcp._tool_manager.get_tool("integration_submit")

    work_item = work_report.parameters["$defs"]["TestEvidence"]
    integration_item = integration.parameters["$defs"]["TestEvidence"]
    assert work_item["properties"]["exit_code"]["type"] == "integer"
    assert integration_item["properties"]["exit_code"]["type"] == "integer"


def test_mcp_message_post_requires_message_level_model_provenance():
    message_post = mcp_server.mcp._tool_manager.get_tool("message_post")

    assert "model_display_name" in message_post.parameters["required"]
    assert message_post.parameters["properties"]["model_display_name"]["type"] == "string"


def test_mcp_room_join_requires_project_path_and_model_only():
    room_join = mcp_server.mcp._tool_manager.get_tool("room_join")

    assert "project_path" in room_join.parameters["required"]
    assert "model" in room_join.parameters["required"]
    assert "agent_key" not in room_join.parameters["required"]


def test_mcp_project_member_tools_share_the_domain_service(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    service.create_project(root_path=str(project_dir))
    joined = mcp_server.room_join(
        project_path=str(project_dir),
        model="unknown",
        agent_key="member-admin-main",
        agent_name="Member Admin",
        client="codex",
        role="coordinator",
    )["result"]
    project_id = joined["project"]["id"]
    created = mcp_server.member_create(
        project_id,
        "codex-main",
        "Codex Main",
        role="coordinator",
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    assert created["ok"] is True
    member_id = created["result"]["member"]["id"]
    listed = mcp_server.member_list(project_id)
    assert listed["ok"] is True
    assert any(item["id"] == member_id for item in listed["result"])
    updated = mcp_server.member_update(
        project_id,
        member_id,
        status="suspended",
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    assert updated["result"]["member"]["status"] == "suspended"
    revoked = mcp_server.member_revoke(
        project_id,
        member_id,
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    assert revoked["result"]["member"]["status"] == "revoked"


def test_mcp_compatibility_unwraps_single_item_array_wrapper():
    schema = {"type": "array", "items": {"type": "object"}}

    assert coerce_schema_value(
        {"item": {"command": "pytest", "exit_code": 0}},
        schema,
        field="tests",
    ) == [{"command": "pytest", "exit_code": 0}]


def test_bridge_error_classifier_unwraps_groups_and_rejects_auth_failures():
    request = mcp_bridge.httpx.Request("POST", "http://127.0.0.1:8765/mcp")
    grouped_disconnect = ExceptionGroup(
        "transport task group",
        [mcp_bridge.httpx.ConnectError("connection refused", request=request)],
    )
    unauthorized = mcp_bridge.httpx.HTTPStatusError(
        "unauthorized",
        request=request,
        response=mcp_bridge.httpx.Response(401, request=request),
    )

    assert mcp_bridge.retryable_bridge_error(grouped_disconnect) is True
    assert mcp_bridge.retryable_bridge_error(unauthorized) is False


@pytest.mark.asyncio
async def test_bridge_presence_tracks_joined_session_and_sends_independent_heartbeat():
    calls = []

    class FakeUpstream:
        async def call_tool(self, name, arguments):
            calls.append((name, dict(arguments)))
            return mcp_bridge.types.CallToolResult(
                content=[
                    mcp_bridge.types.TextContent(
                        type="text",
                        text=json.dumps({"ok": True, "result": {"status": "idle"}}),
                    )
                ]
            )

    manager = mcp_bridge.BridgePresenceManager(
        FakeUpstream(),
        BridgeSettings(
            server_url="http://127.0.0.1:8765/mcp",
            presence_keepalive_interval_seconds=1,
        ),
    )
    joined_result = mcp_bridge.types.CallToolResult(
        content=[
            mcp_bridge.types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "project": {"id": "project_1"},
                            "agent": {"id": "agent_1"},
                            "token": "session-secret",
                        },
                    }
                ),
            )
        ]
    )
    manager.observe("room_join", {}, joined_result)

    await manager.heartbeat_once()

    assert calls[0][0] == "session_heartbeat"
    assert calls[0][1]["project_id"] == "project_1"
    assert calls[0][1]["session_id"] == "agent_1"
    assert "status" not in calls[0][1]
    assert "request_id" not in calls[0][1]

    manager.observe(
        "session_leave",
        {"session_id": "agent_1"},
        mcp_bridge.types.CallToolResult(
            content=[
                mcp_bridge.types.TextContent(
                    type="text", text=json.dumps({"ok": True, "result": {}})
                )
            ]
        ),
    )
    assert manager.sessions == {}


@pytest.mark.asyncio
async def test_bridge_retries_transient_call_with_the_same_request_id(monkeypatch):
    call_arguments = []
    connect_attempts = 0

    class FakeStream:
        async def __aenter__(self):
            nonlocal connect_attempts
            connect_attempts += 1
            return object(), object(), None

        async def __aexit__(self, *_args):
            return False

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _name, arguments):
            call_arguments.append(dict(arguments))
            if len(call_arguments) == 1:
                raise mcp_bridge.httpx.ReadError("connection closed")
            return "ok"

    monkeypatch.setattr(
        mcp_bridge,
        "streamable_http_client",
        lambda *_args, **_kwargs: FakeStream(),
    )
    monkeypatch.setattr(mcp_bridge, "ClientSession", FakeSession)

    forwarded = prepare_tool_arguments(
        "message_post",
        {"project_id": "project_1", "body": "retry once"},
    )
    upstream = ReconnectingUpstream(
        BridgeSettings(
            server_url="http://127.0.0.1:8765/mcp",
            retry_attempts=2,
            retry_backoff_seconds=0,
        ),
        http_client=None,
    )

    result = await upstream.call_tool("message_post", forwarded)

    assert result == "ok"
    assert connect_attempts == 2
    assert len(call_arguments) == 2
    assert call_arguments[0]["request_id"] == forwarded["request_id"]
    assert call_arguments[1]["request_id"] == forwarded["request_id"]


@pytest.mark.asyncio
async def test_bridge_does_not_retry_domain_tool_errors(monkeypatch):
    call_count = 0

    class FakeStream:
        async def __aenter__(self):
            return object(), object(), None

        async def __aexit__(self, *_args):
            return False

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _name, _arguments):
            nonlocal call_count
            call_count += 1
            raise ToolError("domain validation failed")

    monkeypatch.setattr(
        mcp_bridge,
        "streamable_http_client",
        lambda *_args, **_kwargs: FakeStream(),
    )
    monkeypatch.setattr(mcp_bridge, "ClientSession", FakeSession)
    upstream = ReconnectingUpstream(
        BridgeSettings(
            server_url="http://127.0.0.1:8765/mcp",
            retry_attempts=3,
            retry_backoff_seconds=0,
        ),
        http_client=None,
    )

    with pytest.raises(ToolError, match="domain validation failed"):
        await upstream.call_tool("message_post", {"request_id": "request_fixed"})

    assert call_count == 1


def test_mcp_request_id_replays_and_session_can_leave(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    service.create_project(root_path=str(project_dir))
    joined = mcp_server.room_join(
        project_path=str(project_dir),
        model="test-model",
        agent_key="reliable-mcp-main",
        agent_name="Reliable MCP",
        client="grok-build",
        role="executor",
    )["result"]
    project_id = joined["project"]["id"]
    first = mcp_server.message_post(
        project_id,
        joined["agent"]["id"],
        joined["token"],
        "Exactly once",
        "Grok Build UI Model",
        request_id="mcp-message-1",
    )
    replay = mcp_server.message_post(
        project_id,
        joined["agent"]["id"],
        joined["token"],
        "Exactly once",
        "Grok Build UI Model",
        request_id="mcp-message-1",
    )

    assert first["ok"] is True
    assert replay["result"]["event_id"] == first["result"]["event_id"]
    assert replay["result"]["idempotent_replay"] is True

    with service.database.connect() as connection:
        cursor_before_heartbeat = service.latest_cursor(connection, project_id)
    heartbeats = [
        mcp_server.session_heartbeat(
            project_id,
            joined["agent"]["id"],
            joined["token"],
        )
        for _ in range(5)
    ]
    heartbeat = heartbeats[-1]
    assert heartbeat["ok"] is True
    assert heartbeat["result"]["status"] == "online"
    with service.database.connect() as connection:
        assert service.latest_cursor(connection, project_id) == cursor_before_heartbeat
        uncached_heartbeat_count = connection.execute(
            "SELECT COUNT(*) FROM idempotency_records WHERE operation = ?",
            ("session.heartbeat",),
        ).fetchone()[0]
    assert uncached_heartbeat_count == 0

    explicit = mcp_server.session_heartbeat(
        project_id,
        joined["agent"]["id"],
        joined["token"],
        request_id="mcp-heartbeat-1",
    )
    explicit_replay = mcp_server.session_heartbeat(
        project_id,
        joined["agent"]["id"],
        joined["token"],
        request_id="mcp-heartbeat-1",
    )
    assert explicit["result"]["idempotent_replay"] is False
    assert explicit_replay["result"]["idempotent_replay"] is True
    with service.database.connect() as connection:
        cached_heartbeat_count = connection.execute(
            "SELECT COUNT(*) FROM idempotency_records WHERE operation = ?",
            ("session.heartbeat",),
        ).fetchone()[0]
    assert cached_heartbeat_count == 1

    left = mcp_server.session_leave(
        project_id,
        joined["agent"]["id"],
        joined["token"],
        request_id="mcp-leave-1",
    )
    assert left["ok"] is True
    assert left["result"]["agent"]["status"] == "offline"


def test_mcp_handoff_and_integration_share_the_domain_service(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    service.create_project(root_path=str(project_dir))
    executor = mcp_server.room_join(
        project_path=str(project_dir), model="unknown", agent_key="executor-main",
        agent_name="Executor", client="codex", role="executor"
    )["result"]
    successor = mcp_server.room_join(
        project_path=str(project_dir),
        model="test-model",
        agent_key="successor-main",
        agent_name="Successor",
        client="grok-build",
        role="executor",
    )["result"]
    reviewer = mcp_server.room_join(
        project_path=str(project_dir), model="unknown", agent_key="reviewer-main",
        agent_name="Reviewer", client="trae", role="reviewer"
    )["result"]
    project_id = executor["project"]["id"]
    created = mcp_server.task_create(
        project_id,
        "MCP lifecycle",
        ["Lifecycle is complete"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["result"]
    task_id = created["task"]["id"]
    assert mcp_server.task_claim(
        project_id,
        task_id,
        executor["agent"]["id"],
        executor["token"],
    )["ok"] is True

    requested = mcp_server.task_handoff(
        project_id,
        task_id,
        executor["agent"]["id"],
        executor["token"],
        successor["agent"]["id"],
        "Investigation complete",
        "Implement the change",
        completed_items=["Investigation"],
        pending_items=["Implementation"],
        files=["src/mcp_lifecycle.py"],
    )
    assert requested["ok"] is True
    acknowledged = mcp_server.task_handoff_acknowledge(
        project_id,
        task_id,
        requested["result"]["handoff"]["id"],
        successor["agent"]["id"],
        successor["token"],
        "accepted",
    )
    assert acknowledged["result"]["task"]["owner_session_id"] == successor[
        "agent"
    ]["id"]

    reported = mcp_server.work_report(
        project_id,
        task_id,
        successor["agent"]["id"],
        successor["token"],
        "Implemented MCP lifecycle",
        ["src/mcp_lifecycle.py"],
        [{"command": "pytest mcp", "exit_code": 0}],
    )
    assert reported["ok"] is True
    reviewed = mcp_server.review_submit(
        project_id,
        task_id,
        reviewer["agent"]["id"],
        reviewer["token"],
        "approved",
        [{"criterion": "Lifecycle is complete", "status": "passed"}],
    )
    assert reviewed["result"]["verification_status"] == "approved"
    post_review_handoff = mcp_server.task_handoff(
        project_id,
        task_id,
        successor["agent"]["id"],
        successor["token"],
        executor["agent"]["id"],
        "Independent review approved the implementation",
        "Run final integration tests",
        pending_items=["Integration"],
    )
    assert post_review_handoff["ok"] is True
    post_review_ack = mcp_server.task_handoff_acknowledge(
        project_id,
        task_id,
        post_review_handoff["result"]["handoff"]["id"],
        executor["agent"]["id"],
        executor["token"],
        "accepted",
    )
    assert post_review_ack["result"]["task"]["execution_status"] == "completed"
    assert post_review_ack["result"]["task"]["verification_status"] == "approved"
    integrated = mcp_server.integration_submit(
        project_id,
        task_id,
        executor["agent"]["id"],
        executor["token"],
        "done",
        "Merged and tested",
        [{"command": "pytest mcp", "exit_code": 0}],
        files=["src/mcp_lifecycle.py"],
    )
    assert integrated["ok"] is True
    assert integrated["result"]["task"]["integration_status"] == "done"


def test_mcp_command_help_does_not_initialize_storage(tmp_path):
    data_dir = tmp_path / "unused-data"
    environment = os.environ.copy()
    environment["AGENTCHATROOM_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        [sys.executable, "-m", "agentchatroom.mcp_server", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "Run the AgentChatRoom MCP server over stdio" in result.stdout
    assert not data_dir.exists()


def test_mcp_room_join_generates_the_database_identity(monkeypatch, service, project_dir):
    monkeypatch.setattr(mcp_server, "service", service)
    response = mcp_server.room_join(
        project_path=str(project_dir),
        model="local-model",
        agent_key="fourth-agent-main",
        agent_name="Fourth Agent",
        client="custom-client",
        role="specialist",
        capabilities={"mcp": True, "notifications": False},
    )

    assert response["ok"] is True
    assert response["result"]["event_id"] == response["result"]["cursor"]
    agent = response["result"]["agent"]
    assert agent["agent_key"] == agent["member_id"]
    assert response["result"]["identity"]["member_key"] == "software:custom-client"
    assert agent["name"] == "Fourth Agent"
    assert agent["client"] == "custom-client"
    assert agent["role"] == "specialist"
    assert len(service.list_projects()) == 1


def test_mcp_configured_software_identity_ignores_task_aliases(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.setenv(mcp_server.SOFTWARE_KEY_ENV, "codex")
    monkeypatch.setenv(mcp_server.SOFTWARE_NAME_ENV, "Codex")
    monkeypatch.setenv(mcp_server.SOFTWARE_CLIENT_ENV, "codex")

    first = mcp_server.room_join(
        project_path=str(project_dir),
        model="unknown",
        agent_key="runtime-check-main",
        agent_name="Runtime Check",
        client="validation",
    )["result"]
    second = mcp_server.room_join(
        project_path=str(project_dir),
        model="unknown",
        agent_key="codex-review-main",
        agent_name="Codex Review",
        client="reviewer",
        role="reviewer",
    )["result"]

    assert first["agent"]["member_id"] == second["agent"]["member_id"]
    assert second["agent"]["name"] == "Codex"
    assert second["agent"]["client"] == "codex"
    identities = service.snapshot(second["project"]["id"])["agent_identities"]
    assert len(identities) == 1
    assert identities[0]["active_session_count"] == 1


def test_mcp_room_join_uses_unique_existing_room_without_project_key(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    existing = service.create_project(root_path=str(project_dir))

    response = mcp_server.room_join(
        project_path=str(project_dir),
        model="unknown",
        agent_key="trae-main",
        agent_name="Trae",
        client="trae",
    )

    assert response["ok"] is True
    assert response["result"]["project"]["id"] == existing["id"]
    assert response["result"]["project"]["project_key"] == existing["project_key"]
    assert len(service.list_projects()) == 1


@pytest.mark.asyncio
async def test_mcp_compatibility_is_schema_directed(monkeypatch, service, project_dir):
    monkeypatch.setattr(mcp_server, "service", service)
    service.create_project(root_path=str(project_dir))
    joined = await mcp_server.mcp._tool_manager.call_tool(
        "room_join",
        {
            "project_path": str(project_dir),
            "agent_key": "string-wrapped-main",
            "agent_name": "String Wrapped Agent",
            "client": "grok-build",
            "model": "test-model",
            "capabilities": '{"mcp":true,"python":true}',
        },
    )
    project_id = joined["result"]["project"]["id"]
    session_id = joined["result"]["agent"]["id"]
    token = joined["result"]["token"]

    created = await mcp_server.mcp._tool_manager.call_tool(
        "task_create",
        {
            "project_id": project_id,
            "title": "123",
            "acceptance_criteria": '["Compatibility works"]',
            "depends_on": "[]",
            "priority": "1",
            "actor_session_id": session_id,
            "token": token,
        },
    )
    assert created["ok"] is True
    assert created["result"]["task"]["title"] == "123"
    assert created["result"]["task"]["priority"] == 1
    assert created["result"]["task"]["acceptance_criteria"] == [
        "Compatibility works"
    ]

    fetched = await mcp_server.mcp._tool_manager.call_tool(
        "task_get",
        {"project_id": project_id, "task_id": created["result"]["task"]["id"]},
    )
    assert fetched["ok"] is True
    assert fetched["result"]["id"] == created["result"]["task"]["id"]
    assert fetched["result"]["title"] == "123"

    posted = await mcp_server.mcp._tool_manager.call_tool(
        "message_post",
        {
            "project_id": project_id,
            "session_id": session_id,
            "token": token,
            "body": "123",
            "model_display_name": "Compatibility Model",
            "mentions": "[]",
            "files": "",
            "requires_ack": "true",
            "priority": "1",
        },
    )
    assert posted["ok"] is True
    message_event = next(
        event
        for event in service.list_events(project_id, after=0)["events"]
        if event["id"] == posted["result"]["event_id"]
    )
    assert message_event["payload"]["body"] == "123"
    assert message_event["payload"]["model_display_name"] == "Compatibility Model"
    assert message_event["payload"]["requires_ack"] is True
    assert message_event["payload"]["priority"] == 1

    claimed = await mcp_server.mcp._tool_manager.call_tool(
        "task_claim",
        {
            "project_id": project_id,
            "task_id": created["result"]["task"]["id"],
            "session_id": session_id,
            "token": token,
        },
    )
    assert claimed["ok"] is True

    reported = await mcp_server.mcp._tool_manager.call_tool(
        "work_report",
        {
            "project_id": project_id,
            "task_id": created["result"]["task"]["id"],
            "session_id": session_id,
            "token": token,
            "summary": "Read-only compatibility investigation complete",
            "files": "[]",
            "tests": '[{"command":"Manual evidence review","exit_code":0}]',
            "no_code_change_reason": "No repository changes were required",
        },
    )
    assert reported["ok"] is True
    assert reported["result"]["no_code_change_reason"] == (
        "No repository changes were required"
    )
    report = service.snapshot(project_id)["reports"][0]
    assert report["files"] == []
    assert report["no_code_change_reason"] == "No repository changes were required"

    with pytest.raises(ToolError, match="boolean literal true or false"):
        await mcp_server.mcp._tool_manager.call_tool(
            "message_post",
            {
                "project_id": project_id,
                "session_id": session_id,
                "token": token,
                "body": "Invalid boolean",
                "model_display_name": "Compatibility Model",
                "requires_ack": "yes",
            },
        )


@pytest.mark.asyncio
async def test_mcp_stdio_round_trip(tmp_path, project_dir):
    environment = os.environ.copy()
    data_dir = tmp_path / "mcp-data"
    environment["AGENTCHATROOM_DATA_DIR"] = str(data_dir)
    bootstrap_settings = Settings(data_dir=data_dir)
    bootstrap_service = mcp_server.AgentChatRoomService(
        Database(bootstrap_settings.database_path), bootstrap_settings
    )
    bootstrap_service.initialize()
    bootstrap_service.create_project(root_path=str(project_dir))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agentchatroom.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    async def exchange(message: dict):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        except TimeoutError:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
            stderr = await process.stderr.read()
            pytest.fail(f"MCP response timed out: {stderr.decode(errors='replace')}")
        assert line, (await process.stderr.read()).decode(errors="replace")
        return json.loads(line)

    try:
        initialized = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            }
        )
        assert initialized["result"]["serverInfo"]["name"] == "AgentChatRoom"

        process.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
        )
        await process.stdin.drain()

        tools = await exchange(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        assert {tool["name"] for tool in tools["result"]["tools"]} >= {
            "room_join",
            "room_sync",
        }

        joined = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "room_join",
                    "arguments": {
                        "project_path": str(project_dir),
                        "agent_key": "protocol-agent-main",
                        "agent_name": "Protocol Agent",
                        "client": "generic-mcp-client",
                        "model": "unknown",
                        "role": "specialist",
                    },
                },
            }
        )
        payload = json.loads(joined["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["result"]["agent"]["client"] == "generic-mcp-client"
        assert payload["result"]["token"]

    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)


def test_streamable_http_mcp_uses_fastapi_domain_service(settings, project_dir):
    app = create_app(settings)
    project = app.state.service.create_project(root_path=str(project_dir))
    credential = app.state.service.issue_agent_token(
        project["id"], name="HTTP MCP test"
    )
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "host": f"127.0.0.1:{settings.port}",
        "authorization": f"Bearer {credential['token']}",
    }

    def rpc_payload(response):
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            data_line = next(
                line
                for line in response.text.splitlines()
                if line.startswith("data: ")
            )
            return json.loads(data_line.removeprefix("data: "))
        return response.json()

    with TestClient(app) as client:
        unauthorized = client.post(
            settings.mcp_http_path,
            headers={key: value for key, value in headers.items() if key != "authorization"},
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "unauthorized", "version": "1"},
                },
            },
        )
        assert unauthorized.status_code == 401

        initialized = client.post(
            settings.mcp_http_path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "http-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        assert rpc_payload(initialized)["result"]["serverInfo"]["name"] == "AgentChatRoom"

        session_headers = {
            **headers,
            "mcp-session-id": session_id,
            "mcp-protocol-version": "2025-06-18",
        }
        notification = client.post(
            settings.mcp_http_path,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notification.status_code == 202

        joined = client.post(
            settings.mcp_http_path,
            headers=session_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "room_join",
                    "arguments": {
                        "project_path": str(project_dir),
                        "agent_key": "remote-mcp-main",
                        "agent_name": "Remote MCP Agent",
                        "client": "streamable-http-test",
                        "model": "unknown",
                        "host_key": "http-test-host",
                        "host_name": "HTTP Test Host",
                    },
                },
            },
        )
        assert joined.status_code == 200
        tool_result = rpc_payload(joined)["result"]
        payload = json.loads(tool_result["content"][0]["text"])
        project_id = payload["result"]["project"]["id"]
        assert project_id == project["id"]
        snapshot = client.get(f"/api/v1/projects/{project_id}/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["agents"][0]["client"] == "streamable-http-test"


@pytest.mark.asyncio
async def test_stdio_bridge_forwards_to_central_http_mcp(settings, project_dir):
    app = create_app(settings)
    project = app.state.service.create_project(root_path=str(project_dir))
    credential = app.state.service.issue_agent_token(
        project["id"], name="Bridge test"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started

    environment = os.environ.copy()
    environment["AGENTCHATROOM_AGENT_TOKEN"] = credential["token"]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agentchatroom.mcp_bridge",
        "--url",
        f"http://127.0.0.1:{port}{settings.mcp_http_path}",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    async def exchange(message: dict):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        except TimeoutError:
            stderr = await process.stderr.read()
            pytest.fail(f"Bridge response timed out: {stderr.decode(errors='replace')}")
        assert line, (await process.stderr.read()).decode(errors="replace")
        return json.loads(line)

    try:
        initialized = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "bridge-test", "version": "1"},
                },
            }
        )
        assert initialized["result"]["serverInfo"]["name"] == "AgentChatRoom Bridge"
        process.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
        )
        await process.stdin.drain()

        tools = await exchange(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        assert {tool["name"] for tool in tools["result"]["tools"]} >= {
            "room_join",
            "task_assign",
        }

        joined = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "room_join",
                    "arguments": {
                        "project_path": str(project_dir),
                        "agent_key": "bridge-agent-main",
                        "agent_name": "Bridge Agent",
                        "client": "stdio-bridge-test",
                        "model": "unknown",
                        "host_key": "bridge-test-host",
                        "host_name": "Bridge Test Host",
                    },
                },
            }
        )
        payload = json.loads(joined["result"]["content"][0]["text"])
        project_id = payload["result"]["project"]["id"]
        assert project_id == project["id"]
        snapshot = app.state.service.snapshot(project_id)
        assert snapshot["agents"][0]["client"] == "stdio-bridge-test"
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)


@pytest.mark.asyncio
async def test_stdio_bridge_reconnects_after_center_restart_without_duplicate_write(
    settings, project_dir
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    async def start_center(app):
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        task = asyncio.create_task(server.serve())
        deadline = asyncio.get_running_loop().time() + 10
        while not server.started and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert server.started
        async with httpx.AsyncClient() as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(
                        f"http://127.0.0.1:{port}/health",
                        timeout=0.5,
                    )
                    if response.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("center health endpoint did not become ready")
        return server, task

    async def stop_center(server, task):
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)

    app = create_app(settings)
    project = app.state.service.create_project(root_path=str(project_dir))
    credential = app.state.service.issue_agent_token(
        project["id"], name="Restart bridge test"
    )
    server, server_task = await start_center(app)

    environment = os.environ.copy()
    environment["AGENTCHATROOM_AGENT_TOKEN"] = credential["token"]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
            "-m",
            "agentchatroom.mcp_bridge",
            "--url",
            f"http://127.0.0.1:{port}{settings.mcp_http_path}",
            "--retry-attempts",
            "10",
            "--retry-backoff",
            "0.1",
            "--timeout",
            "2",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    restarted_server = None
    restarted_task = None

    async def exchange(message: dict, timeout: float = 15):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except TimeoutError:
            try:
                stderr = await asyncio.wait_for(process.stderr.read(4096), timeout=0.2)
            except TimeoutError:
                stderr = b""
            pytest.fail(
                f"Bridge response timed out: {stderr.decode(errors='replace')}"
            )
        assert line, f"Bridge exited with code {process.returncode}"
        return json.loads(line)

    try:
        initialized = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "restart-bridge-test", "version": "1"},
                },
            }
        )
        assert initialized["result"]["serverInfo"]["name"] == "AgentChatRoom Bridge"
        process.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
        )
        await process.stdin.drain()

        joined = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "room_join",
                    "arguments": {
                        "project_path": str(project_dir),
                        "agent_key": "restart-bridge-main",
                        "agent_name": "Restart Bridge Agent",
                        "client": "stdio-bridge-restart-test",
                        "model": "unknown",
                        "host_key": "restart-bridge-host",
                        "host_name": "Restart Bridge Host",
                    },
                },
            }
        )
        joined_payload = json.loads(joined["result"]["content"][0]["text"])["result"]
        session_id = joined_payload["agent"]["id"]
        session_token = joined_payload["token"]
        request_id = "bridge-center-restart-message-1"
        arguments = {
            "project_id": project["id"],
            "session_id": session_id,
            "token": session_token,
            "body": "Exactly once across center restart",
            "model_display_name": "Restart Bridge Model",
            "request_id": request_id,
        }

        first = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "message_post", "arguments": arguments},
            }
        )
        first_payload = json.loads(first["result"]["content"][0]["text"])["result"]

        await stop_center(server, server_task)
        restarted_app = create_app(settings)
        restarted_server, restarted_task = await start_center(restarted_app)
        replay = await exchange(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "message_post", "arguments": arguments},
            }
        )
        replay_payload = json.loads(replay["result"]["content"][0]["text"])[
            "result"
        ]

        assert replay_payload["event_id"] == first_payload["event_id"]
        assert replay_payload["idempotent_replay"] is True
        events = restarted_app.state.service.list_events(project["id"], after=0)[
            "events"
        ]
        matching = [
            event
            for event in events
            if event["event_type"] == "message.message"
            and event["payload"]["body"] == "Exactly once across center restart"
        ]
        assert len(matching) == 1
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        if restarted_server is not None and restarted_task is not None:
            await stop_center(restarted_server, restarted_task)
        elif not server_task.done():
            await stop_center(server, server_task)
