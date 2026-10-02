"""Task #289: the Bridge must recognise the session signal the SDK really sends.

The center answers a reaped ``mcp-session-id`` with HTTP 404, but an SDK client
never sees that status: the client transport converts it into an in-band JSON-RPC
error 32600 "Session terminated" and raises ``McpError``. Detection that only
inspected ``exception.response.status_code`` never fired, so an expired session
was mis-reported as "the center is unreachable", and the re-initialisation budget
was unreachable code. These tests drive the real SDK client against a fake center.
"""

from __future__ import annotations

import json

import httpx
import mcp.types as types
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from agentchatroom import mcp_http_recovery
from agentchatroom.mcp_bridge import (
    BridgeSessionExpiredError,
    BridgeSettings,
    ReconnectingUpstream,
    expired_session_exception,
    expired_session_tool_payload,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_URL = "http://fake-center/mcp"


def first_error(error: BaseException):
    """The SDK raises through anyio task groups; find the error the transport sent."""
    pending = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, McpError):
            return current
        pending.extend(getattr(current, "exceptions", []) or [])
    raise AssertionError(f"no McpError in {error!r}")


class FakeCenter:
    """Minimal Streamable-HTTP center that can reap the session on demand."""

    def __init__(self, reap_requests: float = float("inf")):
        self.initialize_count = 0
        self.call_count = 0
        # How many tool calls answer 404 before the session "comes back".
        self.reap_requests = reap_requests

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"") or b""
            if message.get("more_body") is not True:
                break
        request = json.loads(body.decode("utf-8")) if body else {}
        if scope.get("method") == "GET":
            # The specification's "no standalone SSE stream here" answer; without it
            # the client keeps a reader task open and the session never closes.
            await send(
                {"type": "http.response.start", "status": 405, "headers": []}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if scope.get("method") == "DELETE":
            # Session teardown: answer plainly so the client does not stall on it.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})
            return
        status, payload = self._respond(request.get("method"), request.get("id"))
        headers = [(b"content-type", b"application/json")]
        if status == 200 and request.get("method") == "initialize":
            self.initialize_count += 1
            headers.append(
                (b"mcp-session-id", f"session-{self.initialize_count}".encode())
            )
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": payload})

    def _respond(self, method, request_id):
        if method == "initialize":
            result = types.Result(
                protocolVersion=PROTOCOL_VERSION,
                capabilities=types.ServerCapabilities(),
                serverInfo=types.Implementation(name="fake-center", version="1"),
            )
            return self._envelope(request_id, result)
        if method == "tools/call":
            self.call_count += 1
            if self.call_count <= self.reap_requests:
                # The specification's answer to an unknown/reaped session id.
                return 404, b""
            payload = types.CallToolResult(
                content=[types.TextContent(type="text", text='{"ok": true}')]
            )
            return self._envelope(request_id, payload)
        return 202, b""

    def _envelope(self, request_id, result):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result.model_dump(by_alias=True, mode="json"),
            }
        ).encode("utf-8")
        return 200, body


def _upstream(center: FakeCenter) -> ReconnectingUpstream:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=center), base_url="http://fake-center"
    )
    return ReconnectingUpstream(
        BridgeSettings(
            server_url=SERVER_URL,
            timeout_seconds=5.0,
            retry_attempts=1,
            retry_backoff_seconds=0.0,
        ),
        http_client=client,
    )


@pytest.mark.asyncio
async def test_sdk_reports_a_reaped_session_as_mcp_error_not_http_status():
    """Pin the SDK behaviour this fix keys on, so it cannot drift blind."""
    center = FakeCenter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=center), base_url="http://fake-center"
    ) as client:
        with pytest.raises(Exception) as captured:
            async with streamable_http_client(SERVER_URL, http_client=client) as (
                read,
                write,
                _session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("room_sync", {})

    error = first_error(captured.value)
    assert error.error.code == mcp_http_recovery.SESSION_TERMINATED_ERROR_CODE
    assert error.error.message == "Session terminated"
    assert not hasattr(error, "response")
    # The bridge receives it wrapped in the SDK's task groups, so both must match.
    assert expired_session_exception(error) is True
    assert expired_session_exception(captured.value) is True


@pytest.mark.asyncio
async def test_persistent_expiry_reports_the_expired_session_contract():
    center = FakeCenter()

    with pytest.raises(BridgeSessionExpiredError) as captured:
        await _upstream(center).call_tool("room_sync", {})

    # The budget is spent on one fresh session, then reported -- never looped on.
    assert center.initialize_count == 2
    assert center.call_count == 2
    assert isinstance(first_error(captured.value.__cause__), McpError)

    payload = expired_session_tool_payload(
        reconnect_hint="reload the connector",
        detail=type(captured.value).__name__,
    )
    assert payload["error"]["code"] == "mcp_session_expired"
    assert payload["error"]["required_action"] == "reconnect_mcp_session"
