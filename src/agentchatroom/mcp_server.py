from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import socket
import sys
import threading
import time
import weakref
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from . import __version__
from .config import load_settings
from .contracts import ReviewCriterion, TestEvidence
from .credential_bundle import (
    CredentialBundleError,
    decode_project_credential_bundle,
    is_project_credential_bundle,
)
from .database import create_database
from .errors import DomainError
from .http_identity import decode_http_identity_value
from .mcp_compat import CompatibleToolManager
from .presence import LocalPresenceManager
from .service_lifetime import require_running_service
from .bootstrap import (
    RuntimeBinding,
    bind_runtime_arguments,
    bootstrap_local_room,
    bootstrap_status_payload,
    configured_software_identity,
    find_registered_checkout,
    workspace_path_from_file_uri,
    PROJECT_PATH_ENV,
    REQUIRED_ACTIONS,
    SOFTWARE_CLIENT_ENV,
    SOFTWARE_KEY_ENV,
    SOFTWARE_NAME_ENV,
)
from .project_registration import (
    register_checkout_project,
    resolve_checkout_project_key,
    validate_project_scope,
)
from .services import AgentChatRoomService, new_id, parse_time


service: AgentChatRoomService | None = None
presence_manager: LocalPresenceManager | None = None
logger = logging.getLogger(__name__)
ServiceProvider = Callable[[], AgentChatRoomService]
_bound_service_provider: ContextVar[ServiceProvider | None] = ContextVar(
    "agentchatroom_mcp_service_provider",
    default=None,
)
_bound_presence_manager: ContextVar[LocalPresenceManager | None] = ContextVar(
    "agentchatroom_mcp_presence_manager",
    default=None,
)
_runtime_binding: ContextVar[RuntimeBinding | None] = ContextVar(
    "agentchatroom_runtime_binding",
    default=None,
)
_session_bindings: dict[str, RuntimeBinding | None] = {}
_session_bindings_lock = threading.RLock()
# Bounded tombstone table: transport session id -> (Room Session binding, recorded
# at). It lets a request that presents a reaped mcp-session-id adopt a fresh
# transport while resuming the same Room Session. The transport id is only a
# client bookmark; identity is always re-derived from credentials and headers.
_transport_tombstones: dict[str, tuple[RuntimeBinding, float]] = {}
_TOMBSTONE_DEFAULT_LIMIT = 256
_TOMBSTONE_DEFAULT_TTL_SECONDS = 86400.0
_tombstone_limit = _TOMBSTONE_DEFAULT_LIMIT
_tombstone_ttl_seconds = _TOMBSTONE_DEFAULT_TTL_SECONDS
_transport_keys: dict[int, tuple[weakref.ReferenceType, str]] = {}
_active_binding_key: ContextVar[str | None] = ContextVar("mcp_binding_key", default=None)
_loaded_identity: tuple[str, str, str] | None = None
_stdio_settings = None
_stdio_generation: str | None = None
_bootstrap_workspace_roots: ContextVar[list[Path] | None] = ContextVar(
    "agentchatroom_bootstrap_workspace_roots",
    default=None,
)
_bootstrap_http_identity: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "agentchatroom_bootstrap_http_identity",
    default=None,
)

SOFTWARE_KEY_HEADER = "x-agentchatroom-software-key"
SOFTWARE_NAME_HEADER = "x-agentchatroom-software-name"
SOFTWARE_CLIENT_HEADER = "x-agentchatroom-software-client"
MCP_SESSION_ID_HEADER = "mcp-session-id"
HTTP_TRANSPORT_KEY_PREFIX = "http-session:"
HTTP_UNREGISTERED_ROOTS_ACTION = (
    "Confirm the MCP client advertised workspace roots match a Project already "
    "registered on this server (root_path, logical_path, or a previously "
    "registered workspace path). If the workspace is new, create or open it in "
    "the Web UI, then issue an Agent Token for that Project. Do not reuse a "
    "token or server entry from another Project."
)


class ServiceBoundToolManager(CompatibleToolManager):
    def __init__(
        self,
        service_provider: ServiceProvider | None = None,
        presence: LocalPresenceManager | None = None,
    ) -> None:
        super().__init__()
        self.service_provider = service_provider
        self.presence = presence

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        provider_token = None
        roots_token = None
        http_identity_token = None
        presence_token = None
        try:
            session_key = mcp_session_key(context)
        except DomainError as error:
            return {"ok": False, **error.as_dict()}
        key_token = _active_binding_key.set(session_key)
        previous_binding = _runtime_binding.get()
        if self.service_provider is not None:
            provider_token = _bound_service_provider.set(self.service_provider)
        if self.presence is not None:
            presence_token = _bound_presence_manager.set(self.presence)
        try:
            if _stdio_settings is not None and self.service_provider is None:
                try:
                    _check_stdio_lifetime()
                except DomainError:
                    clear_runtime_binding()
                    persist_runtime_binding(session_key, None)
                    _runtime_binding.set(None)
                    raise
            forwarded = dict(arguments or {})
            restored = get_runtime_binding(session_key)
            _runtime_binding.set(restored)
            with _session_bindings_lock:
                invalidated = session_key in _session_bindings and restored is None
            if invalidated and name != "room_bootstrap":
                raise DomainError("session_expired", "Bootstrap must succeed before using this connection", status_code=401)
            if name == "room_bootstrap":
                access = get_access_token() if self.service_provider is not None else None
                bundled_project_id = None
                uses_bundle = bool(
                    access is not None
                    and (access.claims or {}).get("credential_bundle")
                )
                if uses_bundle:
                    bundled_project_id = _bundle_project_for_name(
                        access,
                        str(forwarded.get("project_name") or ""),
                    )
                    if restored is not None and restored.project_id != bundled_project_id:
                        raise DomainError(
                            "project_session_rebind_forbidden",
                            "This MCP Session is already bound to another Project; open a new client task Session",
                            status_code=409,
                        )
                # Rebinding must fail closed, including identity and roots errors.
                persist_runtime_binding(session_key, None)
                _runtime_binding.set(None)
                roots_token = _bootstrap_workspace_roots.set(
                    await collect_mcp_workspace_roots(
                        context,
                        timeout_seconds=get_service().settings.mcp_roots_timeout_seconds,
                        required=not uses_bundle,
                    )
                )
                if self.service_provider is not None:
                    http_identity_token = _bootstrap_http_identity.set(
                        _software_identity_from_http(context)
                    )
            elif name != "room_join":
                tool = self.get_tool(name)
                if tool is not None:
                    forwarded = bind_runtime_arguments(
                        list(inspect.signature(tool.fn).parameters),
                        forwarded,
                        get_runtime_binding(session_key),
                    )
            result = await super().call_tool(
                name,
                forwarded,
                context=context,
                convert_result=convert_result,
            )
            if name == "room_bootstrap":
                persist_runtime_binding(session_key, _runtime_binding.get())
            elif name == "session_leave":
                persist_runtime_binding(session_key, None)
                clear_bindings_for_agent_session(str(forwarded.get("session_id") or ""))
            return result
        except DomainError as error:
            return {"ok": False, **error.as_dict()}
        finally:
            if http_identity_token is not None:
                _bootstrap_http_identity.reset(http_identity_token)
            if roots_token is not None:
                _bootstrap_workspace_roots.reset(roots_token)
            if name == "session_leave":
                _runtime_binding.set(None)
            else:
                _runtime_binding.set(previous_binding)
            if provider_token is not None:
                _bound_service_provider.reset(provider_token)
            if presence_token is not None:
                _bound_presence_manager.reset(presence_token)
            _active_binding_key.reset(key_token)


MCP_INSTRUCTIONS = (
    "Call room_bootstrap once at the start of a new conversation before project "
    "work. Do not read or edit mcp.json, config.toml, AgentChatRoom source, or "
    "the database on the normal path. Never supply, infer, or replace a "
    "project_key, logical_path, agent_key, member_id, software name, or client "
    "type. Agents also do not supply logical_path. Local stdio resolves ignored "
    ".agentchatroom/project.json; the MCP process owns one configured software "
    "identity and holds the Session Token in memory. HTTP multi-Project configs "
    "supply only the non-secret project_name generated by onboarding; Project "
    "tokens stay in MCP configuration. One MCP Session binds one Project and "
    "cannot switch to another; parallel client tasks use parallel Sessions and "
    "keep independent task ownership. A new Session never moves another "
    "Session's work. Use task_claim(reclaim=true) only to explicitly resume an "
    "unfinished task whose same-software owner is disconnected. Do not copy project_id, "
    "session_id, or token between tools. Presence from MCP startup is not "
    "conversation sync. If room_bootstrap returns identity_not_configured, use "
    "the local MCP configuration assistant; otherwise follow the single "
    "required_action. room_join remains a compatibility entry and may create a "
    "Room only when the repository scope is genuinely empty. Sync, claim, lease, "
    "and report through the current binding. Every Agent-authored message must "
    "include model_display_name exactly as shown in the client UI."
)


def _configured_local_identity() -> tuple[str, str, str] | None:
    return configured_software_identity()


def _identity_tuple(*values: Any) -> tuple[str, str, str] | None:
    parsed = tuple(str(value or "").strip() for value in values)
    if len(parsed) != 3:
        return None
    if not all(parsed) or any(value.startswith("<") for value in parsed):
        return None
    return parsed[0], parsed[1], parsed[2]


def _identity_from_member(
    project_id: str,
    member_id: str,
    *,
    room_service: AgentChatRoomService | None = None,
) -> tuple[str, str, str] | None:
    if not project_id or not member_id:
        return None
    try:
        members = (room_service or get_service()).list_project_members(
            project_id, include_revoked=False
        )
    except DomainError:
        return None
    member = next((item for item in members if str(item.get("id") or "") == member_id), None)
    if member is None:
        return None
    metadata = member.get("metadata") or {}
    return _identity_tuple(
        metadata.get("software_key"),
        member.get("name"),
        metadata.get("client"),
    )


def _identity_from_credential(project_id: str, credential_id: str) -> tuple[str, str, str] | None:
    if not project_id or not credential_id:
        return None
    try:
        tokens = get_service().list_agent_tokens(project_id)
    except DomainError:
        return None
    credential = next(
        (item for item in tokens if str(item.get("id") or "") == credential_id),
        None,
    )
    if credential is None:
        return None
    return _identity_from_member(project_id, str(credential.get("member_id") or ""))


def _software_identity_from_http(context: Any) -> tuple[str, str, str] | None:
    credential_identity: tuple[str, str, str] | None = None
    try:
        access = get_access_token()
    except LookupError:
        access = None
    if access is not None:
        claims = access.claims or {}
        credential_identity = _identity_tuple(
            claims.get("software_key"),
            claims.get("software_name"),
            claims.get("software_client"),
        )
        if credential_identity is None:
            credential_identity = _identity_from_credential(
                str(claims.get("project_id") or ""),
                str(claims.get("credential_id") or ""),
            )
    request_context = getattr(context, "request_context", None) if context is not None else None
    if request_context is None and context is not None:
        request_context = getattr(context, "_request_context", None)
    request = getattr(request_context, "request", None) if request_context is not None else None
    headers = getattr(request, "headers", None)
    if headers is None:
        return credential_identity
    try:
        header_values = tuple(
            decode_http_identity_value(value)
            if (value := headers.get(header_name)) is not None
            else None
            for header_name in (
                SOFTWARE_KEY_HEADER,
                SOFTWARE_NAME_HEADER,
                SOFTWARE_CLIENT_HEADER,
            )
        )
    except ValueError as error:
        raise DomainError(
            "invalid_software_identity_header",
            str(error),
            status_code=400,
        ) from error
    header_identity = _identity_tuple(*header_values)
    if (
        credential_identity is not None
        and header_identity is not None
        and credential_identity != header_identity
    ):
        raise DomainError(
            "software_identity_mismatch",
            "HTTP software identity headers do not match the linked Agent Token identity",
            status_code=403,
            details={
                "required_action": "restore_linked_software_identity_or_issue_unlinked_token",
            },
        )
    return credential_identity or header_identity


def mcp_session_key(context: Any = None) -> str:
    request_context = getattr(context, "request_context", None) if context is not None else None
    if request_context is None and context is not None:
        request_context = getattr(context, "_request_context", None)
    request = getattr(request_context, "request", None) if request_context is not None else None
    headers = getattr(request, "headers", None)
    if headers is not None:
        http_session_id = str(headers.get(MCP_SESSION_ID_HEADER) or "").strip()
        if http_session_id:
            return f"{HTTP_TRANSPORT_KEY_PREFIX}{http_session_id}"
    session = getattr(request_context, "session", None) if request_context is not None else None
    if session is None and context is not None:
        session = getattr(context, "session", None)
    # The SDK ServerSession does not expose the HTTP transport's session ID.
    # Bind to the actual connection object, never to its credentials: separate
    # connections may legitimately share a software identity and access token.
    if session is not None:
        object_id = id(session)
        with _session_bindings_lock:
            existing = _transport_keys.get(object_id)
            if existing is not None and existing[0]() is session:
                return existing[1]
            key = new_id("transport")
            def released(reference):
                with _session_bindings_lock:
                    entry = _transport_keys.get(object_id)
                    if entry is not None and entry[0] is reference:
                        _transport_keys.pop(object_id, None)
                        _session_bindings.pop(key, None)
            try:
                reference = weakref.ref(session, released)
            except TypeError:
                # Lightweight adapters must retain their own opaque key.
                key = getattr(session, "_acr_binding_key", None) or key
                setattr(session, "_acr_binding_key", key)
            else:
                _transport_keys[object_id] = (reference, key)
            return key
    try:
        access = get_access_token()
    except LookupError:
        access = None
    token = getattr(access, "token", "") if access is not None else ""
    if token or context is not None:
        raise DomainError("runtime_context_missing", "A distinct MCP connection context is required", status_code=409)
    return "stdio:local"


def transport_binding_alive(session_key: str) -> bool:
    """Return whether an MCP connection object still owns this binding key."""
    if not session_key or session_key == "stdio:local":
        return True
    with _session_bindings_lock:
        return any(
            reference() is not None and key == session_key
            for reference, key in _transport_keys.values()
        )


def transport_session_active(session_manager: Any, transport_session_id: str) -> bool:
    """Return whether the SDK still owns this stateful HTTP transport."""
    if not transport_session_id:
        return False
    active = getattr(session_manager, "_server_instances", {})
    transport = active.get(transport_session_id) if isinstance(active, dict) else None
    return transport is not None and not bool(
        getattr(transport, "is_terminated", False)
    )


def configure_transport_tombstones(
    *,
    limit: int | None = None,
    ttl_seconds: float | None = None,
) -> None:
    """Bound the adoption tombstone table (count and age)."""
    global _tombstone_limit, _tombstone_ttl_seconds
    with _session_bindings_lock:
        if limit is not None and limit > 0:
            _tombstone_limit = int(limit)
        if ttl_seconds is not None and ttl_seconds > 0:
            _tombstone_ttl_seconds = float(ttl_seconds)
        _prune_transport_tombstones_locked()


def _prune_transport_tombstones_locked() -> None:
    now = time.monotonic()
    for key, (_, recorded) in list(_transport_tombstones.items()):
        if now - recorded > _tombstone_ttl_seconds:
            _transport_tombstones.pop(key, None)
    while len(_transport_tombstones) > _tombstone_limit:
        oldest = min(_transport_tombstones.items(), key=lambda item: item[1][1])[0]
        _transport_tombstones.pop(oldest, None)


def retain_transport_tombstone(transport_session_id: str) -> RuntimeBinding | None:
    """Move a reaped transport's Room Session binding into the tombstone table."""
    if not transport_session_id:
        return None
    key = f"{HTTP_TRANSPORT_KEY_PREFIX}{transport_session_id}"
    with _session_bindings_lock:
        binding = _session_bindings.pop(key, None)
        if binding is not None:
            _transport_tombstones[transport_session_id] = (binding, time.monotonic())
        _prune_transport_tombstones_locked()
        return binding


def lookup_transport_tombstone(
    transport_session_id: str,
    software_key: str = "",
) -> RuntimeBinding | None:
    """Return a retained binding, refusing any identity that does not match it.

    The presented transport id is only a bookmark: adoption must never move a
    Room Session to a different software identity.
    """
    if not transport_session_id:
        return None
    with _session_bindings_lock:
        _prune_transport_tombstones_locked()
        entry = _transport_tombstones.get(transport_session_id)
    if entry is None:
        return None
    binding = entry[0]
    expected = software_key.strip()
    # Identity is mandatory for adoption: a request that presents no verifiable
    # software key must never resume somebody else's Room Session, so an
    # empty or mismatching key both refuse the binding.
    if not expected or not binding.software_key or binding.software_key != expected:
        return None
    return binding


def adopt_transport_binding(
    transport_session_id: str,
    binding: RuntimeBinding | None,
) -> None:
    """Attach a retained Room Session binding to an adopted transport."""
    if not transport_session_id or binding is None:
        return
    persist_runtime_binding(f"{HTTP_TRANSPORT_KEY_PREFIX}{transport_session_id}", binding)


def clear_transport_tombstones() -> None:
    with _session_bindings_lock:
        _transport_tombstones.clear()


def session_transport_state(project_id: str, session_id: str) -> str:
    """Report whether a Room Session's HTTP transport is 'alive', 'gone', or
    'unknown'.

    A session whose transport binding now lives in the tombstone table is
    'gone': hard evidence the owning client disconnected, which lets the same
    software identity reclaim immediately. Sessions without any HTTP transport
    binding (stdio, tests) are 'unknown' so callers fall back to the heartbeat
    window.
    """
    with _session_bindings_lock:
        for key, binding in _session_bindings.items():
            if (
                key.startswith(HTTP_TRANSPORT_KEY_PREFIX)
                and binding is not None
                and str(getattr(binding, "project_id", "")) == str(project_id)
                and str(getattr(binding, "session_id", "")) == str(session_id)
            ):
                return "alive"
        for _transport_id, (binding, _recorded) in _transport_tombstones.items():
            if (
                str(getattr(binding, "project_id", "")) == str(project_id)
                and str(getattr(binding, "session_id", "")) == str(session_id)
            ):
                return "gone"
    return "unknown"


def http_transport_binding_alive(session_manager: Any, session_key: str) -> bool:
    """Check a stateful HTTP binding against the SDK's active transport map.

    A binding whose transport is gone is retained as a tombstone instead of being
    dropped, so a later request presenting that (now unknown) id can adopt a fresh
    transport and resume the same Room Session.
    """
    if not session_key.startswith(HTTP_TRANSPORT_KEY_PREFIX):
        return transport_binding_alive(session_key)
    http_session_id = session_key[len(HTTP_TRANSPORT_KEY_PREFIX) :]
    if transport_session_active(session_manager, http_session_id):
        return True
    retain_transport_tombstone(http_session_id)
    return False


def get_runtime_binding(session_key: str | None = None) -> RuntimeBinding | None:
    key = session_key or "stdio:local"
    with _session_bindings_lock:
        if key in _session_bindings:
            return _session_bindings[key]
        if session_key is not None:
            return None
    return _runtime_binding.get()


def persist_runtime_binding(session_key: str, binding: RuntimeBinding | None) -> None:
    with _session_bindings_lock:
        _session_bindings[session_key] = binding


def set_runtime_binding(
    binding: RuntimeBinding | None,
    session_key: str | None = None,
) -> None:
    _runtime_binding.set(binding)
    persist_runtime_binding(session_key or _active_binding_key.get() or "stdio:local", binding)


def clear_runtime_binding(session_key: str | None = None) -> None:
    _runtime_binding.set(None)
    if session_key is None:
        with _session_bindings_lock:
            _session_bindings.clear()
            _transport_tombstones.clear()
        return
    persist_runtime_binding(session_key, None)


def clear_bindings_for_agent_session(session_id: str) -> None:
    if not session_id:
        return
    with _session_bindings_lock:
        for key, binding in list(_session_bindings.items()):
            if binding is not None and binding.session_id == session_id:
                _session_bindings[key] = None
    current = _runtime_binding.get()
    if current is not None and current.session_id == session_id:
        _runtime_binding.set(None)


def _http_roots_required_error(message: str) -> DomainError:
    return DomainError(
        "workspace_roots_unavailable",
        message,
        status_code=409,
        details={
            "required_action": REQUIRED_ACTIONS["ambiguous_workspace"],
            "http_correct_action": HTTP_UNREGISTERED_ROOTS_ACTION,
        },
    )


async def collect_mcp_workspace_roots(
    context: Any,
    *,
    timeout_seconds: float = 5.0,
    required: bool = True,
) -> list[Path]:
    bound = _bound_service_provider.get() is not None
    if context is None:
        if bound and required:
            raise _http_roots_required_error(
                "HTTP MCP requires the client to advertise workspace roots"
            )
        return []
    request_context = getattr(context, "request_context", None)
    if request_context is None:
        request_context = getattr(context, "_request_context", None)
    session = getattr(request_context, "session", None) if request_context is not None else None
    if session is None:
        session = getattr(context, "session", None)
    list_roots = getattr(session, "list_roots", None) if session is not None else None
    if list_roots is None:
        if bound and required:
            raise _http_roots_required_error(
                "HTTP MCP requires the client to advertise workspace roots"
            )
        return []
    client_params = getattr(session, "client_params", None)
    if client_params is not None and getattr(client_params.capabilities, "roots", None) is None:
        if bound and required:
            raise _http_roots_required_error(
                "HTTP MCP requires the client to advertise workspace roots"
            )
        return []
    try:
        result = await asyncio.wait_for(list_roots(), timeout=timeout_seconds)
    except TimeoutError as error:
        raise DomainError("workspace_roots_unavailable", "Workspace roots timed out; restore the client connection before bootstrap", status_code=409) from error
    except Exception as error:
        raise DomainError("workspace_roots_unavailable", "Workspace roots failed; restore the client connection before bootstrap", status_code=409) from error
    roots: list[Path] = []
    for root in getattr(result, "roots", None) or []:
        uri = str(getattr(root, "uri", "") or "")
        path = workspace_path_from_file_uri(uri)
        if path is not None:
            roots.append(path)
        else:
            raise DomainError("workspace_roots_unavailable", "Workspace root is not a valid file URI", status_code=409)
    if not roots:
        raise DomainError("workspace_roots_unavailable", "Client supplied an empty workspace", status_code=409)
    return roots


def _auto_join_local_checkout() -> dict[str, Any] | None:
    """Create startup Presence only for a fully configured registered checkout.

    Startup Presence follows the same workspace precedence as room_bootstrap:
    the current working directory's checkout wins; the configured project path
    is only a fallback, so a stale pin cannot pull presence into another Room.
    """
    if _configured_local_identity() is None:
        return None
    target = find_registered_checkout(Path.cwd())
    if target is None:
        return None
    try:
        registered_project_key, _ = resolve_checkout_project_key(target)
    except DomainError as error:
        logger.warning("Local MCP auto-join skipped: %s", error.code)
        return None
    if not registered_project_key:
        return None
    response = room_join(project_path=str(target), model="unknown")
    if not response.get("ok"):
        error = response.get("error") or {}
        logger.warning(
            "Local MCP auto-join skipped: %s",
            error.get("code", "unknown_error"),
        )
        return None
    return response["result"]


def _new_mcp(
    service_provider: ServiceProvider | None = None,
    *,
    presence: LocalPresenceManager | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    stateless_http: bool = False,
    json_response: bool = True,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> FastMCP:
    server = FastMCP(
        "AgentChatRoom",
        instructions=MCP_INSTRUCTIONS,
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
        stateless_http=stateless_http,
        json_response=json_response,
        token_verifier=token_verifier,
        auth=auth,
    )
    server._tool_manager = ServiceBoundToolManager(service_provider, presence)
    return server

mcp = _new_mcp()


def _check_stdio_lifetime() -> None:
    global _stdio_generation
    generation = require_running_service(_stdio_settings)
    if generation != _stdio_generation:
        # A stop/start between calls must not preserve an old writable binding.
        with _session_bindings_lock:
            for key in _session_bindings:
                _session_bindings[key] = None
        _runtime_binding.set(None)
        _stdio_generation = generation


def get_service() -> AgentChatRoomService:
    global service
    provider = _bound_service_provider.get()
    if provider is not None:
        return provider()
    if _stdio_settings is not None:
        _check_stdio_lifetime()
    if service is None:
        settings = load_settings()
        service = AgentChatRoomService(create_database(settings), settings)
        service.initialize()
    return service


def _tool_result(callable_, *args, **kwargs) -> dict[str, Any]:
    try:
        return {"ok": True, "result": callable_(*args, **kwargs)}
    except DomainError as error:
        return {"ok": False, **error.as_dict()}


def _mcp_request_id(request_id: str) -> str:
    return request_id.strip() or new_id("request")


def _register_local_presence(payload: dict[str, Any]) -> None:
    manager = _bound_presence_manager.get() or presence_manager
    if manager is None:
        return
    project = payload.get("project") or {}
    agent = payload.get("agent") or {}
    token = str(payload.get("token", ""))
    if project.get("id") and agent.get("id") and token:
        manager.register(
            str(project["id"]),
            str(agent["id"]),
            token,
            agent_key=str(agent.get("agent_key") or agent["id"]),
            transport_key=_active_binding_key.get() or "",
        )


def _ensure_local_presence(project_id: str, session_id: str, token: str) -> None:
    """Re-register a live session with the local presence manager.

    The presence registry is process-local memory and empties on MCP process
    restart, which strands previously-registered sessions: heartbeats stop,
    and the UI shows a busy agent as disconnected. A successful authenticated
    tool call proves this process still owns the session, so use it to resume
    the background heartbeat keepalive automatically.
    """
    manager = _bound_presence_manager.get() or presence_manager
    if manager is None:
        return
    manager.ensure_registered(
        project_id,
        session_id,
        token,
        transport_key=_active_binding_key.get() or "",
    )


def _project_access_claim(
    access: AccessToken,
    project_id: str,
) -> dict[str, Any] | None:
    claims = access.claims or {}
    project_credentials = claims.get("project_credentials")
    if not isinstance(project_credentials, dict):
        return None
    selected = project_credentials.get(project_id)
    return selected if isinstance(selected, dict) else None


def _bundle_project_for_name(access: AccessToken, project_name: str) -> str:
    requested = project_name.strip()
    claims = access.claims or {}
    project_credentials = claims.get("project_credentials") or {}
    unavailable = claims.get("unavailable_project_credentials") or {}
    available_names = sorted(
        str(item.get("name") or "")
        for item in project_credentials.values()
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    )
    configured_names = sorted(
        set(available_names)
        | {
            str(name)
            for name in unavailable
            if str(name).strip()
        }
    )
    if not requested:
        raise DomainError(
            "project_credential_name_required",
            "room_bootstrap requires the configured Project credential name",
            status_code=409,
            details={
                "required_action": "call_room_bootstrap_with_project_name",
                "available_project_names": available_names,
                "configured_project_names": configured_names,
            },
        )
    unavailable_item = unavailable.get(requested)
    if isinstance(unavailable_item, dict):
        reason = str(unavailable_item.get("reason") or "agent_token_invalid")
        if reason == "agent_token_expired":
            code = "project_credential_expired"
            action = "renew_project_token"
            message = "The selected Project Token has expired"
        elif reason == "agent_token_revoked":
            code = "project_credential_revoked"
            action = "issue_new_project_token_and_update_bundle"
            message = "The selected Project Token has been revoked"
        else:
            code = "project_credential_invalid"
            action = "replace_project_token_in_bundle"
            message = "The selected Project Token is invalid"
        raise DomainError(
            code,
            message,
            status_code=403,
            details={
                "project_name": requested,
                "required_action": action,
                "available_project_names": available_names,
                "configured_project_names": configured_names,
            },
        )
    matches = [
        str(project_id)
        for project_id, item in project_credentials.items()
        if isinstance(item, dict) and str(item.get("name") or "").strip() == requested
    ]
    if len(matches) != 1:
        raise DomainError(
            "project_credential_name_mismatch",
            "The configured Project credential name is missing or ambiguous",
            status_code=403,
            details={
                "required_action": "call_room_bootstrap_with_project_name",
                "available_project_names": available_names,
                "configured_project_names": configured_names,
            },
        )
    return matches[0]


def _current_access_uses_bundle() -> bool:
    access = get_access_token()
    return bool(access is not None and (access.claims or {}).get("credential_bundle"))


def _authorize_remote(
    project_id: str,
    permission: str,
    *,
    project_name: str = "",
    require_project_name: bool = False,
) -> AccessToken | None:
    access = get_access_token()
    if access is None:
        return None
    claims = access.claims or {}
    bundled = bool(claims.get("credential_bundle"))
    selected = _project_access_claim(access, project_id) if bundled else None
    if bundled and selected is None:
        raise DomainError(
            "agent_token_project_forbidden",
            "The MCP credential set has no valid token for this Project",
            status_code=403,
        )
    if not bundled and claims.get("project_id") != project_id:
        raise DomainError(
            "agent_token_project_forbidden",
            "Agent token is not authorized for this Project",
            status_code=403,
        )
    profile_name = str((selected or {}).get("name") or "").strip()
    requested_name = project_name.strip()
    if bundled and require_project_name and not requested_name:
        raise DomainError(
            "project_credential_name_required",
            "room_bootstrap requires the configured Project credential name",
            status_code=409,
            details={"required_action": "call_room_bootstrap_with_project_name"},
        )
    if bundled and requested_name and requested_name != profile_name:
        raise DomainError(
            "project_credential_name_mismatch",
            "The selected Project name does not match the token bound to this workspace",
            status_code=403,
        )
    if bundled:
        try:
            credential = get_service().authenticate_agent_token(
                str((selected or {}).get("token") or ""),
                touch=True,
            )
        except DomainError as error:
            if error.code == "agent_token_expired":
                code = "project_credential_expired"
                action = "renew_project_token"
                message = "The selected Project Token has expired"
            elif error.code == "agent_token_revoked":
                code = "project_credential_revoked"
                action = "issue_new_project_token_and_update_bundle"
                message = "The selected Project Token has been revoked"
            else:
                code = "project_credential_invalid"
                action = "replace_project_token_in_bundle"
                message = "The selected Project Token is invalid"
            raise DomainError(
                code,
                message,
                status_code=403,
                details={
                    "project_name": profile_name,
                    "required_action": action,
                },
            ) from error
        if str(credential.get("project_id") or "") != project_id:
            raise DomainError(
                "agent_token_project_forbidden",
                "The selected Project token does not belong to this Project",
                status_code=403,
            )
        selected_permissions = set(credential.get("permissions") or [])
    else:
        selected_permissions = set(access.scopes)
    if permission not in selected_permissions:
        raise DomainError(
            "agent_token_permission_forbidden",
            "Agent token lacks the required permission",
            status_code=403,
            details={"required_permission": permission},
        )
    return access


class AgentCredentialTokenVerifier:
    def __init__(self, room_service: AgentChatRoomService) -> None:
        self.room_service = room_service

    async def verify_token(self, token: str) -> AccessToken | None:
        if is_project_credential_bundle(token):
            try:
                entries = decode_project_credential_bundle(token)
            except CredentialBundleError:
                return None
            project_credentials: dict[str, dict[str, Any]] = {}
            unavailable_project_credentials: dict[str, dict[str, str]] = {}
            identities: set[tuple[str, str, str]] = set()
            scopes: set[str] = set()
            latest_expiry = 0
            for entry in entries:
                try:
                    credential = self.room_service.authenticate_agent_token(
                        entry["token"], touch=False
                    )
                except DomainError as error:
                    # Expired or revoked credentials disable only their Project;
                    # other valid entries in the same client configuration remain usable.
                    if error.code in {"agent_token_expired", "agent_token_revoked"}:
                        unavailable_project_credentials[entry["name"]] = {
                            "reason": error.code,
                        }
                    continue
                project_id = str(credential["project_id"])
                if project_id in project_credentials:
                    return None
                permissions = list(credential["permissions"])
                project_credentials[project_id] = {
                    "name": entry["name"],
                    "token": entry["token"],
                    "credential_id": credential["id"],
                    "permissions": permissions,
                }
                scopes.update(permissions)
                latest_expiry = max(
                    latest_expiry,
                    int(parse_time(credential["expires_at"]).timestamp()),
                )
                identity = _identity_from_member(
                    project_id,
                    str(credential.get("member_id") or ""),
                    room_service=self.room_service,
                )
                if identity is not None:
                    identities.add(identity)
            if (
                not project_credentials
                and not unavailable_project_credentials
            ) or len(identities) > 1:
                return None
            claims: dict[str, Any] = {
                "credential_bundle": True,
                "project_credentials": project_credentials,
                "unavailable_project_credentials": unavailable_project_credentials,
            }
            if identities:
                identity = next(iter(identities))
                claims["software_key"] = identity[0]
                claims["software_name"] = identity[1]
                claims["software_client"] = identity[2]
            return AccessToken(
                token=token,
                client_id="agentchatroom-project-credentials",
                scopes=sorted(scopes),
                expires_at=latest_expiry or None,
                subject="agentchatroom-project-credentials",
                claims=claims,
            )
        try:
            credential = self.room_service.authenticate_agent_token(token, touch=True)
        except DomainError:
            return None
        claims: dict[str, Any] = {
            "credential_id": credential["id"],
            "project_id": credential["project_id"],
        }
        identity = _identity_from_member(
            str(credential.get("project_id") or ""),
            str(credential.get("member_id") or ""),
            room_service=self.room_service,
        )
        if identity is not None:
            claims["software_key"] = identity[0]
            claims["software_name"] = identity[1]
            claims["software_client"] = identity[2]
        return AccessToken(
            token=token,
            client_id=credential["id"],
            scopes=credential["permissions"],
            expires_at=int(parse_time(credential["expires_at"]).timestamp()),
            subject=credential["id"],
            claims=claims,
        )


@mcp.tool()
def room_bootstrap(model: str = "", project_name: str = "") -> dict[str, Any]:
    """Restore the current checkout Room for this conversation.

    Local stdio is zero-parameter. A multi-Project HTTP configuration supplies
    only its non-secret Project credential name; all tokens stay in MCP config.
    """
    set_runtime_binding(None)
    bound = _bound_service_provider.get() is not None
    identity = _bootstrap_http_identity.get() if bound else _configured_local_identity()
    if identity is None:
        payload = bootstrap_status_payload("identity_not_configured")
        details = {"required_action": payload["required_action"]}
        if bound:
            details["http_correct_action"] = (
                "Issue an Agent Token linked to the software member, or add "
                "X-AgentChatRoom-Software-Key/Name/Client headers from the "
                "generated HTTP MCP config. Do not invent a software identity."
            )
            payload = dict(payload)
            payload["details"] = {**(payload.get("details") or {}), **details}
        return {
            "ok": False,
            "result": payload,
            "error": {
                "code": payload["status"],
                "message": "Local MCP software identity is not configured"
                if not bound
                else "HTTP MCP software identity is not configured",
                "details": details,
            },
        }
    software_key, software_name, client = identity
    access = get_access_token() if bound else None
    selected_project_id = (
        _bundle_project_for_name(access, project_name)
        if access is not None and (access.claims or {}).get("credential_bundle")
        else None
    )
    outcome = bootstrap_local_room(
        get_service(),
        software_key=software_key,
        software_name=software_name,
        client=client,
        model=model,
        workspace_roots=_bootstrap_workspace_roots.get(),
        cwd=None if bound else Path.cwd(),
        explicit_project_path=None if bound else os.getenv(PROJECT_PATH_ENV, "").strip() or None,
        loaded_identity=None if bound else _loaded_identity,
        authorize_project=lambda project_id: _authorize_remote(
            project_id,
            "room:join",
            project_name=project_name,
            require_project_name=bound and _current_access_uses_bundle(),
        ),
        database_first=bound,
        selected_project_id=selected_project_id,
    )
    if outcome.binding is None:
        payload = dict(outcome.public)
        details = {
            "required_action": payload.get("required_action"),
        }
        if bound:
            details["http_correct_action"] = HTTP_UNREGISTERED_ROOTS_ACTION
            payload["details"] = {**(payload.get("details") or {}), **details}
        return {
            "ok": False,
            "result": payload,
            "error": {
                "code": payload["status"],
                "message": "Room bootstrap is not ready",
                "details": details,
            },
        }
    set_runtime_binding(outcome.binding)
    _register_local_presence(
        {
            "project": {"id": outcome.binding.project_id},
            "agent": {
                "id": outcome.binding.session_id,
                "agent_key": outcome.binding.agent_key,
            },
            "token": outcome.binding.token,
        }
    )
    return {"ok": True, "result": outcome.public}


@mcp.tool()
def room_join(
    project_path: str,
    model: str,
    role: str = "executor",
    branch: str = "",
    worktree: str = "",
    capabilities: dict[str, Any] | None = None,
    host_key: str = "",
    host_name: str = "",
    git_remote: str = "",
    member_id: str = "",
    agent_key: str = "",
    agent_name: str = "",
    client: str = "",
) -> dict[str, Any]:
    """Join as this MCP process's configured software identity."""
    try:
        room_service = get_service()
        access = get_access_token()
        if access is not None:
            claims = access.claims or {}
            project_id = str(claims.get("project_id", ""))
            if claims.get("credential_bundle"):
                raise DomainError(
                    "room_join_not_supported_for_credential_bundle",
                    "Multi-Project HTTP credentials must use room_bootstrap",
                    status_code=409,
                )
            agent_token = access.token
            _authorize_remote(project_id, "room:join")
            if not host_key.strip() or not host_name.strip():
                raise DomainError(
                    "remote_host_required",
                    "Remote room_join requires host_key and host_name",
                )
            joined = room_service.join_remote_room(
                project_id,
                agent_token=agent_token,
                host_key=host_key,
                host_name=host_name,
                workspace_path=worktree or project_path,
                agent_key=agent_key,
                name=agent_name,
                client=client,
                model=model,
                role=role,
                branch=branch,
                worktree=worktree,
                git_remote=git_remote,
                capabilities=capabilities or {"mcp": True},
            )
            payload = {"project": room_service.get_project(project_id), **joined}
            payload["project_documents"] = room_service.list_project_documents(
                project_id
            )["documents"]
            _register_local_presence(payload)
            return {"ok": True, "result": payload}
        configured_identity = _configured_local_identity()
        if configured_identity is None:
            raise DomainError(
                "software_identity_not_configured",
                "Local MCP must configure a stable software identity",
            )
        effective_software_key, effective_name, effective_client = configured_identity

        resolved_project_key, _ = resolve_checkout_project_key(project_path)
        project = room_service.resolve_project_for_join(
            root_path=project_path,
            registered_project_key=resolved_project_key,
        )
        validate_project_scope(project_path, project)
        register_checkout_project(project_path, project, replace_existing=True)
        workspace_path = str(Path(worktree or project_path).expanduser().resolve())
        local_host_name = host_name.strip() or socket.gethostname() or "local-host"
        local_host_key = host_key.strip() or f"host:{local_host_name}"
        registered = room_service.register_workspace(
            project["id"],
            host_key=local_host_key,
            host_name=local_host_name,
            local_path=workspace_path,
            branch=branch,
            worktree=worktree or workspace_path,
            git_remote=git_remote,
        )
        joined = room_service.join_room(
            project["id"],
            agent_key=agent_key,
            software_key=effective_software_key,
            name=effective_name,
            client=effective_client,
            model=model,
            role=role,
            branch=branch,
            worktree=worktree or workspace_path,
            capabilities=capabilities or {"mcp": True},
            member_id=member_id or None,
            host_id=registered["host"]["id"],
            workspace_id=registered["workspace"]["id"],
        )
        payload = {"project": project, **joined}
        payload["project_documents"] = room_service.list_project_documents(
            project["id"]
        )["documents"]
        _register_local_presence(payload)
        return {"ok": True, "result": payload}
    except DomainError as error:
        return {"ok": False, **error.as_dict()}


@mcp.tool()
def room_sync(
    project_id: str = "",
    session_id: str = "",
    token: str = "",
    after: int = 0,
) -> dict[str, Any]:
    """Return the room snapshot and events newer than the supplied cursor."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    result = _tool_result(
        get_service().room_sync,
        project_id,
        after=after,
        session_id=session_id,
        token=token,
        mcp_context=True,
    )
    if result.get("ok"):
        _ensure_local_presence(project_id, session_id, token)
    return result


@mcp.tool()
def session_heartbeat(
    project_id: str = "",
    session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Refresh connection liveness without advancing the Room cursor."""
    try:
        _authorize_remote(project_id, "room:join")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    result = _tool_result(
        get_service().heartbeat,
        project_id,
        session_id,
        token,
        request_id=request_id.strip() or None,
    )
    if result.get("ok"):
        _ensure_local_presence(project_id, session_id, token)
    return result


@mcp.tool()
def session_leave(
    project_id: str = "",
    session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Close an Agent session explicitly and release its active file leases."""
    try:
        _authorize_remote(project_id, "room:join")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    result = _tool_result(
        get_service().leave_session,
        project_id,
        session_id,
        token,
        request_id=_mcp_request_id(request_id),
    )
    if result.get("ok") and presence_manager is not None:
        presence_manager.unregister(session_id)
    binding = get_runtime_binding()
    if binding is not None and binding.session_id == session_id:
        clear_bindings_for_agent_session(session_id)
    return result


@mcp.tool()
def message_post(
    project_id: str = "",
    session_id: str = "",
    token: str = "",
    body: str = "",
    model_display_name: str = "",
    kind: str = "message",
    task_id: str = "",
    mentions: list[str] | None = None,
    files: list[str] | None = None,
    requires_ack: bool = False,
    priority: int = 2,
    channel: str = "public",
    request_id: str = "",
) -> dict[str, Any]:
    """Post a message with the client-visible model used for this response."""
    try:
        _authorize_remote(project_id, "message:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    result = _tool_result(
        get_service().post_message,
        project_id,
        body=body,
        kind=kind,
        session_id=session_id,
        token=token,
        model_display_name=model_display_name,
        task_id=task_id or None,
        mentions=mentions,
        files=files,
        requires_ack=requires_ack,
        priority=priority,
        channel=channel,
        request_id=_mcp_request_id(request_id),
    )
    if result.get("ok"):
        _ensure_local_presence(project_id, session_id, token)
    return result


@mcp.tool()
def message_acknowledge(
    project_id: str = "",
    event_id: int = 0,
    session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Acknowledge a room message that explicitly requires confirmation."""
    try:
        _authorize_remote(project_id, "message:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().acknowledge_event,
        project_id,
        event_id,
        session_id,
        token,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_list(
    project_id: str = "",
    status: str = "",
    phase: str = "",
) -> dict[str, Any]:
    """List project tasks, optionally filtered by legacy status or view phase.

    phase accepts a versioned phase code from the shared task view projection
    (e.g. pending_integration, changes_requested) or "attention" for the
    deduplicated needs-attention inbox.
    """
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().list_tasks,
        project_id,
        status=status or None,
        phase=phase or None,
    )


@mcp.tool()
def project_document_list(project_id: str = "") -> dict[str, Any]:
    """List versioned project documents (manifest only; use project_document_get for content)."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(get_service().list_project_documents, project_id)


@mcp.tool()
def project_document_get(
    project_id: str = "",
    doc_key: str = "",
    version: int = 0,
) -> dict[str, Any]:
    """Fetch full content of one versioned project document (default: current version)."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    if not doc_key.strip():
        return {"ok": False, "error": {"code": "invalid_project_document", "message": "doc_key is required"}}
    return _tool_result(
        get_service().get_project_document,
        project_id,
        doc_key,
        version=version or None,
    )


@mcp.tool()
def project_document_upsert(
    project_id: str = "",
    doc_key: str = "",
    kind: str = "binding",
    title: str = "",
    content: str = "",
) -> dict[str, Any]:
    """Create a new immutable version of a project document (binding or reference)."""
    try:
        _authorize_remote(project_id, "document:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().upsert_project_document,
        project_id,
        doc_key=doc_key,
        kind=kind,
        title=title,
        content=content,
        actor="agent",
    )


@mcp.tool()
def task_get(project_id: str = "", task_id: str = "") -> dict[str, Any]:
    """Return one complete task by ID without expanding the whole project board."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    def _get_with_documents(pid: str, tid: str) -> dict[str, Any]:
        service = get_service()
        task = service.get_task(pid, tid)
        # Additive injection: keeps the historical task-object contract intact.
        task["project_documents"] = service.injectable_project_documents(pid)
        return task

    return _tool_result(_get_with_documents, project_id, task_id)


@mcp.tool()
def task_history(
    project_id: str = "",
    task_id: str = "",
    after: int = 0,
    before: int = 0,
    cursor: int = 0,
    limit: int = 50,
    event_type: str = "",
) -> dict[str, Any]:
    """Return a paginated, redacted evidence chain for one task."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().list_task_history,
        project_id,
        task_id,
        after=after,
        before=before,
        cursor=cursor,
        limit=limit,
        event_type=event_type,
    )


@mcp.tool()
def task_get_by_number(project_id: str = "", task_number: int = 0) -> dict[str, Any]:
    """Return one complete task by its stable human-readable Project number."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(get_service().get_task_by_number, project_id, task_number)


@mcp.tool()
def task_intake_targets(project_id: str = "") -> dict[str, Any]:
    """List all non-revoked Agent identities that can receive a user task intake.

    The result includes previously connected but currently offline identities;
    their connection status is exposed so clients can explain deferred intake.
    """
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(get_service().list_task_intake_targets, project_id)


@mcp.tool()
def task_intake_submit(
    project_id: str = "",
    raw_description: str = "",
    target_member_id: str = "",
    target_session_id: str = "",
    created_by_session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Submit raw user intent for a selected Agent to accept and define."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().submit_task_intake,
        project_id,
        raw_description=raw_description,
        target_member_id=target_member_id,
        target_session_id=target_session_id or None,
        created_by_session_id=created_by_session_id or None,
        token=token or None,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_intake_list(project_id: str = "", status: str = "") -> dict[str, Any]:
    """List user task intakes, optionally filtered by lifecycle status."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().list_task_intakes,
        project_id,
        status=status or None,
    )


@mcp.tool()
def task_intake_get(project_id: str = "", intake_id: str = "") -> dict[str, Any]:
    """Return one user task intake and its current formal-task link."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(get_service().get_task_intake, project_id, intake_id)


@mcp.tool()
def task_intake_acknowledge(
    project_id: str = "",
    intake_id: str = "",
    session_id: str = "",
    token: str = "",
    response: str = "accepted",
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Accept, decline, or block a user task intake as its selected Agent."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().acknowledge_task_intake,
        project_id,
        intake_id,
        session_id=session_id,
        token=token,
        response=response,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_intake_reassign(
    project_id: str = "",
    intake_id: str = "",
    target_member_id: str = "",
    target_session_id: str = "",
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Redirect an undefined user task intake to another available Agent."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().reassign_task_intake,
        project_id,
        intake_id,
        target_member_id=target_member_id,
        target_session_id=target_session_id or None,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_define_from_intake(
    project_id: str = "",
    intake_id: str = "",
    session_id: str = "",
    token: str = "",
    title: str = "",
    acceptance_criteria: list[str] | None = None,
    description: str = "",
    depends_on: list[str] | None = None,
    priority: int | None = None,
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Define and dispatch the formal task contract after intake acceptance."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().define_task_from_intake,
        project_id,
        intake_id,
        session_id=session_id,
        token=token,
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        depends_on=depends_on,
        priority=priority,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def audit_query(
    project_id: str = "",
    after: int = 0,
    before: int = 0,
    limit: int = 200,
    event_type: str = "",
    actor_session_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Query append-only Project audit events with optional stable filters."""
    try:
        _authorize_remote(project_id, "audit:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().query_audit,
        project_id,
        after=after,
        before=before,
        limit=limit,
        event_type=event_type or None,
        actor_session_id=actor_session_id or None,
        task_id=task_id or None,
    )


@mcp.tool()
def member_list(project_id: str = "", include_revoked: bool = True) -> dict[str, Any]:
    """List versioned Project members and their token/session counts."""
    try:
        _authorize_remote(project_id, "member:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().list_project_members,
        project_id,
        include_revoked=include_revoked,
    )


@mcp.tool()
def member_create(
    project_id: str = "",
    member_key: str = "",
    name: str = "",
    kind: str = "agent",
    role: str = "",
    status: str = "active",
    metadata: dict[str, Any] | None = None,
    actor_session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Create a Project member through the shared domain service."""
    try:
        _authorize_remote(project_id, "member:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().create_project_member,
        project_id,
        member_key=member_key,
        name=name,
        kind=kind,
        role=role,
        status=status,
        metadata=metadata,
        actor_session_id=actor_session_id or None,
        token=token or None,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def member_update(
    project_id: str = "",
    member_id: str = "",
    name: str = "",
    kind: str = "",
    role: str = "",
    status: str = "",
    metadata: dict[str, Any] | None = None,
    actor_session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Update a Project member without rewriting prior audit events."""
    try:
        _authorize_remote(project_id, "member:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().update_project_member,
        project_id,
        member_id,
        name=name or None,
        kind=kind or None,
        role=role or None,
        status=status or None,
        metadata=metadata,
        actor_session_id=actor_session_id or None,
        token=token or None,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def member_revoke(
    project_id: str = "",
    member_id: str = "",
    actor_session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Revoke a Project member while preserving its historical record."""
    try:
        _authorize_remote(project_id, "member:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().revoke_project_member,
        project_id,
        member_id,
        actor_session_id=actor_session_id or None,
        token=token or None,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_create(
    project_id: str = "",
    title: str = "",
    acceptance_criteria: list[str] | None = None,
    description: str = "",
    depends_on: list[str] | None = None,
    priority: int | None = None,
    actor_session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Create a task with explicit acceptance criteria and optional dependencies."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().create_task,
        project_id,
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        depends_on=depends_on,
        priority=priority,
        actor_session_id=actor_session_id or None,
        token=token or None,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_claim(
    project_id: str = "",
    task_id: str = "",
    session_id: str = "",
    token: str = "",
    reclaim: bool = False,
    request_id: str = "",
) -> dict[str, Any]:
    """Claim an available task, or explicitly reclaim same-identity work after disconnect."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().claim_task,
        project_id,
        task_id,
        session_id,
        token,
        reclaim=reclaim,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_release(
    project_id: str = "",
    task_id: str = "",
    reason_code: str = "",
    reason: str = "",
    session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Release an owned task back to the claimable pool without cancelling it.

    reason_code must be one of: quota_exhausted, agent_unavailable,
    user_requested, reassignment_needed, other. The task returns to todo with
    its contract, progress, and history intact; active file leases are released
    and pending assignments/handoffs are cancelled.
    """
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().release_task,
        project_id,
        task_id,
        reason_code=reason_code,
        reason=reason,
        session_id=session_id or None,
        token=token or None,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_assign(
    project_id: str = "",
    task_id: str = "",
    assigned_by_session_id: str = "",
    token: str = "",
    assigned_to_session_id: str = "",
    assigned_to_member_id: str = "",
    target_role: str = "",
    required_capability: str = "",
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Assign a task to a specific Agent (online session or persistent offline identity) or to a role/capability target."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().assign_task,
        project_id,
        task_id,
        assigned_by_session_id=assigned_by_session_id,
        token=token,
        assigned_to_session_id=assigned_to_session_id or None,
        assigned_to_member_id=assigned_to_member_id or None,
        target_role=target_role,
        required_capability=required_capability,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_acknowledge(
    project_id: str = "",
    task_id: str = "",
    assignment_id: str = "",
    session_id: str = "",
    token: str = "",
    response: str = "",
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Accept, decline, or report a block on a pending task assignment."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().acknowledge_task_assignment,
        project_id,
        task_id,
        assignment_id,
        session_id=session_id,
        token=token,
        response=response,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_handoff(
    project_id: str = "",
    task_id: str = "",
    from_session_id: str = "",
    token: str = "",
    to_session_id: str = "",
    summary: str = "",
    next_step: str = "",
    completed_items: list[str] | None = None,
    pending_items: list[str] | None = None,
    files: list[str] | None = None,
    risks: list[str] | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Request an acknowledged ownership handoff with structured continuation context."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().handoff_task,
        project_id,
        task_id,
        from_session_id=from_session_id,
        token=token,
        to_session_id=to_session_id,
        summary=summary,
        completed_items=completed_items,
        pending_items=pending_items,
        files=files,
        risks=risks,
        next_step=next_step,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_handoff_acknowledge(
    project_id: str = "",
    task_id: str = "",
    handoff_id: str = "",
    session_id: str = "",
    token: str = "",
    response: str = "",
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Accept, decline, or report a block on a pending task handoff."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().acknowledge_task_handoff,
        project_id,
        task_id,
        handoff_id,
        session_id=session_id,
        token=token,
        response=response,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_update(
    project_id: str = "",
    task_id: str = "",
    session_id: str = "",
    token: str = "",
    status: str = "",
    title: str = "",
    description: str = "",
    acceptance_criteria: list[str] | None = None,
    depends_on: list[str] | None = None,
    priority: int | None = None,
    progress_percent: int | None = None,
    current_step: str = "",
    blocker_reason: str = "",
    next_step: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Update owned task status or details within the server-side state machine."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().update_task,
        project_id,
        task_id,
        status=status or None,
        title=title or None,
        description=description or None,
        acceptance_criteria=acceptance_criteria,
        depends_on=depends_on,
        priority=priority,
        progress_percent=progress_percent,
        current_step=current_step or None,
        blocker_reason=blocker_reason or None,
        next_step=next_step or None,
        session_id=session_id,
        token=token,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def lease_acquire(
    project_id: str = "",
    session_id: str = "",
    token: str = "",
    path_pattern: str = "",
    mode: str = "exclusive",
    task_id: str = "",
    reason: str = "",
    ttl_seconds: int | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Acquire a time-limited file or glob lease and report overlap conflicts."""
    try:
        _authorize_remote(project_id, "lease:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().acquire_lease,
        project_id,
        session_id=session_id,
        token=token,
        path_pattern=path_pattern,
        mode=mode,
        task_id=task_id or None,
        reason=reason,
        ttl_seconds=ttl_seconds,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def lease_release(
    project_id: str = "",
    lease_id: str = "",
    session_id: str = "",
    token: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Release a file lease owned by this agent session."""
    try:
        _authorize_remote(project_id, "lease:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().release_lease,
        project_id,
        lease_id,
        session_id,
        token,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def work_report(
    project_id: str = "",
    task_id: str = "",
    session_id: str = "",
    token: str = "",
    summary: str = "",
    files: list[str] | None = None,
    tests: list[TestEvidence] | None = None,
    commit_hash: str = "",
    no_code_change_reason: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Submit test evidence plus changed files or an explicit no-code reason."""
    try:
        _authorize_remote(project_id, "task:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().submit_work_report,
        project_id,
        task_id,
        session_id=session_id,
        token=token,
        summary=summary,
        files=files or [],
        tests=tests or [],
        commit_hash=commit_hash,
        no_code_change_reason=no_code_change_reason,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def review_submit(
    project_id: str = "",
    task_id: str = "",
    reviewer_session_id: str = "",
    token: str = "",
    verdict: str = "",
    criteria: list[ReviewCriterion] | None = None,
    notes: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Submit an independent acceptance review for work awaiting verification."""
    try:
        _authorize_remote(project_id, "review:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().submit_review,
        project_id,
        task_id,
        reviewer_session_id=reviewer_session_id,
        token=token,
        verdict=verdict,
        criteria=criteria or [],
        notes=notes,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def integration_submit(
    project_id: str = "",
    task_id: str = "",
    integrator_session_id: str = "",
    token: str = "",
    result: str = "",
    summary: str = "",
    tests: list[TestEvidence] | None = None,
    files: list[str] | None = None,
    commit_hash: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Record final integration evidence after independent verification."""
    try:
        _authorize_remote(project_id, "integration:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().submit_integration,
        project_id,
        task_id,
        integrator_session_id=integrator_session_id,
        token=token,
        result=result,
        summary=summary,
        files=files or [],
        tests=tests or [],
        commit_hash=commit_hash,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_candidate_submit(
    project_id: str = "",
    session_id: str = "",
    token: str = "",
    title: str = "",
    body: str = "",
    kind: str = "",
    summary: str = "",
    tags: list[str] | None = None,
    source_type: str = "manual",
    source_task_id: str = "",
    source_report_id: str = "",
    source_review_id: str = "",
    source_integration_id: str = "",
    source_event_ids: list[int] | None = None,
    asset_id: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Submit a Knowledge Asset candidate version with traceable provenance."""
    try:
        _authorize_remote(project_id, "knowledge:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().submit_knowledge_candidate,
        project_id,
        session_id=session_id,
        token=token,
        title=title,
        body=body,
        kind=kind,
        summary=summary,
        tags=tags,
        source_type=source_type,
        source_task_id=source_task_id,
        source_report_id=source_report_id,
        source_review_id=source_review_id,
        source_integration_id=source_integration_id,
        source_event_ids=source_event_ids,
        asset_id=asset_id,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_review(
    project_id: str = "",
    asset_id: str = "",
    reviewer_session_id: str = "",
    token: str = "",
    verdict: str = "",
    criteria: list[ReviewCriterion] | None = None,
    notes: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Review a candidate Knowledge Asset version with an independent verdict."""
    try:
        _authorize_remote(project_id, "review:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().submit_knowledge_review,
        project_id,
        asset_id,
        reviewer_session_id=reviewer_session_id,
        token=token,
        verdict=verdict,
        criteria=criteria or [],
        notes=notes,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_supersede(
    project_id: str = "",
    asset_id: str = "",
    session_id: str = "",
    token: str = "",
    reason: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Mark an approved Knowledge Asset as superseded by newer knowledge."""
    try:
        _authorize_remote(project_id, "knowledge:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().supersede_knowledge_asset,
        project_id,
        asset_id,
        session_id=session_id,
        token=token,
        reason=reason,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_archive(
    project_id: str = "",
    asset_id: str = "",
    session_id: str = "",
    token: str = "",
    reason: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Archive a Knowledge Asset so it stops receiving new versions."""
    try:
        _authorize_remote(project_id, "knowledge:write")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().archive_knowledge_asset,
        project_id,
        asset_id,
        session_id=session_id,
        token=token,
        reason=reason,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_get(
    project_id: str = "",
    asset_id: str = "",
    version_id: str = "",
) -> dict[str, Any]:
    """Read one Knowledge Asset with its full version and review history."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().get_knowledge_asset,
        project_id,
        asset_id,
        version_id=version_id,
    )


@mcp.tool()
def knowledge_list(
    project_id: str = "",
    status: str = "",
    kind: str = "",
    source_task_id: str = "",
) -> dict[str, Any]:
    """List Knowledge Assets filtered by status, kind, or source task."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(
        get_service().list_knowledge_assets,
        project_id,
        status=status or None,
        kind=kind or None,
        source_task_id=source_task_id or None,
    )


def create_mcp(
    room_service: AgentChatRoomService,
    *,
    presence: LocalPresenceManager | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    stateless_http: bool = False,
    json_response: bool = True,
    auth_required: bool = False,
    issuer_url: str = "http://127.0.0.1",
    resource_server_url: str = "http://127.0.0.1/mcp",
) -> FastMCP:
    """Create an MCP adapter bound to an existing domain service instance."""
    auth = (
        AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            required_scopes=[],
        )
        if auth_required
        else None
    )
    verifier = AgentCredentialTokenVerifier(room_service) if auth_required else None
    server = _new_mcp(
        lambda: room_service,
        presence=presence,
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
        stateless_http=stateless_http,
        json_response=json_response,
        token_verifier=verifier,
        auth=auth,
    )
    for tool in mcp._tool_manager.list_tools():
        server.add_tool(
            tool.fn,
            name=tool.name,
            title=tool.title,
            description=tool.description,
            annotations=tool.annotations,
            icons=tool.icons,
            meta=tool.meta,
        )
    return server


MCP_STARTUP_UNAVAILABLE_EXIT_CODE = 2


def main(argv: list[str] | None = None) -> None:
    global presence_manager, _stdio_settings, _stdio_generation
    parser = argparse.ArgumentParser(
        prog="agentchatroom-mcp",
        description="Run the AgentChatRoom MCP server over stdio.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args(argv)
    global _loaded_identity
    # Startup must fail bounded: when the local engine cannot start (unusable
    # data directory, broken database, identity misconfiguration), print one
    # diagnosable stderr line and exit without spawning any process, retrying,
    # or leaking a traceback, tokens, or user paths beyond the failing input.
    try:
        settings = load_settings()
        _stdio_generation = require_running_service(settings)
        _stdio_settings = settings
        room_service = get_service()
        _loaded_identity = _configured_local_identity()
        presence_manager = LocalPresenceManager(
            room_service,
            enabled=settings.presence_keepalive_enabled,
            interval_seconds=settings.presence_keepalive_interval_seconds,
            availability_check=lambda: require_running_service(settings),
        )
        presence_manager.start()
    except Exception as error:  # noqa: BLE001 - bounded startup failure boundary
        code = getattr(error, "code", type(error).__name__)
        sys.stderr.write(
            f"agentchatroom mcp unavailable ({code})\n"
            "recovery: start the configured service explicitly; prefer HTTP MCP to avoid client-spawned processes\n"
        )
        raise SystemExit(MCP_STARTUP_UNAVAILABLE_EXIT_CODE) from None
    # Wait for client roots: startup cwd/pins cannot safely establish presence.
    try:
        from .stdio_runtime import install_protocol_stdin_guard, verify_protocol_stdin

        install_protocol_stdin_guard()
        mcp.run(transport="stdio")
        verify_protocol_stdin()
    finally:
        presence_manager.stop()
        presence_manager = None
        _stdio_settings = None
        _stdio_generation = None


if __name__ == "__main__":
    main(sys.argv[1:])
