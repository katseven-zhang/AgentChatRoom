from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace

import httpx
import pytest
import uvicorn

from agentchatroom import mcp_server
from agentchatroom.api import create_app
from agentchatroom.credential_bundle import encode_project_credential_bundle


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
def _isolate_adoption_context():
    _clear_http_mcp_context()
    yield
    _clear_http_mcp_context()


def _initialize_request(request_id: int, client_name: str = "adoption-client") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1"},
        },
    }


def _tool_payload(response: httpx.Response) -> dict:
    return json.loads(response.json()["result"]["content"][0]["text"])


def _sse_tool_payload(response: httpx.Response) -> dict:
    """Parse the tool payload from an SSE (json_response=false) POST body."""
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            return json.loads(payload["result"]["content"][0]["text"])
    raise AssertionError(f"no SSE data event: {response.text[:200]!r}")


def _parse_session_tool(result) -> dict:
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        return structured
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("tool result carries no payload")


async def _stop_server(server, task) -> None:
    """Shut the test server down without hanging on a kept-alive SSE stream."""
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


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


def _http_headers(bundle: str, *, key: str, name: str, client: str = "test-http") -> dict:
    return {
        "Authorization": f"Bearer {bundle}",
        "X-AgentChatRoom-Software-Key": key,
        "X-AgentChatRoom-Software-Name": name,
        "X-AgentChatRoom-Software-Client": client,
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }


def _project_with_bundle(app, project_dir, *, name: str):
    project = app.state.service.create_project(root_path=str(project_dir), name=name)
    credential = app.state.service.issue_agent_token(project["id"], name=name)
    bundle = encode_project_credential_bundle(
        [{"name": project["name"], "token": credential["token"]}]
    )
    return project, bundle


def _tuned(settings, **overrides):
    base = {
        "heartbeat_timeout_seconds": 30,
        "presence_keepalive_interval_seconds": 0.05,
        "mcp_http_session_idle_timeout_seconds": 0.3,
    }
    base.update(overrides)
    return replace(settings, **base)


class _HttpSession:
    """Raw JSON-RPC client that can keep replaying an adopted bookmark."""

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers
        self.transport_id = ""
        self._ids = iter(range(200, 400))

    async def open(self, client: httpx.AsyncClient) -> str:
        initialized = await client.post(
            self.url, json=_initialize_request(next(self._ids))
        )
        assert initialized.status_code == 200, initialized.text
        self.transport_id = initialized.headers["mcp-session-id"]
        notified = await client.post(
            self.url,
            headers={"mcp-session-id": self.transport_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notified.status_code == 202, notified.text
        return self.transport_id

    async def call(
        self,
        client: httpx.AsyncClient,
        name: str,
        arguments: dict,
        *,
        session_id: str | None = None,
    ) -> httpx.Response:
        return await client.post(
            self.url,
            headers={"mcp-session-id": session_id or self.transport_id},
            json={
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )


@pytest.mark.asyncio
async def test_adopted_reaped_session_replays_writes_and_resumes_room_session(
    settings, project_dir
):
    """标准 1+2：默认模式收养已回收会话，200 且真实生效，Room Session 不变。"""
    app = create_app(_tuned(settings))
    project, bundle = _project_with_bundle(app, project_dir, name="Adoption Base")
    headers = _http_headers(bundle, key="adopted-http", name="Adopted HTTP")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            session = _HttpSession(url, headers)
            await session.open(client)
            booted = _tool_payload(
                await session.call(
                    client, "room_bootstrap", {"project_name": project["name"]}
                )
            )
            assert booted["ok"], booted
            room_session_id = booted["result"]["session"]["id"]

            # Let the stateful transport exceed the configured idle timeout.
            await asyncio.sleep(0.6)

            replayed = await session.call(
                client,
                "message_post",
                {"body": "#117 adoption write", "model_display_name": "test"},
            )
            assert replayed.status_code == 200, replayed.text
            adopted_transport = replayed.headers.get("mcp-session-id")
            assert adopted_transport and adopted_transport != session.transport_id
            assert _tool_payload(replayed)["ok"]

            binding = mcp_server.get_runtime_binding(
                f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{adopted_transport}"
            )
            assert binding is not None
            assert binding.session_id == room_session_id
            assert binding.project_id == project["id"]
            assert (
                mcp_server.get_runtime_binding(
                    f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{session.transport_id}"
                )
                is None
            )

        messages = [
            event
            for event in app.state.service.query_audit(project["id"])["events"]
            if event["event_type"] == "message.message"
            and (event.get("payload") or {}).get("body") == "#117 adoption write"
        ]
        assert messages
        assert messages[-1]["actor_session_id"] == room_session_id
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_strict_mode_keeps_the_expired_session_contract(settings, project_dir):
    """标准 1：严格模式保留 #116 契约（404 + mcp_session_expired + 响应头）。"""
    app = create_app(_tuned(settings, mcp_http_session_adoption=False))
    project, bundle = _project_with_bundle(app, project_dir, name="Strict Session")
    headers = _http_headers(bundle, key="strict-http", name="Strict HTTP")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            session = _HttpSession(url, headers)
            await session.open(client)
            booted = _tool_payload(
                await session.call(
                    client, "room_bootstrap", {"project_name": project["name"]}
                )
            )
            assert booted["ok"], booted
            await asyncio.sleep(0.6)

            expired = await session.call(client, "room_sync", {})
            assert expired.status_code == 404, expired.text
            assert expired.headers.get("x-agentchatroom-mcp-session") == "expired"
            error = expired.json()["error"]
            assert error["message"] == "Session not found"
            assert error["data"]["code"] == "mcp_session_expired"
            assert error["data"]["required_action"] == "reconnect_mcp_session"
            assert error["data"]["reconnect_hint"]
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_parallel_adopted_sessions_stay_isolated(settings, project_dir):
    """标准 3：同身份并行会话各自收养，归属各自 Room Session。"""
    app = create_app(_tuned(settings))
    project, bundle = _project_with_bundle(app, project_dir, name="Parallel Adoption")
    headers = _http_headers(bundle, key="parallel-http", name="Parallel HTTP")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            first = _HttpSession(url, headers)
            second = _HttpSession(url, headers)
            await first.open(client)
            await second.open(client)
            assert first.transport_id != second.transport_id

            sessions = {}
            for label, session in (("first", first), ("second", second)):
                booted = _tool_payload(
                    await session.call(
                        client, "room_bootstrap", {"project_name": project["name"]}
                    )
                )
                assert booted["ok"], booted
                sessions[label] = booted["result"]["session"]["id"]
            assert sessions["first"] != sessions["second"]

            await asyncio.sleep(0.6)

            for label, session in (("first", first), ("second", second)):
                replayed = await session.call(
                    client,
                    "message_post",
                    {"body": f"#117 parallel {label}", "model_display_name": "test"},
                )
                assert replayed.status_code == 200, replayed.text
                assert _tool_payload(replayed)["ok"]
                adopted = replayed.headers["mcp-session-id"]
                binding = mcp_server.get_runtime_binding(
                    f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{adopted}"
                )
                assert binding is not None
                assert binding.session_id == sessions[label]

        authored = {
            (event.get("payload") or {}).get("body"): event["actor_session_id"]
            for event in app.state.service.query_audit(project["id"])["events"]
            if event["event_type"] == "message.message"
            and str((event.get("payload") or {}).get("body", "")).startswith(
                "#117 parallel"
            )
        }
        assert authored["#117 parallel first"] == sessions["first"]
        assert authored["#117 parallel second"] == sessions["second"]
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_adoption_refuses_a_foreign_credential_bookmark(settings, tmp_path):
    """标准 4：A 重放 B 的已回收 session id 不能以 B 身份执行操作。"""
    app = create_app(_tuned(settings))
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    root_a.mkdir()
    root_b.mkdir()
    project_b, bundle_b = _project_with_bundle(app, root_b, name="Adoption B")
    _, bundle_a = _project_with_bundle(app, root_a, name="Adoption A")
    headers_a = _http_headers(bundle_a, key="identity-a", name="Identity A")
    headers_b = _http_headers(bundle_b, key="identity-b", name="Identity B")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers_b, timeout=10.0) as client_b:
            owner = _HttpSession(url, headers_b)
            await owner.open(client_b)
            booted = _tool_payload(
                await owner.call(
                    client_b, "room_bootstrap", {"project_name": project_b["name"]}
                )
            )
            assert booted["ok"], booted
            room_session_b = booted["result"]["session"]["id"]

        await asyncio.sleep(0.6)

        async with httpx.AsyncClient(headers=headers_a, timeout=10.0) as client_a:
            intruder = _HttpSession(url, headers_a)
            replayed = await intruder.call(
                client_a,
                "message_post",
                {"body": "must-not-write-as-b", "model_display_name": "test"},
                session_id=owner.transport_id,
            )
            # Adoption happens at the transport level, but no Room Session is
            # resumed: the bookmark belongs to another software identity.
            assert replayed.status_code == 200, replayed.text
            payload = _tool_payload(replayed)
            assert not payload["ok"], payload
            assert str(payload["error"]["code"]) not in {"", "internal_error"}
            adopted = replayed.headers["mcp-session-id"]
            assert (
                mcp_server.get_runtime_binding(
                    f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{adopted}"
                )
                is None
            )

        events = app.state.service.query_audit(project_b["id"])["events"]
        assert [
            event
            for event in events
            if (event.get("payload") or {}).get("body") == "must-not-write-as-b"
        ] == []
        assert [
            item["id"]
            for item in app.state.service.snapshot(project_b["id"])["agents"]
            if item["id"] == room_session_b
        ] == [room_session_b]
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_adoption_without_a_tombstone_degrades_safely(settings, project_dir):
    """标准 5：墓碑丢失（重启）后收养不 5xx，返回结构化错误并可重新 bootstrap。"""
    app = create_app(_tuned(settings))
    project, bundle = _project_with_bundle(app, project_dir, name="Restart Degrade")
    headers = _http_headers(bundle, key="restart-http", name="Restart HTTP")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            session = _HttpSession(url, headers)
            await session.open(client)
            booted = _tool_payload(
                await session.call(
                    client, "room_bootstrap", {"project_name": project["name"]}
                )
            )
            assert booted["ok"], booted
            await asyncio.sleep(0.6)

            # Simulate a restart that lost both live bindings and tombstones.
            mcp_server.clear_runtime_binding()

            replayed = await session.call(
                client,
                "message_post",
                {"body": "after-restart", "model_display_name": "test"},
            )
            assert replayed.status_code == 200, replayed.text
            payload = _tool_payload(replayed)
            assert not payload["ok"], payload
            assert str(payload["error"]["code"]) not in {"", "internal_error"}

            rebound = _tool_payload(
                await session.call(
                    client, "room_bootstrap", {"project_name": project["name"]}
                )
            )
            assert rebound["ok"], rebound
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_adopted_session_still_submits_past_the_presence_window(
    settings, project_dir
):
    """传输回收不再关闭 Room Session：越出 presence 窗口仍能提交（仅显示离线）。"""
    app = create_app(_tuned(settings, heartbeat_timeout_seconds=0.5))
    project, bundle = _project_with_bundle(app, project_dir, name="Window Edge")
    headers = _http_headers(bundle, key="window-http", name="Window HTTP")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            session = _HttpSession(url, headers)
            await session.open(client)
            booted = _tool_payload(
                await session.call(
                    client, "room_bootstrap", {"project_name": project["name"]}
                )
            )
            assert booted["ok"], booted
            room_session_id = booted["result"]["session"]["id"]

            # Reap the transport and let the presence window lapse as well.
            await asyncio.sleep(1.5)
            reaped = next(
                item
                for item in app.state.service.snapshot(project["id"])["agents"]
                if item["id"] == room_session_id
            )
            assert reaped["status"] == "offline"

            replayed = await session.call(
                client,
                "message_post",
                {"body": "past-window", "model_display_name": "test"},
            )
            assert replayed.status_code == 200, replayed.text
            assert _tool_payload(replayed)["ok"]
            binding = mcp_server.get_runtime_binding(
                f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}"
                f"{replayed.headers['mcp-session-id']}"
            )
            assert binding is not None
            assert binding.session_id == room_session_id
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_sse_response_mode_serves_normal_sessions(settings, project_dir):
    """标准 6：SSE（json_response=false）模式下正常会话与收养中间件共存。"""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = create_app(_tuned(settings, mcp_http_json_response=False))
    project, bundle = _project_with_bundle(app, project_dir, name="SSE Mode")
    headers = _http_headers(bundle, key="sse-mode", name="SSE Mode")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_client:
            async with streamable_http_client(
                url, http_client=http_client
            ) as (read, write, _session):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    booted = _parse_session_tool(
                        await session.call_tool(
                            "room_bootstrap", {"project_name": project["name"]}
                        )
                    )
                    assert booted["ok"], booted
                    synced = _parse_session_tool(
                        await session.call_tool("room_sync", {})
                    )
                    assert synced["ok"], synced
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_adopted_replay_works_in_sse_response_mode(settings, project_dir):
    """标准 6：SSE（json_response=false）模式下收养重放同样返回完整结果。

    回归锚点：合成握手与重放的 ASGI receive 一旦提前返回断连或急速轮询，
    会卡死或截断整个事件循环——本用例在该模式下走完回收→重放全链路。
    """
    app = create_app(_tuned(settings, mcp_http_json_response=False))
    project, bundle = _project_with_bundle(app, project_dir, name="SSE Adoption")
    headers = _http_headers(bundle, key="sse-adoption", name="SSE Adoption")
    server, task, url = await _serve(app)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            session = _HttpSession(url, headers)
            await session.open(client)
            booted = _sse_tool_payload(
                await session.call(
                    client, "room_bootstrap", {"project_name": project["name"]}
                )
            )
            assert booted["ok"], booted

            await asyncio.sleep(0.6)

            replayed = await session.call(
                client,
                "message_post",
                {"body": "#117 sse adoption", "model_display_name": "test"},
            )
            assert replayed.status_code == 200, replayed.text
            adopted = replayed.headers.get("mcp-session-id")
            assert adopted and adopted != session.transport_id
            assert _sse_tool_payload(replayed)["ok"]

        messages = [
            event
            for event in app.state.service.query_audit(project["id"])["events"]
            if event["event_type"] == "message.message"
            and (event.get("payload") or {}).get("body") == "#117 sse adoption"
        ]
        assert messages
    finally:
        await _stop_server(server, task)
        _clear_http_mcp_context()
