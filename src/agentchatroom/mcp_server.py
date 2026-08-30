from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from contextvars import ContextVar
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
        binding = None
        if self.service_provider is not None:
            binding = _bound_service_provider.set(self.service_provider)
        try:
            return await super().call_tool(
                name,
                arguments,
                context=context,
                convert_result=convert_result,
            )
        finally:
            if binding is not None:
                _bound_service_provider.reset(binding)


MCP_INSTRUCTIONS = (
    "Join the current project before doing work. Never supply, infer, or replace "
    "a project_key: the backend owns it and local room_join reads the server-managed "
    ".agentchatroom/project.json registration. Agents also do not supply logical_path; "
    "the backend derives it from project_path relative to the detected repository root. "
    "The MCP process owns one configured software identity. Agents must not invent "
    "or rename agent_key, agent_name, or client values for different tasks or roles. "
    "A fully configured local stdio process automatically establishes Presence for "
    "an existing registered checkout, but Agents still call room_join and room_sync "
    "before work to obtain runtime credentials and synchronize current facts. "
    "room_join may create a Room only when "
    "the repository scope is genuinely empty. Sync before claiming a task, "
    "before editing files, and before reporting completion. Acquire file leases "
    "before edits and include evidence in work reports. Every Agent-authored "
    "message must include model_display_name exactly as shown in the client UI."
)

SOFTWARE_KEY_ENV = "AGENTCHATROOM_SOFTWARE_KEY"
SOFTWARE_NAME_ENV = "AGENTCHATROOM_SOFTWARE_NAME"
SOFTWARE_CLIENT_ENV = "AGENTCHATROOM_SOFTWARE_CLIENT"
PROJECT_PATH_ENV = "AGENTCHATROOM_PROJECT_PATH"


def _configured_local_identity() -> tuple[str, str, str] | None:
    software_key = os.getenv(SOFTWARE_KEY_ENV, "").strip()
    software_name = os.getenv(SOFTWARE_NAME_ENV, "").strip()
    client = os.getenv(SOFTWARE_CLIENT_ENV, "").strip()
    values = (software_key, software_name, client)
    if not all(values) or any(value.startswith("<") for value in values):
        return None
    return software_key, software_name, client


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
        joined = room_service.join_room(
            project["id"],
            agent_key=agent_key,
            software_key=effective_software_key,
            name=effective_name,
            client=effective_client,
            model=model,
            role=role,
            branch=branch,
            worktree=worktree or project_path,
            capabilities=capabilities or {"mcp": True},
            member_id=member_id or None,
        )
        payload = {"project": project, **joined}
        _register_local_presence(payload)
        return {"ok": True, "result": payload}
    except DomainError as error:
        return {"ok": False, **error.as_dict()}


@mcp.tool()
def room_sync(
    project_id: str,
    session_id: str,
    token: str,
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
    project_id: str,
    session_id: str,
    token: str,
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
    project_id: str,
    session_id: str,
    token: str,
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
    return result


@mcp.tool()
def message_post(
    project_id: str,
    session_id: str,
    token: str,
    body: str,
    model_display_name: str,
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
    project_id: str,
    event_id: int,
    session_id: str,
    token: str,
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
def task_list(project_id: str, status: str = "") -> dict[str, Any]:
    """List project tasks, optionally filtered by status."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(get_service().list_tasks, project_id, status=status or None)


@mcp.tool()
def task_get(project_id: str, task_id: str) -> dict[str, Any]:
    """Return one complete task by ID without expanding the whole project board."""
    try:
        _authorize_remote(project_id, "room:read")
    except DomainError as error:
        return {"ok": False, **error.as_dict()}
    return _tool_result(get_service().get_task, project_id, task_id)


@mcp.tool()
def audit_query(
    project_id: str,
    after: int = 0,
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
        limit=limit,
        event_type=event_type or None,
        actor_session_id=actor_session_id or None,
        task_id=task_id or None,
    )


@mcp.tool()
def member_list(project_id: str, include_revoked: bool = True) -> dict[str, Any]:
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
    project_id: str,
    member_key: str,
    name: str,
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
    project_id: str,
    member_id: str,
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
    project_id: str,
    member_id: str,
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
    project_id: str,
    title: str,
    acceptance_criteria: list[str],
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
    project_id: str,
    task_id: str,
    session_id: str,
    token: str,
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
def task_assign(
    project_id: str,
    task_id: str,
    assigned_by_session_id: str,
    token: str,
    assigned_to_session_id: str = "",
    target_role: str = "",
    required_capability: str = "",
    note: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Assign a task to a specific Agent or to a role/capability target."""
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
        target_role=target_role,
        required_capability=required_capability,
        note=note,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def task_acknowledge(
    project_id: str,
    task_id: str,
    assignment_id: str,
    session_id: str,
    token: str,
    response: str,
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
    project_id: str,
    task_id: str,
    from_session_id: str,
    token: str,
    to_session_id: str,
    summary: str,
    next_step: str,
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
    project_id: str,
    task_id: str,
    handoff_id: str,
    session_id: str,
    token: str,
    response: str,
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
    project_id: str,
    task_id: str,
    session_id: str,
    token: str,
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
    project_id: str,
    session_id: str,
    token: str,
    path_pattern: str,
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
    project_id: str,
    lease_id: str,
    session_id: str,
    token: str,
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
    project_id: str,
    task_id: str,
    session_id: str,
    token: str,
    summary: str,
    files: list[str],
    tests: list[TestEvidence],
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
        files=files,
        tests=tests,
        commit_hash=commit_hash,
        no_code_change_reason=no_code_change_reason,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def review_submit(
    project_id: str,
    task_id: str,
    reviewer_session_id: str,
    token: str,
    verdict: str,
    criteria: list[ReviewCriterion],
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
        criteria=criteria,
        notes=notes,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def integration_submit(
    project_id: str,
    task_id: str,
    integrator_session_id: str,
    token: str,
    result: str,
    summary: str,
    tests: list[TestEvidence],
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
        files=files,
        tests=tests,
        commit_hash=commit_hash,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_candidate_submit(
    project_id: str,
    session_id: str,
    token: str,
    title: str,
    body: str,
    kind: str,
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
    project_id: str,
    asset_id: str,
    reviewer_session_id: str,
    token: str,
    verdict: str,
    criteria: list[ReviewCriterion],
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
        criteria=criteria,
        notes=notes,
        request_id=_mcp_request_id(request_id),
    )


@mcp.tool()
def knowledge_supersede(
    project_id: str,
    asset_id: str,
    session_id: str,
    token: str,
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
    project_id: str,
    asset_id: str,
    session_id: str,
    token: str,
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
    project_id: str,
    asset_id: str,
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
    project_id: str,
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
    settings = load_settings()
    room_service = get_service()
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
