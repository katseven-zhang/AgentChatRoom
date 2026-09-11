from __future__ import annotations

import json

import httpx
import pytest

from agentchatroom.mcp_http_recovery import (
    DIRECT_CLIENT_RECONNECT_HINT,
    MCP_SESSION_EXPIRED_CODE,
    MCP_SESSION_RECONNECT_ACTION,
    MCP_SESSION_STATUS_HEADER,
    expired_session_exception,
    expired_session_response_body,
    is_expired_session_payload,
    with_expired_session_hints,
)


def _expired_body() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "server-error",
            "error": {"code": -32600, "message": "Session not found"},
        }
    ).encode("utf-8")


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://127.0.0.1:8765/mcp")


def test_expired_session_body_gains_machine_readable_recovery_contract():
    enriched = json.loads(expired_session_response_body(_expired_body()))
    error = enriched["error"]
    # The specification message and code stay untouched.
    assert error["message"] == "Session not found"
    assert error["code"] == -32600
    assert error["data"]["code"] == MCP_SESSION_EXPIRED_CODE
    assert error["data"]["required_action"] == MCP_SESSION_RECONNECT_ACTION
    assert error["data"]["reconnect_hint"] == DIRECT_CLIENT_RECONNECT_HINT

    # Unrelated JSON-RPC errors keep their exact bytes.
    untouched = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32602, "message": "Invalid params"},
        }
    ).encode("utf-8")
    assert expired_session_response_body(untouched) == untouched
    assert is_expired_session_payload(untouched) is False
    assert is_expired_session_payload(b"not-json") is False
    assert is_expired_session_payload(_expired_body()) is True


def test_expired_session_exception_recognises_the_center_404():
    expired = httpx.HTTPStatusError(
        "404",
        request=_request(),
        response=httpx.Response(404, content=_expired_body(), request=_request()),
    )
    assert expired_session_exception(expired) is True

    server_error = httpx.HTTPStatusError(
        "500", request=_request(), response=httpx.Response(500, request=_request())
    )
    assert expired_session_exception(server_error) is False

    # The response header alone is enough when the body is unreadable.
    marked = httpx.HTTPStatusError(
        "404",
        request=_request(),
        response=httpx.Response(
            404, headers={MCP_SESSION_STATUS_HEADER: "expired"}, request=_request()
        ),
    )
    assert expired_session_exception(marked) is True

    grouped = BaseExceptionGroup("group", [expired])
    assert expired_session_exception(grouped) is True
    assert expired_session_exception(RuntimeError("boom")) is False


def _response_app(body: bytes, status: int = 404):
    async def app(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


async def _call(app, scope: dict | None = None) -> list[dict]:
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    async def receive() -> dict:
        return {"type": "http.request"}

    middleware = with_expired_session_hints(app)
    await middleware(scope or {"type": "http", "method": "POST", "path": "/mcp"}, receive, send)
    return messages


@pytest.mark.asyncio
async def test_expired_session_middleware_annotates_only_expired_404s():
    messages = await _call(_response_app(_expired_body()))
    start, body = messages
    assert start["status"] == 404
    headers = {bytes(key).decode().lower(): bytes(value).decode() for key, value in start["headers"]}
    assert headers[MCP_SESSION_STATUS_HEADER] == "expired"
    assert headers["content-length"] == str(len(body["body"]))
    payload = json.loads(body["body"])
    assert payload["error"]["data"]["required_action"] == MCP_SESSION_RECONNECT_ACTION

    # Any other 404 (for example the REST "Not Found") passes through untouched.
    plain = b'{"detail":"Not Found"}'
    messages = await _call(_response_app(plain))
    start, body = messages
    assert start["status"] == 404
    assert body["body"] == plain
    assert all(
        bytes(key).decode().lower() != MCP_SESSION_STATUS_HEADER
        for key, _ in start["headers"]
    )

    # Successful responses are never buffered into a rewritten body.
    ok_body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    messages = await _call(_response_app(ok_body, status=200))
    start, body = messages
    assert start["status"] == 200
    assert body["body"] == ok_body
    assert all(
        bytes(key).decode().lower() != MCP_SESSION_STATUS_HEADER
        for key, _ in start["headers"]
    )


@pytest.mark.asyncio
async def test_expired_session_middleware_forwards_non_http_scopes():
    seen: list[str] = []

    async def app(scope, receive, send) -> None:
        seen.append(str(scope.get("type")))

    async def send(message: dict) -> None:  # pragma: no cover - nothing is sent
        raise AssertionError("non-http scopes must not produce a response")

    async def receive() -> dict:  # pragma: no cover - nothing is read
        raise AssertionError("non-http scopes must not be read")

    await with_expired_session_hints(app)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
