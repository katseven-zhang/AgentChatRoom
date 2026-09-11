"""Tolerant adoption of stateful MCP HTTP sessions whose transport was reaped.

The MCP SDK answers a request that presents an unknown or already-reaped
``mcp-session-id`` with the specification-compliant HTTP 404 ``Session not
found``. Third-party host clients (WorkBuddy, ZCode, ...) do not re-initialise
on that status: they keep replaying the stale bookmark, so after an idle timeout
every tool call fails and the submission loop (``work_report`` /
``review_submit`` / ``message_post``) is blocked by a dead transport session.

This adapter removes that failure mode without weakening the identity model:

* The presented ``mcp-session-id`` is treated only as a client bookmark, never
  as a credential. Identity and Project still come from the Authorization
  credential plus the software identity headers, exactly as before.
* For an unknown/reaped bookmark the adapter performs the MCP initialisation
  handshake server-side (``initialize`` + ``notifications/initialized``), then
  replays the original request against that fresh transport and answers the
  client with 200, publishing the new session id in the response header so a
  compliant client re-books the session transparently.
* The Room Session binding retained in the tombstone table is reattached only
  when the request identity matches the retained binding; a request that
  replays somebody else's bookmark therefore resumes nothing and can only act
  as its own identity.
* ``enabled=False`` (strict mode) keeps the #116 contract: HTTP 404 with
  ``mcp_session_expired`` / ``reconnect_mcp_session``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, MutableMapping

from .http_identity import decode_http_identity_value

logger = logging.getLogger(__name__)

MCP_SESSION_ID_HEADER = b"mcp-session-id"
SOFTWARE_KEY_HEADER = b"x-agentchatroom-software-key"
JSON_CONTENT_TYPE = b"application/json"
SSE_ACCEPT = b"application/json, text/event-stream"
ADOPTED_FROM_HEADER = b"x-agentchatroom-adopted-from"

ADOPTION_CLIENT_NAME = "AgentChatRoom session adoption"
ADOPTION_CLIENT_VERSION = "1"
ADOPTION_PROTOCOL_VERSION = "2025-06-18"
ADOPTION_HANDSHAKE_TIMEOUT_SECONDS = 10.0
ADOPTION_MAP_LIMIT = 512
ADOPTION_MAP_TTL_SECONDS = 86400.0

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]


def _scope_header(scope: Scope, name: bytes) -> str:
    wanted = name.lower()
    for key, value in scope.get("headers") or ():
        if bytes(key).lower() == wanted:
            return bytes(value).decode("latin-1")
    return ""


def _upsert_header(
    headers: list[tuple[bytes, bytes]], name: bytes, value: bytes
) -> list[tuple[bytes, bytes]]:
    wanted = name.lower()
    kept = [(key, item) for key, item in headers if bytes(key).lower() != wanted]
    kept.append((name, value))
    return kept


def _replace_header(
    headers: Any, name: bytes, value: bytes
) -> list[tuple[bytes, bytes]]:
    wanted = name.lower()
    replaced = False
    result: list[tuple[bytes, bytes]] = []
    for key, item in headers or ():
        if bytes(key).lower() == wanted:
            if not replaced:
                result.append((name, value))
                replaced = True
            continue
        result.append((bytes(key), bytes(item)))
    if not replaced:
        result.append((name, value))
    return result


class _Recorder:
    """Minimal ASGI responder used for the server-side initialisation handshake."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.payload = bytearray()
        # The handshake only needs the response head (it carries the new session
        # id). SSE responses stay open after the head, so waiting for the full
        # body would block adoption.
        self.head = asyncio.Event()
        self.first_body = asyncio.Event()
        self._body_sent = False
        self._disconnected = asyncio.Event()

    async def receive(self) -> Message:
        # ASGI receive must truly block once the body is consumed: a streaming
        # (SSE) response polls it in ``while True: await receive()`` looking for
        # ``http.disconnect``, and a receive that returns instantly would turn
        # that poll into a busy loop that wedges the whole event loop.
        if not self._body_sent:
            self._body_sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        if self._disconnected.is_set():
            return {"type": "http.disconnect"}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        message_type = message.get("type")
        if message_type == "http.response.start":
            self.status = int(message.get("status") or 0)
            self.headers = [
                (bytes(key), bytes(value))
                for key, value in (message.get("headers") or ())
            ]
            self.head.set()
        elif message_type == "http.response.body":
            self.payload.extend(message.get("body") or b"")
            self.first_body.set()

    def disconnect(self) -> None:
        self._disconnected.set()

    def header(self, name: bytes) -> str:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value.decode("latin-1")
        return ""


class TolerantSessionAdoptionMiddleware:
    """ASGI wrapper that adopts unknown/reaped MCP session bookmarks."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        session_manager: Any,
        enabled: bool = True,
        is_active: Callable[[str], bool] | None = None,
        lookup_binding: Callable[[str, str], Any] | None = None,
        adopt_binding: Callable[[str, Any], None] | None = None,
        map_limit: int = ADOPTION_MAP_LIMIT,
        map_ttl_seconds: float = ADOPTION_MAP_TTL_SECONDS,
        handshake_timeout_seconds: float = ADOPTION_HANDSHAKE_TIMEOUT_SECONDS,
    ) -> None:
        self.app = app
        self.session_manager = session_manager
        self.enabled = enabled
        self._is_active = is_active
        self._lookup_binding = lookup_binding
        self._adopt_binding = adopt_binding
        self._map_limit = max(1, int(map_limit))
        self._map_ttl_seconds = float(map_ttl_seconds)
        self._handshake_timeout_seconds = float(handshake_timeout_seconds)
        self._adopted: dict[str, tuple[str, float]] = {}
        # Parked synthetic handshake tasks (kept referenced so they are never
        # garbage-collected while still running; each ends when its transport
        # is reaped).
        self._parked: set[asyncio.Task] = set()

    def _park(self, task: asyncio.Task) -> None:
        if task.done():
            return
        self._parked.add(task)
        task.add_done_callback(self._parked.discard)

    async def __call__(
        self,
        scope: Scope,
        receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        if not self.enabled or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "GET").upper()
        if method == "DELETE":
            # An explicit session teardown for an unknown id stays a no-op 404.
            await self.app(scope, receive, send)
            return
        session_id = _scope_header(scope, MCP_SESSION_ID_HEADER)
        if not session_id or self._transport_active(session_id):
            await self.app(scope, receive, send)
            return

        body = await _read_request_body(receive) if method == "POST" else b""
        surrogate = self._active_surrogate(session_id)
        if surrogate is None:
            surrogate = await self._open_surrogate(scope)
            if surrogate is None:
                # The server-side handshake is impossible (invalid credentials,
                # shutdown, ...). Replay untouched so the original, closed
                # behaviour (401/404) still applies instead of hanging.
                await self._forward(scope, send, body, session_id=None)
                return
            self._remember(session_id, surrogate)
            self._restore_room_binding(session_id, surrogate, scope)
            logger.info(
                "Adopted reaped MCP session %s as %s",
                session_id[:16],
                surrogate[:16],
            )
        await self._forward(
            scope, send, body, session_id=surrogate, adopted_from=session_id
        )

    def _transport_active(self, transport_session_id: str) -> bool:
        if self._is_active is not None:
            return bool(self._is_active(transport_session_id))
        active = getattr(self.session_manager, "_server_instances", {})
        transport = active.get(transport_session_id) if isinstance(active, dict) else None
        return transport is not None and not bool(
            getattr(transport, "is_terminated", False)
        )

    def _prune_adopted(self) -> None:
        now = time.monotonic()
        for key, (_, recorded) in list(self._adopted.items()):
            if now - recorded > self._map_ttl_seconds:
                self._adopted.pop(key, None)
        while len(self._adopted) > self._map_limit:
            oldest = min(self._adopted.items(), key=lambda item: item[1][1])[0]
            self._adopted.pop(oldest, None)

    def _active_surrogate(self, original_session_id: str) -> str | None:
        entry = self._adopted.get(original_session_id)
        if entry is None:
            return None
        surrogate, _ = entry
        if not self._transport_active(surrogate):
            self._adopted.pop(original_session_id, None)
            return None
        return surrogate

    def _remember(self, original_session_id: str, surrogate: str) -> None:
        self._adopted[original_session_id] = (surrogate, time.monotonic())
        self._prune_adopted()

    def _restore_room_binding(
        self, original_session_id: str, surrogate: str, scope: Scope
    ) -> None:
        if self._lookup_binding is None or self._adopt_binding is None:
            return
        raw_key = _scope_header(scope, SOFTWARE_KEY_HEADER)
        software_key = ""
        if raw_key:
            try:
                software_key = decode_http_identity_value(raw_key).strip()
            except ValueError:
                software_key = ""
        binding = self._lookup_binding(original_session_id, software_key)
        if binding is None:
            return
        self._adopt_binding(surrogate, binding)

    async def _open_surrogate(self, scope: Scope) -> str | None:
        """Run the MCP initialisation handshake and return the new session id."""
        headers = [
            (bytes(key), bytes(value))
            for key, value in (scope.get("headers") or ())
            if bytes(key).lower() not in {MCP_SESSION_ID_HEADER, b"content-length"}
        ]
        headers = _upsert_header(headers, b"content-type", JSON_CONTENT_TYPE)
        headers = _upsert_header(headers, b"accept", SSE_ACCEPT)
        synthetic_scope: Scope = {**scope, "method": "POST", "headers": headers}
        initialize = {
            "jsonrpc": "2.0",
            "id": "agentchatroom-adopt",
            "method": "initialize",
            "params": {
                "protocolVersion": ADOPTION_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": ADOPTION_CLIENT_NAME,
                    "version": ADOPTION_CLIENT_VERSION,
                },
            },
        }
        recorder = _Recorder(json.dumps(initialize).encode("utf-8"))
        if not await self._call_downstream(
            synthetic_scope, recorder, require_body=True
        ):
            return None
        transport_id = recorder.header(MCP_SESSION_ID_HEADER)
        if not transport_id or recorder.status >= 300:
            return None
        # The transport rejects tool calls until the handshake completes.
        notify_headers = _upsert_header(
            headers, MCP_SESSION_ID_HEADER, transport_id.encode("latin-1")
        )
        notified = _Recorder(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            ).encode("utf-8")
        )
        await self._call_downstream(
            {**synthetic_scope, "headers": notify_headers},
            notified,
            require_body=False,
        )
        return transport_id

    async def _call_downstream(
        self, scope: Scope, recorder: _Recorder, *, require_body: bool
    ) -> bool:
        """Run one synthetic handshake request and capture what adoption needs.

        Returns True when the response head (and, when ``require_body`` is set,
        the first body chunk — the initialize result) was captured.
        """
        task = asyncio.create_task(self.app(scope, recorder.receive, recorder.send))
        try:
            await asyncio.wait_for(
                recorder.head.wait(),
                timeout=self._handshake_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - adoption must fail closed
            logger.warning("Adoption handshake failed: %s", type(error).__name__)
            task.cancel()
            return False
        if require_body:
            try:
                await asyncio.wait_for(
                    recorder.first_body.wait(),
                    timeout=self._handshake_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - the result never arrived
                recorder.disconnect()
                logger.warning("Adoption handshake produced no initialize result")
                task.cancel()
                return False
        # The head (and the initialize result) are captured. A streaming SSE
        # response for the handshake never closes on its own, and cancelling it
        # mid-flight poisons the freshly created transport's response routing
        # (the next business call then loses its result: "Request stream not
        # found"). So the handshake task is parked instead: it holds the
        # synthetic response open and ends only when the transport itself is
        # reaped.
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._handshake_timeout_seconds
            )
        except Exception:  # noqa: BLE001 - the head is already captured
            self._park(task)
        return True

    async def _forward(
        self,
        scope: Scope,
        send: Callable[[Message], Awaitable[None]],
        body: bytes,
        *,
        session_id: str | None,
        adopted_from: str | None = None,
    ) -> None:
        headers = [
            (bytes(key), bytes(value))
            for key, value in (scope.get("headers") or ())
        ]
        if session_id is None:
            # Clean passthrough: nothing was adopted, so the request is replayed
            # exactly as received (including its unknown bookmark) and the
            # original closed behaviour applies unchanged.
            pass
        else:
            headers = [
                (key, value)
                for key, value in headers
                if key.lower() != MCP_SESSION_ID_HEADER
            ]
            headers.append((MCP_SESSION_ID_HEADER, session_id.encode("latin-1")))
            if adopted_from:
                headers.append(
                    (ADOPTED_FROM_HEADER, adopted_from.encode("latin-1"))
                )
        delivered = False
        # Parked forever: after the body is delivered there is no disconnect
        # signal to relay, and a prematurely reported disconnect would truncate
        # a streaming (SSE) response mid-body. POST SSE responses self-close
        # after the JSON-RPC result event, ending the replay naturally.
        parked = asyncio.Event()

        async def replay_receive() -> Message:
            nonlocal delivered
            if delivered:
                await parked.wait()
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def replay_send(message: Message) -> None:
            if (
                message.get("type") == "http.response.start"
                and session_id is not None
            ):
                message = {
                    **message,
                    "headers": _replace_header(
                        message.get("headers"),
                        MCP_SESSION_ID_HEADER,
                        session_id.encode("latin-1"),
                    ),
                }
            await send(message)

        await self.app({**scope, "headers": headers}, replay_receive, replay_send)


async def _read_request_body(
    receive: Callable[[], Awaitable[Message]],
    *,
    limit: int = 8 * 1024 * 1024,
) -> bytes:
    chunks = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunks.extend(message.get("body") or b"")
        if not message.get("more_body") or len(chunks) >= limit:
            break
    return bytes(chunks)


def with_session_adoption(
    app: Callable[..., Awaitable[None]],
    *,
    session_manager: Any,
    enabled: bool = True,
    is_active: Callable[[str], bool] | None = None,
    lookup_binding: Callable[[str, str], Any] | None = None,
    adopt_binding: Callable[[str, Any], None] | None = None,
    map_limit: int = ADOPTION_MAP_LIMIT,
    map_ttl_seconds: float = ADOPTION_MAP_TTL_SECONDS,
) -> TolerantSessionAdoptionMiddleware:
    """Wrap a Streamable HTTP MCP ASGI app with tolerant session adoption."""
    return TolerantSessionAdoptionMiddleware(
        app,
        session_manager=session_manager,
        enabled=enabled,
        is_active=is_active,
        lookup_binding=lookup_binding,
        adopt_binding=adopt_binding,
        map_limit=map_limit,
        map_ttl_seconds=map_ttl_seconds,
    )
