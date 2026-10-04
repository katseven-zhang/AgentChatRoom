"""Task #288: an adopted surrogate belongs to the caller that created it.

The presented ``mcp-session-id`` is a bookmark, never a credential. The reuse map
keyed on the bookmark alone, so a second caller who learned it (client INFO logs,
the ``x-agentchatroom-adopted-from`` response header) rode a transport that was
opened with the first caller's credential and re-attached to the first caller's
Room Session -- and the runtime binding injects that Session's token into the
tool arguments, so the second caller could write as the first one.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from agentchatroom.mcp_http_adoption import (
    MCP_SESSION_ID_HEADER,
    SOFTWARE_KEY_HEADER,
    TolerantSessionAdoptionMiddleware,
)

BOOKMARK = "reaped-session"


class Center:
    """A downstream MCP app that mints a fresh transport per initialize."""

    def __init__(self) -> None:
        self.initializations = 0
        self.forwarded: list[tuple[str, str]] = []

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"") or b""
            if message.get("more_body") is not True:
                break
        request = json.loads(body.decode("utf-8")) if body else {}
        method = request.get("method")
        headers = [(b"content-type", b"application/json")]
        status = 200
        if method == "initialize":
            self.initializations += 1
            headers.append(
                (MCP_SESSION_ID_HEADER, f"surrogate-{self.initializations}".encode())
            )
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                }
            ).encode("utf-8")
        elif method == "notifications/initialized":
            status, payload = 202, b""
        else:
            session_id = ""
            for key, value in scope.get("headers") or ():
                if bytes(key).lower() == MCP_SESSION_ID_HEADER:
                    session_id = bytes(value).decode("latin-1")
            self.forwarded.append((method or "", session_id))
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request.get("id"), "result": {"ok": True}}
            ).encode("utf-8")
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": payload})


def _middleware(center: Center, adopted: list):
    bindings = {"caller-a": SimpleNamespace(owner="caller-a"),
                "caller-b": SimpleNamespace(owner="caller-b")}

    def is_active(transport_id: str) -> bool:
        # Every surrogate this center mints is a live transport; only the
        # bookmark the clients keep replaying has been reaped.
        return transport_id != BOOKMARK

    return TolerantSessionAdoptionMiddleware(
        center,
        session_manager=None,
        is_active=is_active,
        lookup_binding=lambda bookmark, software_key: bindings.get(software_key),
        adopt_binding=lambda surrogate, binding: adopted.append(
            (surrogate, binding.owner)
        ),
    )


def _post(client: TestClient, path: str, method: str, auth: str, software: str):
    return client.post(
        path,
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": {}},
        headers={
            "Authorization": f"Bearer {auth}",
            SOFTWARE_KEY_HEADER.decode(): software,
            MCP_SESSION_ID_HEADER.decode(): BOOKMARK,
        },
    )


def test_same_bookmark_from_a_second_caller_gets_its_own_transport():
    center = Center()
    adopted: list = []
    client = TestClient(_middleware(center, adopted))

    first = _post(client, "/mcp", "tools/call", "token-a", "caller-a")
    assert first.status_code == 200
    assert center.forwarded[-1] == ("tools/call", "surrogate-1")
    assert adopted[-1] == ("surrogate-1", "caller-a")

    # A different caller presenting the very same bookmark must not inherit the
    # transport -- and therefore not the Room binding/token -- of the first one.
    second = _post(client, "/mcp", "tools/call", "token-b", "caller-b")

    assert second.status_code == 200
    assert center.forwarded[-1] == ("tools/call", "surrogate-2")
    assert adopted[-1] == ("surrogate-2", "caller-b")


def test_the_same_caller_still_reuses_its_adopted_transport():
    center = Center()
    adopted: list = []
    client = TestClient(_middleware(center, adopted))

    for _ in range(2):
        response = _post(client, "/mcp", "tools/call", "token-a", "caller-a")
        assert response.status_code == 200

    assert center.initializations == 1, "adoption repeated for one caller"
    assert [item[1] for item in center.forwarded] == ["surrogate-1", "surrogate-1"]
    # The identity comparison runs on every reuse, not only on the first adoption.
    assert adopted == [("surrogate-1", "caller-a")] * 2


def test_a_rotated_credential_for_one_identity_is_not_reused_either():
    center = Center()
    adopted: list = []
    client = TestClient(_middleware(center, adopted))

    assert _post(client, "/mcp", "tools/call", "token-a", "caller-a").status_code == 200
    assert _post(client, "/mcp", "tools/call", "token-a2", "caller-a").status_code == 200

    assert [item[1] for item in center.forwarded] == ["surrogate-1", "surrogate-2"]


@pytest.mark.asyncio
async def test_parallel_retries_of_one_bookmark_share_a_single_handshake():
    """#354: the check-then-act spans awaits, so retries used to multiply transports."""
    center = SlowCenter()
    adopted: list = []
    middleware = _middleware(center, adopted)

    statuses = await asyncio.gather(
        *(_issue(middleware, "token-a", "caller-a") for _ in range(5))
    )

    assert statuses == [200] * 5
    assert center.initializations == 1, "five parallel retries opened five transports"
    assert [item[1] for item in center.forwarded] == ["surrogate-1"] * 5
    # One guard lock per (bookmark, identity): the table must not grow with the
    # number of requests either.
    assert len(middleware._adopting) <= 1


class SlowCenter(Center):
    """Center whose handshake is slow enough for the retries to overlap."""

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"") or b""
            if message.get("more_body") is not True:
                break
        request = json.loads(body.decode("utf-8")) if body else {}
        if request.get("method") == "initialize":
            await asyncio.sleep(0.05)

        async def one_shot():
            return {"type": "http.request", "body": body, "more_body": False}

        await super().__call__(scope, one_shot, send)


async def _issue(middleware, auth: str, software: str) -> int:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": "1", "method": "tools/call", "params": {}}
    ).encode("utf-8")
    delivered = {"sent": False, "status": 0}

    async def receive():
        if not delivered["sent"]:
            delivered["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        if message.get("type") == "http.response.start":
            delivered["status"] = int(message.get("status") or 0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "client": ("test", 1),
        "server": ("test", 80),
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {auth}".encode()),
            (SOFTWARE_KEY_HEADER, software.encode()),
            (MCP_SESSION_ID_HEADER, BOOKMARK.encode()),
        ],
    }
    await middleware(scope, receive, send)
    return delivered["status"]
