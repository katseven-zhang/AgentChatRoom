from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentchatroom import mcp_server
from agentchatroom.api import create_app
from agentchatroom.bootstrap import (
    bootstrap_local_room,
    match_registered_projects,
)
from agentchatroom.project_registration import register_checkout_project


def _clear_http_mcp_context() -> None:
    mcp_server.clear_runtime_binding()
    mcp_server._bound_service_provider.set(None)
    mcp_server._bootstrap_workspace_roots.set(None)
    mcp_server._bootstrap_http_identity.set(None)
    try:
        from mcp.server.auth.middleware.auth_context import auth_context_var

        auth_context_var.set(None)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_http_mcp_context():
    _clear_http_mcp_context()
    yield
    _clear_http_mcp_context()


def _boot(service, *, key, name, client, **kwargs):
    return bootstrap_local_room(
        service,
        software_key=key,
        software_name=name,
        client=client,
        model="unknown",
        **kwargs,
    )


def test_db_root_match_does_not_need_checkout_file(service, project_dir):
    project = service.create_project(root_path=str(project_dir), name="HTTP Room")
    assert not (project_dir / ".agentchatroom" / "project.json").exists()
    matched = match_registered_projects(service, [project_dir])
    assert len(matched) == 1
    assert matched[0][0]["id"] == project["id"]
    outcome = _boot(
        service,
        key="http-agent",
        name="HTTP Agent",
        client="http-client",
        workspace_roots=[project_dir],
        cwd=None,
        explicit_project_path=None,
    )
    assert outcome.binding is not None
    assert outcome.binding.project_id == project["id"]
    assert outcome.public["status"] == "ready"


def test_db_match_succeeds_when_client_path_is_absent_on_server(service, tmp_path):
    real = tmp_path / "server-copy"
    real.mkdir()
    project = service.create_project(root_path=str(real), name="Cloud Room")
    ghost = tmp_path / "client-machine" / "workspace"
    assert not ghost.exists()
    service.register_workspace(
        project["id"],
        host_key="host:cloud-client",
        host_name="Cloud Client",
        local_path=str(ghost),
    )
    outcome = _boot(
        service,
        key="cloud-agent",
        name="Cloud Agent",
        client="http-client",
        workspace_roots=[ghost],
        cwd=None,
        explicit_project_path=None,
    )
    assert outcome.binding is not None
    assert outcome.binding.project_id == project["id"]
    assert outcome.public["project"]["id"] == project["id"]


def test_unregistered_roots_return_required_action(service, tmp_path):
    missing = tmp_path / "never-registered"
    missing.mkdir()
    outcome = _boot(
        service,
        key="lost-agent",
        name="Lost Agent",
        client="http-client",
        workspace_roots=[missing],
        cwd=None,
        explicit_project_path=None,
    )
    assert outcome.binding is None
    assert outcome.public["status"] == "project_not_registered"
    assert outcome.public["required_action"] == "create_or_open_project_in_web"


def test_dual_room_writes_do_not_leak(service, tmp_path):
    room_a = tmp_path / "room-a"
    room_b = tmp_path / "room-b"
    room_a.mkdir()
    room_b.mkdir()
    project_a = service.create_project(root_path=str(room_a), name="Room A")
    project_b = service.create_project(root_path=str(room_b), name="Room B")
    boot_a = _boot(
        service,
        key="agent-a",
        name="Agent A",
        client="client-a",
        workspace_roots=[room_a],
        cwd=None,
    )
    boot_b = _boot(
        service,
        key="agent-b",
        name="Agent B",
        client="client-b",
        workspace_roots=[room_b],
        cwd=None,
    )
    assert boot_a.binding is not None and boot_b.binding is not None
    assert boot_a.binding.project_id == project_a["id"]
    assert boot_b.binding.project_id == project_b["id"]

    posted = service.post_message(
        project_a["id"],
        body="secret-from-room-a",
        session_id=boot_a.binding.session_id,
        token=boot_a.binding.token,
        model_display_name="test-model",
    )
    task = service.create_task(
        project_a["id"],
        title="only-in-a",
        acceptance_criteria=["done"],
        actor_session_id=boot_a.binding.session_id,
        token=boot_a.binding.token,
    )
    lease = service.acquire_lease(
        project_a["id"],
        session_id=boot_a.binding.session_id,
        token=boot_a.binding.token,
        path_pattern="src/secret.py",
        reason="room-a-only",
    )
    snapshot_b = service.snapshot(project_b["id"])
    encoded_b = json.dumps(snapshot_b, ensure_ascii=False)
    assert "secret-from-room-a" not in encoded_b
    assert task["task"]["id"] not in {
        item["id"] for item in snapshot_b.get("tasks") or []
    }
    assert lease["lease"]["id"] not in {
        item["id"] for item in snapshot_b.get("leases") or []
    }
    events_b = service.list_events(project_b["id"])
    event_blob = json.dumps(events_b, ensure_ascii=False)
    assert "secret-from-room-a" not in event_blob
    assert posted["event_id"] not in {event["id"] for event in events_b["events"]}


def test_http_bound_unregistered_roots_include_http_action(monkeypatch, service, tmp_path):
    missing = tmp_path / "plain"
    missing.mkdir()
    provider = mcp_server._bound_service_provider.set(lambda: service)
    roots = mcp_server._bootstrap_workspace_roots.set([missing])
    identity = mcp_server._bootstrap_http_identity.set(
        ("http-agent", "HTTP Agent", "http-client")
    )
    monkeypatch.setattr(mcp_server, "service", service)
    try:
        result = mcp_server.room_bootstrap()
    finally:
        mcp_server._bound_service_provider.reset(provider)
        mcp_server._bootstrap_workspace_roots.reset(roots)
        mcp_server._bootstrap_http_identity.reset(identity)
    assert result["ok"] is False
    assert result["result"]["status"] == "project_not_registered"
    assert result["result"]["required_action"] == "create_or_open_project_in_web"
    assert "http_correct_action" in (result["error"].get("details") or {})
    assert "Web UI" in result["error"]["details"]["http_correct_action"]


def test_stdio_roots_still_outrank_cwd_and_env(service, project_dir, tmp_path, monkeypatch):
    register_checkout_project(
        project_dir, service.create_project(root_path=str(project_dir), name="Pinned")
    )
    other = tmp_path / "other"
    other.mkdir()
    other_project = service.create_project(root_path=str(other), name="Other")
    register_checkout_project(other, other_project)
    selected = _boot(
        service,
        key="stdio-agent",
        name="Stdio Agent",
        client="codex",
        workspace_roots=[other],
        cwd=project_dir,
        explicit_project_path=project_dir,
    )
    assert selected.binding is not None
    assert selected.binding.project_id == other_project["id"]


def _parse_tool(result) -> dict:
    for item in result.content:
        if isinstance(item, types.TextContent):
            return json.loads(item.text)
    raise AssertionError("tool result had no text payload")


@pytest.mark.asyncio
async def test_streamable_http_room_bootstrap_with_token_and_rejects_invalid(
    settings, project_dir
):
    app = create_app(settings)
    project = app.state.service.create_project(root_path=str(project_dir), name="HTTP Primary")
    credential = app.state.service.issue_agent_token(
        project["id"], name="HTTP bootstrap"
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
    url = f"http://127.0.0.1:{port}{settings.mcp_http_path}"

    async def list_roots(_context):
        return types.ListRootsResult(
            roots=[types.Root(uri=Path(project_dir).resolve().as_uri(), name="project")]
        )

    headers = {
        "Authorization": f"Bearer {credential['token']}",
        "X-AgentChatRoom-Software-Key": "http-bootstrap",
        "X-AgentChatRoom-Software-Name": "HTTP Bootstrap",
        "X-AgentChatRoom-Software-Client": "http-bootstrap",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_client:
            unauthorized = await httpx.AsyncClient(timeout=10.0).post(
                url,
                headers={
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "no-token", "version": "1"},
                    },
                },
            )
            assert unauthorized.status_code == 401

            async with streamable_http_client(url, http_client=http_client) as (
                read_stream,
                write_stream,
                _session_id,
            ):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    list_roots_callback=list_roots,
                    client_info=types.Implementation(name="http-bootstrap", version="1"),
                ) as session:
                    await session.initialize()
                    payload = _parse_tool(await session.call_tool("room_bootstrap", {}))
                    assert payload["ok"] is True
                    assert payload["result"]["status"] == "ready"
                    assert payload["result"]["project"]["id"] == project["id"]
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    assert "room_bootstrap" in names
                    assert "room_sync" in names
    finally:
        server.should_exit = True
        await server_task
        _clear_http_mcp_context()
