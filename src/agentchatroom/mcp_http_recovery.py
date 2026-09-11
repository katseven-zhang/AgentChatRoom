"""Actionable semantics for stateful MCP HTTP sessions the center has reaped.

The MCP SDK answers an unknown or expired ``mcp-session-id`` with HTTP 404 and a
JSON-RPC ``Session not found`` error, exactly as the specification requires. That
status alone is not actionable for a client that never re-initialises: a host
that keeps replaying the stale session id fails every tool call forever. This
adapter keeps the spec-compatible 404 status while adding a machine-readable
state and a human-actionable recovery hint.

It is shared by the server-side ASGI wrapper (``api.py``) and the repository's
own stdio Bridge (``mcp_bridge.py``), so both ends agree on one wire contract.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, MutableMapping

# Wire contract shared by the server adapter and the stdio Bridge.
MCP_SESSION_EXPIRED_CODE = "mcp_session_expired"
MCP_SESSION_RECONNECT_ACTION = "reconnect_mcp_session"
MCP_SESSION_STATUS_HEADER = "x-agentchatroom-mcp-session"
MCP_SESSION_EXPIRED_STATUS = "expired"

# The SDK's message for both an unknown/expired session id and a session id that
# was created under a different credential. Both mean "re-initialise".
EXPIRED_SESSION_MESSAGE = "Session not found"

HTTP_404 = 404
_MAX_BUFFERED_BODY = 64 * 1024

DIRECT_CLIENT_RECONNECT_HINT = (
    "该 MCP HTTP 会话已过期或已被服务端回收。请重新初始化会话（等价操作：在客户端"
    "重新加载/重连 agentchatroom 连接器一次），然后重新调用 room_bootstrap 继续原 "
    "Project；不要无限重试同一个会话。已连接的项目凭据与软件身份不需要更换。"
)


def expired_session_error_data(
    *, reconnect_hint: str = DIRECT_CLIENT_RECONNECT_HINT
) -> dict[str, Any]:
    """Machine-readable state plus the single human recovery step."""
    return {
        "code": MCP_SESSION_EXPIRED_CODE,
        "required_action": MCP_SESSION_RECONNECT_ACTION,
        "reconnect_hint": reconnect_hint,
    }


def is_expired_session_message(message: str) -> bool:
    return EXPIRED_SESSION_MESSAGE in message


def is_expired_session_payload(body: bytes) -> bool:
    """Return True when a JSON-RPC error body reports an unusable session."""
    if not body:
        return False
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    error = parsed.get("error")
    if not isinstance(error, dict):
        return False
    return is_expired_session_message(str(error.get("message") or ""))


def expired_session_response_body(
    body: bytes, *, reconnect_hint: str = DIRECT_CLIENT_RECONNECT_HINT
) -> bytes:
    """Add the recovery contract to an expired-session JSON-RPC error body."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(parsed, dict):
        return body
    error = parsed.get("error")
    if not isinstance(error, dict):
        return body
    if not is_expired_session_message(str(error.get("message") or "")):
        return body
    data = dict(error.get("data") or {})
    data.update(expired_session_error_data(reconnect_hint=reconnect_hint))
    error["data"] = data
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8")


def _header_value(headers: Any, name: str) -> str:
    """Read one header from httpx.Headers, a mapping, or ASGI header tuples."""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        return "" if value is None else str(value)
    wanted = name.lower()
    for key, value in headers:
        key_text = (
            key.decode("latin-1") if isinstance(key, (bytes, bytearray)) else str(key)
        )
        if key_text.lower() != wanted:
            continue
        return (
            value.decode("latin-1")
            if isinstance(value, (bytes, bytearray))
            else str(value)
        )
    return ""


def expired_session_exception(error: BaseException) -> bool:
    """Inspect an exception tree for an expired-session HTTP 404 from the center."""
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if status == HTTP_404:
            if (
                _header_value(
                    getattr(response, "headers", None), MCP_SESSION_STATUS_HEADER
                )
                == MCP_SESSION_EXPIRED_STATUS
            ):
                return True
            body = getattr(response, "content", b"") or b""
            if isinstance(body, str):
                body = body.encode("utf-8", "replace")
            if is_expired_session_payload(body):
                return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return False


class ExpiredSessionHintMiddleware:
    """ASGI wrapper that annotates expired-session 404 responses.

    Responses that are not a 404 JSON-RPC ``Session not found`` error are passed
    through untouched, so the MCP specification behaviour is preserved.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        reconnect_hint: str = DIRECT_CLIENT_RECONNECT_HINT,
    ) -> None:
        self.app = app
        self.reconnect_hint = reconnect_hint

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start: dict[str, Any] = {}
        chunks: list[bytes] = []
        buffering = False

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal buffering
            message_type = message.get("type")
            if message_type == "http.response.start":
                start.clear()
                start.update(message)
                buffering = int(message.get("status") or 200) == HTTP_404
                if not buffering:
                    await send(message)
                return
            if message_type == "http.response.body":
                body = message.get("body") or b""
                if not buffering:
                    await send(message)
                    return
                chunks.append(body)
                buffered = sum(len(chunk) for chunk in chunks)
                if message.get("more_body") and buffered <= _MAX_BUFFERED_BODY:
                    return
                await self._emit(start, b"".join(chunks), send)
                buffering = False
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _emit(
        self,
        start: dict[str, Any],
        body: bytes,
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if not is_expired_session_payload(body):
            # Untouched passthrough: the buffered body is byte-identical to what
            # the original response would have streamed.
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        body = expired_session_response_body(body, reconnect_hint=self.reconnect_hint)
        headers = [
            (key, value)
            for key, value in start.get("headers") or ()
            if bytes(key).lower() != b"content-length"
        ]
        headers.append((b"content-length", str(len(body)).encode("ascii")))
        headers.append(
            (
                MCP_SESSION_STATUS_HEADER.encode("ascii"),
                MCP_SESSION_EXPIRED_STATUS.encode("ascii"),
            )
        )
        start["headers"] = headers
        await send(start)
        await send({"type": "http.response.body", "body": body, "more_body": False})


def with_expired_session_hints(
    app: Callable[..., Awaitable[None]],
    *,
    reconnect_hint: str = DIRECT_CLIENT_RECONNECT_HINT,
) -> ExpiredSessionHintMiddleware:
    """Wrap a Streamable HTTP MCP ASGI app with the recovery contract."""
    return ExpiredSessionHintMiddleware(app, reconnect_hint=reconnect_hint)
