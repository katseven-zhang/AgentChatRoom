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
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable, MutableMapping

from .http_identity import decode_http_identity_value

logger = logging.getLogger(__name__)

MCP_SESSION_ID_HEADER = b"mcp-session-id"
SOFTWARE_KEY_HEADER = b"x-agentchatroom-software-key"
AUTHORIZATION_HEADER = b"authorization"
JSON_CONTENT_TYPE = b"application/json"
SSE_ACCEPT = b"application/json, text/event-stream"
ADOPTED_FROM_HEADER = b"x-agentchatroom-adopted-from"

ADOPTION_CLIENT_NAME = "AgentChatRoom session adoption"
ADOPTION_CLIENT_VERSION = "1"
ADOPTION_PROTOCOL_VERSION = "2025-06-18"
ADOPTION_HANDSHAKE_TIMEOUT_SECONDS = 10.0
ADOPTION_MAP_LIMIT = 512
ADOPTION_MAP_TTL_SECONDS = 86400.0
# Max adoption-path request body. Oversized bodies fail closed with 413
# rather than being silently truncated before replay (#192).
ADOPTION_BODY_LIMIT = 8 * 1024 * 1024

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]


def _software_key(scope: Scope) -> str:
    """The declared software identity of this request, or empty when unusable."""
    raw_key = _scope_header(scope, SOFTWARE_KEY_HEADER)
    if not raw_key:
        return ""
    try:
        return decode_http_identity_value(raw_key).strip()
    except ValueError:
        return ""


def _request_identity(scope: Scope) -> str:
    """A stable, non-reversible fingerprint of who is asking.

    An adopted transport was created with one caller's credential and carries
    that caller's Room binding, so it may only be reused by the same caller. The
    fingerprint covers the credential and the declared software identity; a
    rotated token or a different install therefore opens its own surrogate
    instead of borrowing someone else's. Only a digest is kept, never the secret.
    """
    material = "\x00".join(
        (_scope_header(scope, AUTHORIZATION_HEADER), _software_key(scope))
    )
    return hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()


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


class PayloadTooLargeError(Exception):
    """Raised when an adoption-path request body exceeds the configured limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"request body exceeds {limit} bytes")
        self.limit = limit


class IncompleteRequestBodyError(Exception):
    """Raised when the client aborted before the adoption-path body completed."""

    def __init__(self, received: int) -> None:
        super().__init__(f"request body incomplete after {received} bytes")
        self.received = received


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
        max_parked_handshakes: int = 32,
        body_limit: int = ADOPTION_BODY_LIMIT,
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
        self._max_parked = max(1, int(max_parked_handshakes))
        self._body_limit = max(1, int(body_limit))
        self._adopted: dict[tuple[str, str], tuple[str, float]] = {}
        # One adoption at a time per (bookmark, identity): the read-check-write
        # around ``_open_surrogate`` spans awaits, so without this a client
        # retrying in parallel opened a separate transport per request and each
        # one parked a handshake task until the bound failed adoption closed.
        self._adopting: dict[tuple[str, str], asyncio.Lock] = {}
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
        # A bookmark alone never identifies a caller, so it cannot identify an
        # adopted transport either: the surrogate was opened with its creator's
        # credential and re-attached to that creator's Room binding.
        adoption_key = (session_id, _request_identity(scope))

        body = b""
        body_consumed = False
        if method == "POST":
            body_consumed = True
            try:
                body = await _read_request_body(receive, limit=self._body_limit)
            except PayloadTooLargeError as error:
                # Fail closed with a structured 413 instead of replaying a
                # silently truncated body that only fails as a parse error (#192).
                await self._send_payload_too_large(send, error.limit)
                return
            except IncompleteRequestBodyError as error:
                # The client is already gone: nothing may be replayed and no
                # surrogate may be opened for a request that never completed.
                await self._send_incomplete_body(send, error.received)
                return
        surrogate = self._active_surrogate(adoption_key)
        if surrogate is None:
            async with self._adoption_lock(adoption_key):
                # Re-check inside the lock: a parallel request with the same
                # bookmark and identity may already have finished the handshake,
                # and sharing that transport is the whole point.
                surrogate = self._active_surrogate(adoption_key)
                if surrogate is None:
                    surrogate = await self._open_surrogate(scope)
                    if surrogate is None:
                        # The server-side handshake is impossible (invalid
                        # credentials, shutdown, ...). Replay untouched so the
                        # original, closed behaviour (401/404) still applies
                        # instead of hanging.
                        await self._forward(
                            scope,
                            send,
                            body,
                            session_id=None,
                            upstream_receive=receive,
                            body_consumed=body_consumed,
                        )
                        return
                    self._remember(adoption_key, surrogate)
                    logger.info(
                        "Adopted reaped MCP session %s as %s",
                        session_id[:16],
                        surrogate[:16],
                    )
        # Run on every request, not only on the first adoption: the retained
        # binding is re-attached to the surrogate under the same identity check,
        # so a reuse cannot outlive the comparison that authorised it.
        self._restore_room_binding(session_id, surrogate, scope)
        await self._forward(
            scope,
            send,
            body,
            session_id=surrogate,
            adopted_from=session_id,
            upstream_receive=receive,
            body_consumed=body_consumed,
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
        # A guard lock is only worth keeping while it still names a live mapping
        # or an adoption in flight; otherwise the lock table would outgrow the
        # bounded map it protects.
        for key, lock in list(self._adopting.items()):
            if key not in self._adopted and not lock.locked():
                self._adopting.pop(key, None)
        while len(self._adopting) > self._map_limit:
            idle = next(
                (k for k, lock in self._adopting.items() if not lock.locked()), None
            )
            if idle is None:
                break
            self._adopting.pop(idle, None)

    def _adoption_lock(self, adoption_key: tuple[str, str]) -> asyncio.Lock:
        lock = self._adopting.get(adoption_key)
        if lock is None:
            lock = asyncio.Lock()
            self._adopting[adoption_key] = lock
        return lock

    def _active_surrogate(self, adoption_key: tuple[str, str]) -> str | None:
        entry = self._adopted.get(adoption_key)
        if entry is None:
            return None
        surrogate, _ = entry
        if not self._transport_active(surrogate):
            self._adopted.pop(adoption_key, None)
            return None
        return surrogate

    def _remember(self, adoption_key: tuple[str, str], surrogate: str) -> None:
        self._adopted[adoption_key] = (surrogate, time.monotonic())
        self._prune_adopted()

    def _restore_room_binding(
        self, original_session_id: str, surrogate: str, scope: Scope
    ) -> None:
        if self._lookup_binding is None or self._adopt_binding is None:
            return
        binding = self._lookup_binding(original_session_id, _software_key(scope))
        if binding is None:
            return
        self._adopt_binding(surrogate, binding)

    async def _send_payload_too_large(
        self, send: Callable[[Message], Awaitable[None]], limit: int
    ) -> None:
        payload = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"Request body exceeds {limit} bytes",
                    "data": {
                        "code": "payload_too_large",
                        "limit": limit,
                        "required_action": "reduce_request_size",
                    },
                }
            }
        ).encode("utf-8")
        logger.warning(
            "Adoption rejected oversized request body (limit=%d bytes)", limit
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def _send_incomplete_body(
        self, send: Callable[[Message], Awaitable[None]], received: int
    ) -> None:
        payload = json.dumps(
            {
                "error": {
                    "code": "request_body_incomplete",
                    "message": "Client disconnected before the request body completed",
                    "data": {
                        "code": "request_body_incomplete",
                        "received_bytes": received,
                        "required_action": "resend_request",
                    },
                }
            }
        ).encode("utf-8")
        logger.warning(
            "Adoption dropped request aborted after %d body bytes", received
        )
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

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
        # reaped. The parked set is bounded; when it is full the adoption fails
        # closed (the original request is replayed untouched) rather than
        # letting parked tasks accumulate without limit.
        if len(self._parked) >= self._max_parked:
            logger.warning(
                "Adoption parked-handshake limit reached (%d); failing closed",
                self._max_parked,
            )
            task.cancel()
            return False
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
        upstream_receive: Callable[[], Awaitable[Message]],
        adopted_from: str | None = None,
        body_consumed: bool = False,
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

        async def replay_receive() -> Message:
            nonlocal delivered
            if body_consumed and not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            # The buffered body has been handed over, so the transport's own
            # receive is the only source of a disconnect. Parking here instead
            # meant a real client abort never reached the downstream app: the
            # adopted request kept its stream alive until the transport was
            # reaped (#306). Nothing is invented -- http.disconnect arrives when
            # it actually happens, never synthesised early, so a streaming (SSE)
            # response is still not truncated mid-body.
            return await upstream_receive()

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
    limit: int = ADOPTION_BODY_LIMIT,
) -> bytes:
    """Buffer a POST body, failing closed past ``limit`` bytes (#192).

    Never returns a truncated body: hitting the limit raises
    :class:`PayloadTooLargeError` so the middleware can answer 413 instead
    of replaying incomplete JSON-RPC downstream, and an abort before the
    body completes raises :class:`IncompleteRequestBodyError` (#306).
    """
    chunks = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            # The client went away mid-body. The bytes buffered so far are a
            # prefix of a request, not a request: returning them would replay a
            # truncated JSON-RPC call downstream as though it were complete.
            raise IncompleteRequestBodyError(len(chunks))
        chunks.extend(message.get("body") or b"")
        if len(chunks) > limit:
            raise PayloadTooLargeError(limit)
        if not message.get("more_body"):
            break
        if len(chunks) >= limit:
            # More body is still coming but we are already at the limit —
            # truncating here would silently corrupt the replayed request.
            raise PayloadTooLargeError(limit)
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
    max_parked_handshakes: int = 32,
    body_limit: int = ADOPTION_BODY_LIMIT,
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
        max_parked_handshakes=max_parked_handshakes,
        body_limit=body_limit,
    )
