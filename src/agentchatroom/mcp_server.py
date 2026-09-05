from __future__ import annotations

import argparse
import hashlib
import inspect
import logging
import os
import socket
import sys
import threading
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
from .database import create_database
from .errors import DomainError
from .mcp_compat import CompatibleToolManager
from .presence import LocalPresenceManager
from .bootstrap import (
    RuntimeBinding,
    bind_runtime_arguments,
    bootstrap_local_room,
    bootstrap_status_payload,
    configured_software_identity,
    workspace_path_from_file_uri,
    PROJECT_PATH_ENV,
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
_runtime_binding: ContextVar[RuntimeBinding | None] = ContextVar(
    "agentchatroom_runtime_binding",
    default=None,
)
_session_bindings: dict[str, RuntimeBinding] = {}
_session_bindings_lock = threading.RLock()
_loaded_identity: tuple[str, str, str] | None = None
_bootstrap_workspace_roots: ContextVar[list[Path] | None] = ContextVar(
    "agentchatroom_bootstrap_workspace_roots",
    default=None,
)


class ServiceBoundToolManager(CompatibleToolManager):
    def __init__(self, service_provider: ServiceProvider | None = None) -> None:
        super().__init__()
        self.service_provider = service_provider

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        provider_token = None
        roots_token = None
        session_key = mcp_session_key(context)
        previous_binding = _runtime_binding.get()
        if self.service_provider is not None:
            provider_token = _bound_service_provider.set(self.service_provider)
        try:
            forwarded = dict(arguments or {})
            restored = get_runtime_binding(session_key)
            if restored is not None:
                _runtime_binding.set(restored)
            if name == "room_bootstrap":
                roots_token = _bootstrap_workspace_roots.set(
                    await collect_mcp_workspace_roots(context)
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
            if roots_token is not None:
                _bootstrap_workspace_roots.reset(roots_token)
            if name == "session_leave":
                _runtime_binding.set(None)
            else:
                _runtime_binding.set(previous_binding)
            if provider_token is not None:
                _bound_service_provider.reset(provider_token)


MCP_INSTRUCTIONS = (
    "Call room_bootstrap once at the start of a new conversation before project "
    "work. Do not read or edit mcp.json, config.toml, AgentChatRoom source, or "
    "the database on the normal path. Never supply, infer, or replace a "
    "project_key, logical_path, agent_key, member_id, software name, or client "
    "type. Agents also do not supply logical_path. Local stdio resolves ignored "
    ".agentchatroom/project.json; the MCP process owns one configured software "
    "identity and holds the Session Token in memory. Do not copy project_id, "
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


def mcp_session_key(context: Any = None) -> str:
    request_context = getattr(context, "request_context", None) if context is not None else None
    if request_context is None and context is not None:
        request_context = getattr(context, "_request_context", None)
    session = getattr(request_context, "session", None) if request_context is not None else None
    if session is None and context is not None:
        session = getattr(context, "session", None)
    for attr in ("session_id", "_session_id", "id"):
        value = getattr(session, attr, None) if session is not None else None
        if value:
            return f"mcp:{value}"
    try:
        access = get_access_token()
    except LookupError:
        access = None
    token = getattr(access, "token", "") if access is not None else ""
    if token:
        digest = hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]
        return f"auth:{digest}"
    return "stdio:local"


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
    persist_runtime_binding(session_key or "stdio:local", binding)


def clear_runtime_binding(session_key: str | None = None) -> None:
    _runtime_binding.set(None)
    if session_key is None:
        with _session_bindings_lock:
            _session_bindings.clear()
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


async def collect_mcp_workspace_roots(context: Any) -> list[Path]:
    if context is None:
        return []
    request_context = getattr(context, "request_context", None)
    if request_context is None:
        request_context = getattr(context, "_request_context", None)
    session = getattr(request_context, "session", None) if request_context is not None else None
    if session is None:
        session = getattr(context, "session", None)
    list_roots = getattr(session, "list_roots", None) if session is not None else None
    if list_roots is None:
        return []
    try:
        result = await list_roots()
    except Exception:
        logger.debug("MCP client did not provide workspace roots")
        return []
    roots: list[Path] = []
    for root in getattr(result, "roots", None) or []:
        uri = str(getattr(root, "uri", "") or "")
        path = workspace_path_from_file_uri(uri)
        if path is not None:
            roots.append(path)
    return roots


def _auto_join_local_checkout() -> dict[str, Any] | None:
    """Create startup Presence only for a fully configured registered checkout."""
    project_path = os.getenv(PROJECT_PATH_ENV, "").strip()
    if not project_path or _configured_local_identity() is None:
        return None
    try:
        registered_project_key, _ = resolve_checkout_project_key(project_path)
    except DomainError as error:
        logger.warning("Local MCP auto-join skipped: %s", error.code)
        return None
    if not registered_project_key:
        return None
    response = room_join(project_path=project_path, model="unknown")
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
    server._tool_manager = ServiceBoundToolManager(service_provider)
    return server

mcp = _new_mcp()


def get_service() -> AgentChatRoomService:
    global service
    provider = _bound_service_provider.get()
    if provider is not None:
        return provider()
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
    if presence_manager is None:
        return
    project = payload.get("project") or {}
    agent = payload.get("agent") or {}
    token = str(payload.get("token", ""))
    if project.get("id") and agent.get("id") and token:
        presence_manager.register(
            str(project["id"]),
            str(agent["id"]),
            token,
            agent_key=str(agent.get("agent_key") or agent["id"]),
        )


def _ensure_local_presence(project_id: str, session_id: str, token: str) -> None:
    """Re-register a live session with the local presence manager.

    The presence registry is process-local memory and empties on MCP process
    restart, which strands previously-registered sessions: heartbeats stop,
    and the UI shows a busy agent as disconnected. A successful authenticated
    tool call proves this process still owns the session, so use it to resume
    the background heartbeat keepalive automatically.
    """
    if presence_manager is None:
        return
    presence_manager.ensure_registered(project_id, session_id, token)


def _authorize_remote(project_id: str, permission: str) -> AccessToken | None:
    access = get_access_token()
    if access is None:
        return None
    claims = access.claims or {}
    if claims.get("project_id") != project_id:
        raise DomainError(
            "agent_token_project_forbidden",
            "Agent token is not authorized for this Project",
            status_code=403,
        )
    if permission not in access.scopes:
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
        try:
            credential = self.room_service.authenticate_agent_token(token, touch=True)
        except DomainError:
            return None
        return AccessToken(
            token=token,
            client_id=credential["id"],
            scopes=credential["permissions"],
            expires_at=int(parse_time(credential["expires_at"]).timestamp()),
            subject=credential["id"],
            claims={
                "credential_id": credential["id"],
                "project_id": credential["project_id"],
            },
        )


@mcp.tool()
def room_bootstrap(model: str = "") -> dict[str, Any]:
    """Restore the current checkout Room for this conversation.

    Zero-parameter on the normal path. The MCP process supplies software
    identity and checkout registration; the Session Token stays in process memory.
    """
    identity = _configured_local_identity()
    if identity is None:
        payload = bootstrap_status_payload("identity_not_configured")
        return {
            "ok": False,
            "result": payload,
            "error": {
                "code": payload["status"],
                "message": "Local MCP software identity is not configured",
                "details": {"required_action": payload["required_action"]},
            },
        }
    software_key, software_name, client = identity
    outcome = bootstrap_local_room(
        get_service(),
        software_key=software_key,
        software_name=software_name,
        client=client,
        model=model,
        workspace_roots=_bootstrap_workspace_roots.get(),
        cwd=Path.cwd(),
        explicit_project_path=os.getenv(PROJECT_PATH_ENV, "").strip() or None,
        loaded_identity=_loaded_identity,
    )
    if outcome.binding is None:
        payload = outcome.public
        return {
            "ok": False,
            "result": payload,
            "error": {
                "code": payload["status"],
                "message": "Room bootstrap is not ready",
                "details": {
                    "required_action": payload.get("required_action"),
                },
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
            _authorize_remote(project_id, "room:join")
            if not host_key.strip() or not host_name.strip():
                raise DomainError(
                    "remote_host_required",
                    "Remote room_join requires host_key and host_name",
                )
            joined = room_service.join_remote_room(
                project_id,
                agent_token=access.token,
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
    priority: int = 2,
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
    priority: int = 2,
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
    request_id: str = "",
) -> dict[str, Any]:
    """Atomically claim an available task for this agent session."""
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


def main(argv: list[str] | None = None) -> None:
    global presence_manager
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
    settings = load_settings()
    room_service = get_service()
    _loaded_identity = _configured_local_identity()
    presence_manager = LocalPresenceManager(
        room_service,
        enabled=settings.presence_keepalive_enabled,
        interval_seconds=settings.presence_keepalive_interval_seconds,
    )
    presence_manager.start()
    _auto_join_local_checkout()
    try:
        mcp.run(transport="stdio")
    finally:
        presence_manager.stop()
        presence_manager = None


if __name__ == "__main__":
    main(sys.argv[1:])
