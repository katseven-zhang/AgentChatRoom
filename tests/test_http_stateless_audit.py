"""#120 audit: fixate current ``mcp_http_stateless=true`` behaviour.

Research task deliverable — these probes document (without changing) what the
stateless mode can and cannot do today:

* the plain HTTP/JSON-RPC layer answers without a session id (D0);
* the MCP room flow is unusable because ``room_bootstrap`` must request
  workspace roots from the client, which needs a persistent session (D1);
* an SSE-mode stateless ``tools/call`` without initialization is rejected
  (D7).

The lease-conflict and reclaim findings of the audit live in the REST domain
layer and are already covered by ``test_services`` / ``test_presence_decoupling``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace

import httpx
import pytest
import uvicorn

from agentchatroom.api import create_app


async def _serve(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    return server, task, f"http://127.0.0.1:{port}/mcp"


async def _stop(server, task) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-AgentChatRoom-Software-Key": "audit-http",
        "X-AgentChatRoom-Software-Name": "Audit HTTP",
        "X-AgentChatRoom-Software-Client": "audit",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }


@pytest.mark.asyncio
async def test_stateless_http_answers_without_a_session_id(settings, project_dir):
    """D0：stateless 下 initialize 无需 mcp-session-id 即返回 200。"""
    app = create_app(replace(settings, mcp_http_stateless=True))
    project = app.state.service.create_project(
        root_path=str(project_dir), name="Stateless Audit"
    )
    credential = app.state.service.issue_agent_token(
        project["id"], name="Audit"
    )
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "audit", "version": "1"},
                    },
                },
                headers=_headers(credential["token"]),
            )
            assert response.status_code == 200
            assert not response.headers.get("mcp-session-id")
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_stateless_room_bootstrap_cannot_request_workspace_roots(
    settings, project_dir
):
    """D1：stateless 下 room_bootstrap 失败（workspace_roots_unavailable）。

    room_bootstrap 必须向客户端请求 workspace roots；stateless 传输没有
    可挂起的持久会话，请求超时并返回结构化错误。MCP 房间流程因此不可用，
    提交兜底走 REST（见 tests/test_cli_submit.py 与 #118）。
    """
    app = create_app(replace(settings, mcp_http_stateless=True))
    project = app.state.service.create_project(
        root_path=str(project_dir), name="Stateless Audit"
    )
    credential = app.state.service.issue_agent_token(
        project["id"], name="Audit"
    )
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            boot = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "room_bootstrap",
                        "arguments": {"project_name": "Stateless Audit"},
                    },
                },
                headers=_headers(credential["token"]),
            )
            assert boot.status_code == 200
            payload = json.loads(boot.json()["result"]["content"][0]["text"])
            assert payload["ok"] is False
            assert payload["error"]["code"] == "workspace_roots_unavailable"
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_stateless_sse_tools_call_without_initialize_is_rejected(
    settings, project_dir
):
    """D7：SSE（json_response=false）+ stateless 接受未初始化的 tools/call，
    工具层因无 bootstrap 绑定而优雅失败——传输层与领域层各自独立。"""
    app = create_app(
        replace(settings, mcp_http_stateless=True, mcp_http_json_response=False)
    )
    project = app.state.service.create_project(
        root_path=str(project_dir), name="Stateless Audit"
    )
    credential = app.state.service.issue_agent_token(
        project["id"], name="Audit"
    )
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {"name": "room_sync", "arguments": {}},
                },
                headers=_headers(credential["token"]),
            )
            # The transport answers with an SSE stream; without a bootstrap
            # binding the tool layer fails gracefully and points at
            # room_bootstrap instead of hanging or crashing.
            assert response.status_code == 200
            assert "event: message" in response.text
            assert "call_room_bootstrap" in response.text
    finally:
        await _stop(server, task)
