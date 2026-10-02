from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agentchatroom.mcp_http_adoption import (
    IncompleteRequestBodyError,
    PayloadTooLargeError,
    _read_request_body,
    with_session_adoption,
)

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


class HangingInitApp(FakeMcpApp):
    """Answers the handshake but never finishes its SSE response."""

    async def __call__(self, scope, receive, send) -> None:
        chunks = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                if message.get("type") == "http.disconnect":
                    return
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
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (MCP_SESSION_ID.encode(), session_id.encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'event: message\ndata: {"result":{}}\n\n',
                    "more_body": True,
                }
            )
            # Mimic an SSE POST stream that stays open: park until disconnect.
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
        await send(
            {
                "type": "http.response.start",
                "status": 202,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})


@pytest.mark.asyncio
async def test_parked_handshakes_are_bounded_and_fail_closed():
    app = HangingInitApp()
    middleware = _middleware(app, max_parked_handshakes=1)

    # First adoption parks its non-terminating handshake task.
    first = await _call(middleware, headers={MCP_SESSION_ID: "reaped-1"}, payload=_business_call("reaped-1"))
    assert len(middleware._parked) == 1
    assert app.requests[-1]["payload"].get("method") == "tools/call"

    # Second adoption: the parked set is full, so the adoption fails closed
    # and the original request is replayed untouched with its own bookmark.
    second = await _call(middleware, headers={MCP_SESSION_ID: "reaped-2"}, payload=_business_call("reaped-2"))
    assert len(middleware._parked) == 1
    # Fail-closed passthrough: the original request is replayed with its own
    # (still unknown) bookmark instead of being adopted.
    assert app.requests[-1]["headers"][MCP_SESSION_ID] == "reaped-2"
    assert app.requests[-1]["payload"].get("method") == "tools/call"


@pytest.mark.asyncio
async def test_read_request_body_rejects_oversize_single_chunk():
    """#192: body over limit raises instead of silently truncating."""
    body = b"x" * 33

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    with pytest.raises(PayloadTooLargeError) as exc_info:
        await _read_request_body(receive, limit=32)
    assert exc_info.value.limit == 32


@pytest.mark.asyncio
async def test_read_request_body_rejects_when_more_body_pending_at_limit():
    """#192: at limit with more_body still coming is truncation — fail closed."""

    async def receive():
        return {"type": "http.request", "body": b"y" * 32, "more_body": True}

    with pytest.raises(PayloadTooLargeError):
        await _read_request_body(receive, limit=32)


@pytest.mark.asyncio
async def test_read_request_body_accepts_exact_limit_without_more_body():
    async def receive():
        return {"type": "http.request", "body": b"z" * 32, "more_body": False}

    assert await _read_request_body(receive, limit=32) == b"z" * 32


@pytest.mark.asyncio
async def test_read_request_body_stitches_chunks_under_limit():
    chunks = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ]
    index = {"i": 0}

    async def receive():
        message = chunks[index["i"]]
        index["i"] += 1
        return message

    assert await _read_request_body(receive, limit=8) == b"abcd"


@pytest.mark.asyncio
async def test_oversized_adoption_post_returns_413_not_truncated_replay():
    """#192: adoption-path oversize body answers structured 413, not parse error."""
    app = FakeMcpApp()
    middleware = _middleware(app, body_limit=16)

    body = json.dumps(_business_call("reaped-1")).encode("utf-8")
    assert len(body) > 16

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"mcp-session-id", b"reaped-1")],
        "query_string": b"",
    }
    messages: list[dict] = []
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 413
    response_body = b"".join(
        m.get("body") or b"" for m in messages if m["type"] == "http.response.body"
    )
    error = json.loads(response_body)["error"]
    assert error["code"] == "payload_too_large"
    assert error["data"]["limit"] == 16
    assert error["data"]["required_action"] == "reduce_request_size"
    assert app.requests == []


@pytest.mark.asyncio
async def test_normal_sized_adoption_post_still_replays():
    """#192: bodies under the limit keep the existing adoption path."""
    app = FakeMcpApp()
    middleware = _middleware(app, body_limit=8 * 1024 * 1024)

    messages = await _call(
        middleware,
        headers={MCP_SESSION_ID: "reaped-1"},
        payload=_business_call("reaped-1"),
    )
    start, body = _response(messages)
    assert start["status"] == 200
    assert json.loads(body)["ok"] is True
    methods = [r["payload"].get("method") for r in app.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]


class _StreamingApp:
    """Streams like an SSE writer and closes only when the transport aborts."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.active: dict[str, bool] = {}
        self.observed: list[dict] = []
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
        payload = json.loads(bytes(chunks) or b"{}")
        self.requests.append({"method": payload.get("method")})
        if payload.get("method") == "initialize":
            self._counter += 1
            session_id = f"synthetic-{self._counter}"
            self.active[session_id] = True
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(MCP_SESSION_ID.encode(), session_id.encode())],
                }
            )
            await send({"type": "http.response.body", "body": b'{"result":{}}'})
            return
        if payload.get("method") == "notifications/initialized":
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"data: one\n\n", "more_body": True}
        )
        self.observed.append(await receive())
        await send({"type": "http.response.body", "body": b""})


def _abort_scope() -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (MCP_SESSION_ID.encode(), b"reaped-1"),
            (b"x-agentchatroom-software-key", b"workbuddy"),
        ],
        "query_string": b"",
    }


def _abort_receive(frames: list[dict]):
    """A transport receive: hands over ``frames`` then blocks (client connected)."""
    queue = list(frames)
    stuck = asyncio.Event()

    async def receive():
        if queue:
            return queue.pop(0)
        await stuck.wait()
        return {"type": "http.disconnect"}

    return receive


@pytest.mark.asyncio
async def test_read_request_body_rejects_disconnect_before_completion():
    """#306: a mid-body abort is a prefix, not a request, so it must not return."""
    frames = [
        {"type": "http.request", "body": b'{"method":"tools/call"', "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return frames.pop(0)

    with pytest.raises(IncompleteRequestBodyError) as exc_info:
        await _read_request_body(receive, limit=4096)
    assert exc_info.value.received == len(b'{"method":"tools/call"')


@pytest.mark.asyncio
async def test_truncated_body_is_rejected_instead_of_replayed_as_a_request():
    app = FakeMcpApp()
    middleware = _middleware(app)
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    await middleware(
        _abort_scope(),
        _abort_receive(
            [
                {
                    "type": "http.request",
                    "body": b'{"jsonrpc":"2.0","id":1,"method":"tools/call"',
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ]
        ),
        send,
    )

    start, body = _response(messages)
    assert start["status"] == 400
    error = json.loads(body)["error"]
    assert error["code"] == "request_body_incomplete"
    assert error["data"]["required_action"] == "resend_request"
    # Neither the truncated prefix nor a surrogate handshake reached downstream.
    assert app.requests == []


@pytest.mark.asyncio
async def test_client_abort_reaches_the_adopted_streaming_request():
    """#306: the replay must forward a real disconnect, not park forever."""
    app = _StreamingApp()
    middleware = _middleware(app)
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        if reads == 1:
            return {
                "type": "http.request",
                "body": json.dumps(_business_call("reaped-1")).encode(),
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    await asyncio.wait_for(middleware(_abort_scope(), receive, send), timeout=2)

    assert app.observed[-1] == {"type": "http.disconnect"}
    start, _ = _response(messages)
    assert start["status"] == 200


@pytest.mark.asyncio
async def test_connected_client_is_not_reported_as_aborted_mid_stream():
    """The relay must forward disconnects, never invent them mid-response."""
    app = _StreamingApp()
    middleware = _middleware(app)

    async def send(message):
        pass

    receive = _abort_receive(
        [
            {
                "type": "http.request",
                "body": json.dumps(_business_call("reaped-1")).encode(),
                "more_body": False,
            }
        ]
    )

    task = asyncio.create_task(middleware(_abort_scope(), receive, send))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    assert app.observed == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
