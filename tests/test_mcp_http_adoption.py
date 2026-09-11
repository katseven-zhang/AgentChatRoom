from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentchatroom.mcp_http_adoption import with_session_adoption

MCP_SESSION_ID = "mcp-session-id"


class FakeMcpApp:
    """Answers the synthetic handshake the way the SDK's manager does."""

    def __init__(self, *, initialize_status: int = 200, streaming: bool = False) -> None:
        self.initialize_status = initialize_status
        self.streaming = streaming
        self.requests: list[dict] = []
        self.active: dict[str, bool] = {}
        self._counter = 0

    def is_active(self, transport_id: str) -> bool:
        return bool(self.active.get(transport_id))

    async def __call__(self, scope, receive, send) -> None:
        chunks = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.extend(message.get("body") or b"")
            if not message.get("more_body"):
                break
        headers = {
            bytes(key).decode("latin-1"): bytes(value).decode("latin-1")
            for key, value in scope.get("headers") or ()
        }
        payload = json.loads(bytes(chunks) or b"{}")
        self.requests.append(
            {"method": scope["method"], "headers": headers, "payload": payload}
        )
        method = payload.get("method")
        if method == "initialize":
            self._counter += 1
            session_id = f"synthetic-{self._counter}"
            self.active[session_id] = True
            await send(
                {
                    "type": "http.response.start",
                    "status": self.initialize_status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (MCP_SESSION_ID.encode(), session_id.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"result":{}}'})
            return
        if method == "notifications/initialized":
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if self.streaming:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
            await send({"type": "http.response.body", "body": b"data: two\n\n"})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"ok": True, "transport": headers.get(MCP_SESSION_ID, "")}).encode(),
            }
        )


async def _call(middleware, *, method: str = "POST", headers=None, payload=None):
    raw = [
        (key.encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    body = json.dumps(payload or {}).encode("utf-8")
    scope = {
        "type": "http",
        "method": method,
        "path": "/mcp",
        "headers": raw,
        "query_string": b"",
    }
    messages: list[dict] = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)
    return messages


def _response(messages: list[dict]) -> tuple[dict, bytes]:
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body") or b""
        for item in messages
        if item["type"] == "http.response.body"
    )
    return start, body


def _headers(start: dict) -> dict[str, str]:
    return {
        bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
        for key, value in start.get("headers") or ()
    }


def _middleware(app, **kwargs):
    defaults = {
        "session_manager": SimpleNamespace(_server_instances={}),
        "is_active": getattr(app, "is_active", None),
    }
    defaults.update(kwargs)
    return with_session_adoption(app, **defaults)


def _business_call(session_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "room_sync", "arguments": {}},
    }


@pytest.mark.asyncio
async def test_unknown_session_is_adopted_with_a_fresh_transport():
    app = FakeMcpApp()
    restored: list[tuple[str, object]] = []
    middleware = _middleware(
        app,
        lookup_binding=lambda old, key: "binding-for-" + old,
        adopt_binding=lambda new, binding: restored.append((new, binding)),
    )

    messages = await _call(
        middleware,
        headers={MCP_SESSION_ID: "reaped-1", "x-agentchatroom-software-key": "workbuddy"},
        payload=_business_call("reaped-1"),
    )
    start, body = _response(messages)

    # Handshake ran server-side and the business call was replayed.
    methods = [request["payload"].get("method") for request in app.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert app.requests[-1]["headers"][MCP_SESSION_ID] == "synthetic-1"
    assert app.requests[-1]["headers"]["x-agentchatroom-adopted-from"] == "reaped-1"
    # The client learns the replacement bookmark and still gets its result.
    assert _headers(start)[MCP_SESSION_ID] == "synthetic-1"
    assert json.loads(body)["ok"] is True
    assert restored == [("synthetic-1", "binding-for-reaped-1")]


@pytest.mark.asyncio
async def test_adopted_bookmark_is_reused_without_a_second_handshake():
    app = FakeMcpApp()
    middleware = _middleware(app)

    await _call(middleware, headers={MCP_SESSION_ID: "reaped-1"}, payload=_business_call("reaped-1"))
    await _call(middleware, headers={MCP_SESSION_ID: "reaped-1"}, payload=_business_call("reaped-1"))

    methods = [request["payload"].get("method") for request in app.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    assert "synthetic-2" not in json.dumps(methods)


@pytest.mark.asyncio
async def test_active_session_and_delete_pass_through_untouched():
    app = FakeMcpApp()
    app.active["live-1"] = True
    middleware = _middleware(app)

    messages = await _call(
        middleware, headers={MCP_SESSION_ID: "live-1"}, payload=_business_call("live-1")
    )
    start, _ = _response(messages)
    assert [request["payload"].get("method") for request in app.requests] == ["tools/call"]
    assert _headers(start).get(MCP_SESSION_ID) is None

    app.requests.clear()
    await _call(middleware, method="DELETE", headers={MCP_SESSION_ID: "reaped-1"})
    # DELETE is only forwarded; an unknown bookmark is never adopted for it.
    assert [request["method"] for request in app.requests] == ["DELETE"]


@pytest.mark.asyncio
async def test_strict_mode_and_handshake_failure_stay_closed():
    app = FakeMcpApp()
    strict = _middleware(app, enabled=False)
    await _call(strict, headers={MCP_SESSION_ID: "reaped-1"}, payload=_business_call("reaped-1"))
    # In strict mode the adapter never synthesises a handshake.
    assert [request["payload"].get("method") for request in app.requests] == ["tools/call"]
    assert app.requests[0]["headers"][MCP_SESSION_ID] == "reaped-1"

    failing = FakeMcpApp(initialize_status=500)
    degraded = _middleware(failing, lookup_binding=lambda old, key: "binding")
    messages = await _call(
        degraded, headers={MCP_SESSION_ID: "reaped-1"}, payload=_business_call("reaped-1")
    )
    start, _ = _response(messages)
    # The original request is replayed untouched so the closed behaviour applies.
    assert failing.requests[-1]["payload"]["method"] == "tools/call"
    assert failing.requests[-1]["headers"][MCP_SESSION_ID] == "reaped-1"


@pytest.mark.asyncio
async def test_foreign_identity_never_restores_a_room_binding():
    app = FakeMcpApp()
    seen: list[tuple[str, str]] = []
    restored: list[tuple[str, object]] = []

    def lookup(old: str, key: str):
        seen.append((old, key))
        return None  # mismatching identity is reported as "no binding"

    middleware = _middleware(app, lookup_binding=lookup, adopt_binding=lambda new, b: restored.append((new, b)))
    await _call(
        middleware,
        headers={MCP_SESSION_ID: "reaped-b", "x-agentchatroom-software-key": "identity-a"},
        payload=_business_call("reaped-b"),
    )

    assert seen == [("reaped-b", "identity-a")]
    assert restored == []


@pytest.mark.asyncio
async def test_streaming_responses_keep_their_body_and_gain_the_bookmark():
    app = FakeMcpApp(streaming=True)
    middleware = _middleware(app, lookup_binding=lambda old, key: "binding")

    messages = await _call(middleware, headers={MCP_SESSION_ID: "reaped-1"}, payload=_business_call("reaped-1"))
    start, body = _response(messages)
    assert _headers(start)[MCP_SESSION_ID] == "synthetic-1"
    assert body == b"data: one\n\ndata: two\n\n"


@pytest.mark.asyncio
async def test_non_http_scopes_are_forwarded():
    seen: list[str] = []

    async def app(scope, receive, send) -> None:
        seen.append(str(scope.get("type")))

    middleware = _middleware(app)
    await middleware({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
