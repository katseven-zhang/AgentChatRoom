from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders

from . import __version__
from .config import Settings, load_settings
from .database import create_database
from .errors import DomainError
from .integrations import build_mcp_integration
from .mcp_server import create_mcp
from .contracts import (
    ASSIGNMENT_RESPONSES,
    ASSIGNMENT_STATUSES,
    DOMAIN_SCHEMA_VERSION,
    HANDOFF_RESPONSES,
    HANDOFF_STATUSES,
    INTEGRATION_RESULTS,
    MODEL_DISPLAY_NAME_MAX_LENGTH,
    PROJECT_MEMBER_SCHEMA_VERSION,
    PROJECT_MEMBER_STATUSES,
    TASK_EXECUTION_STATUSES,
    TASK_INTEGRATION_STATUSES,
    TASK_VERIFICATION_STATUSES,
)
from .services import (
    AGENT_STATUSES,
    AGENT_PERMISSIONS,
    LEASE_MODES,
    MESSAGE_CHANNELS,
    MESSAGE_KINDS,
    PROJECT_SETTINGS_DEFAULTS,
    TASK_STATUSES,
    TASK_TRANSITIONS,
    AgentChatRoomService,
    REQUEST_ID_PATTERN,
    new_id,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestIdASGIMiddleware:
    """Attach request IDs without BaseHTTPMiddleware stream buffering."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        supplied = (
            request.headers.get("idempotency-key")
            or request.headers.get("x-request-id")
            or ""
        ).strip()
        request_id = supplied or new_id("request")
        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            await JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_request_id",
                        "message": "Request ID must be 1-128 safe ASCII characters",
                        "details": {},
                    }
                },
            )(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["idempotency_request_id"] = supplied or None

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                message = dict(message)
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                headers["X-Request-ID"] = request_id
                message["headers"] = headers.raw
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class ManagementAuthASGIMiddleware:
    """Protect management APIs without buffering streaming MCP responses."""

    PUBLIC_API_PATHS = frozenset({"/api/v1/config/public"})

    def __init__(self, app: Any, *, authenticated: Any) -> None:
        self.app = app
        self.authenticated = authenticated

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        protected = (
            path.startswith("/api/v1/")
            and not path.startswith("/api/v1/auth/")
            and path not in self.PUBLIC_API_PATHS
        )
        if protected and not self.authenticated(Request(scope, receive)):
            await JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "management_auth_required",
                        "message": "Management authentication is required",
                        "details": {},
                    }
                },
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class AdminLogin(StrictModel):
    token: str


def _redact_log_line(line: str) -> str:
    """Keep operational context while removing common credential-shaped values."""
    redacted = re.sub(
        r"(?i)(bearer\s+)[^\s,}\]]+",
        r"\1[REDACTED]",
        line,
    )
    return re.sub(
        r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)[^\s,}\]]+",
        r"\1\2[REDACTED]",
        redacted,
    )


def _management_session_hash(session_id: str) -> str:
    """Store only a digest of the browser session cookie in the database."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class ProjectCreate(StrictModel):
    root_path: str
    name: str | None = None
    project_key: str | None = None
    logical_path: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(StrictModel):
    name: str | None = None
    settings: dict[str, Any] | None = None


class AgentJoin(StrictModel):
    agent_key: str
    name: str
    client: str
    model: str
    role: str = "executor"
    branch: str = ""
    worktree: str = ""
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    member_id: str | None = None


class AgentHeartbeat(StrictModel):
    token: str
    status: Literal["online", "idle", "working", "blocked"] = "online"


class AgentLeave(StrictModel):
    token: str


class AgentTokenCreate(StrictModel):
    name: str
    member_id: str | None = None
    permissions: list[str] | None = None
    expires_in_seconds: int | None = None


class AgentTokenRotate(StrictModel):
    expires_in_seconds: int | None = None


class ProjectMemberCreate(StrictModel):
    member_key: str
    name: str
    kind: str = "agent"
    role: str = ""
    status: Literal["invited", "active", "suspended", "revoked"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectMemberUpdate(StrictModel):
    name: str | None = None
    kind: str | None = None
    role: str | None = None
    status: Literal["invited", "active", "suspended", "revoked"] | None = None
    metadata: dict[str, Any] | None = None


class WorkspaceRegister(StrictModel):
    host_key: str
    host_name: str
    local_path: str
    branch: str = ""
    worktree: str = ""
    git_remote: str = ""
    host_metadata: dict[str, Any] = Field(default_factory=dict)
    workspace_metadata: dict[str, Any] = Field(default_factory=dict)


class RoomSync(StrictModel):
    session_id: str | None = None
    token: str | None = None
    after: int = Field(default=0, ge=0)


class MessageCreate(StrictModel):
    body: str
    kind: Literal["message", "decision", "blocker", "system"] = "message"
    session_id: str | None = None
    token: str | None = None
    model_display_name: str | None = Field(
        default=None,
        max_length=MODEL_DISPLAY_NAME_MAX_LENGTH,
    )
    task_id: str | None = None
    mentions: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    requires_ack: bool = False
    priority: int = Field(default=2, ge=0, le=4)
    channel: Literal["public", "task", "review", "system"] = "public"


class EventAcknowledge(StrictModel):
    session_id: str
    token: str


class TaskCreate(StrictModel):
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 2
    actor_session_id: str | None = None
    token: str | None = None


class TaskClaim(StrictModel):
    session_id: str
    token: str


class TaskAssign(StrictModel):
    assigned_by_session_id: str | None = None
    token: str | None = None
    assigned_to_session_id: str | None = None
    target_role: str = ""
    required_capability: str = ""
    note: str = ""


class TaskAssignmentAcknowledge(StrictModel):
    session_id: str
    token: str
    response: Literal["accepted", "declined", "blocked"]
    note: str = ""


class TaskHandoffCreate(StrictModel):
    from_session_id: str | None = None
    token: str | None = None
    to_session_id: str
    summary: str
    completed_items: list[str] = Field(default_factory=list)
    pending_items: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_step: str


class TaskHandoffAcknowledge(StrictModel):
    session_id: str
    token: str
    response: Literal["accepted", "declined", "blocked"]
    note: str = ""


class TaskUpdate(StrictModel):
    status: str | None = None
    title: str | None = None
    description: str | None = None
    acceptance_criteria: list[str] | None = None
    depends_on: list[str] | None = None
    priority: int | None = None
    progress_percent: int | None = None
    current_step: str | None = None
    blocker_reason: str | None = None
    next_step: str | None = None
    session_id: str | None = None
    token: str | None = None


class LeaseCreate(StrictModel):
    session_id: str
    token: str
    path_pattern: str
    mode: Literal["readonly", "shared", "exclusive"] = "exclusive"
    task_id: str | None = None
    reason: str = ""
    ttl_seconds: int | None = None


class LeaseCheck(StrictModel):
    paths: list[str]
    session_id: str | None = None
    token: str | None = None


class WorkReportCreate(StrictModel):
    session_id: str
    token: str
    summary: str
    files: list[str]
    tests: list[dict[str, Any]]
    commit_hash: str = ""
    no_code_change_reason: str = ""


class ReviewCreate(StrictModel):
    reviewer_session_id: str
    token: str
    verdict: Literal["approved", "changes_requested"]
    criteria: list[dict[str, Any]]
    notes: str = ""


class IntegrationCreate(StrictModel):
    integrator_session_id: str | None = None
    token: str | None = None
    result: Literal["done", "failed"]
    summary: str
    files: list[str] = Field(default_factory=list)
    tests: list[dict[str, Any]]
    commit_hash: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    service = AgentChatRoomService(create_database(resolved), resolved)
    service.initialize()
    management_token = os.getenv(resolved.management_token_env, "")
    if resolved.management_auth_required and len(management_token) < 24:
        raise ValueError(
            f"{resolved.management_token_env} must contain at least 24 characters "
            "when management authentication is required"
        )
    public_host = "127.0.0.1" if resolved.host in {"0.0.0.0", "::"} else resolved.host
    base_url = resolved.external_base_url or f"http://{public_host}:{resolved.port}"
    mcp_url = f"{base_url}{resolved.mcp_http_path}"
    mcp_server = create_mcp(
        service,
        host=resolved.host,
        port=resolved.port,
        streamable_http_path=resolved.mcp_http_path,
        stateless_http=resolved.mcp_http_stateless,
        json_response=resolved.mcp_http_json_response,
        auth_required=resolved.mcp_http_auth_required,
        issuer_url=base_url,
        resource_server_url=mcp_url,
    )
    mcp_http_app = (
        mcp_server.streamable_http_app() if resolved.mcp_http_enabled else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if mcp_http_app is None:
                yield
            else:
                async with mcp_server.session_manager.run():
                    yield
        finally:
            service.database.close()

    app = FastAPI(
        title=resolved.product_name,
        version=__version__,
        description="Local-first coordination service for heterogeneous coding agents",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.service = service
    app.state.mcp_server = mcp_server

    def management_authenticated(request: Request) -> bool:
        if not resolved.management_auth_required:
            return True
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer ") and hmac.compare_digest(
            authorization.removeprefix("Bearer "), management_token
        ):
            return True
        session_id = request.cookies.get(resolved.management_cookie_name, "")
        if not session_id:
            return False
        with service.database.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM management_sessions WHERE session_hash = ?",
                (_management_session_hash(session_id),),
            ).fetchone()
        return row is not None and float(row["expires_at"]) > time.time()

    app.add_middleware(
        ManagementAuthASGIMiddleware,
        authenticated=management_authenticated,
    )
    app.add_middleware(RequestIdASGIMiddleware)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.as_dict())

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "service": resolved.product_name}

    @app.get("/health/live")
    def health_live() -> dict[str, Any]:
        """Public process liveness probe with no database access."""
        return {
            "status": "ok",
            "version": __version__,
            "service": resolved.product_name,
        }

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        """Readiness probe for the database/schema boundary.

        Keep failure details deliberately generic so database URLs, driver
        errors, and deployment paths never leak through a public probe.
        """
        try:
            with service.database.connect() as connection:
                row = connection.execute(
                    "SELECT version FROM schema_meta LIMIT 1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("schema_meta is empty")
                schema_version = int(row["version"])
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "version": __version__,
                    "service": resolved.product_name,
                    "database": {"status": "unavailable"},
                },
            )
        return JSONResponse(
            {
                "status": "ready",
                "version": __version__,
                "service": resolved.product_name,
                "database": {
                    "status": "ok",
                    "backend": resolved.database_backend,
                    "schema_version": schema_version,
                },
            }
        )

    @app.get("/api/v1/auth/status")
    def management_auth_status(request: Request) -> dict[str, Any]:
        return {
            "required": resolved.management_auth_required,
            "authenticated": management_authenticated(request),
        }

    @app.post("/api/v1/auth/login")
    def management_login(body: AdminLogin) -> JSONResponse:
        if resolved.management_auth_required and not hmac.compare_digest(
            body.token, management_token
        ):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "invalid_management_token",
                        "message": "Management token is invalid",
                        "details": {},
                    }
                },
            )
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + resolved.management_session_ttl_seconds
        with service.database.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM management_sessions WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO management_sessions(session_hash, expires_at, created_at)
                VALUES (?, ?, ?)
                """,
                (_management_session_hash(session_id), expires_at, now),
            )
        response = JSONResponse(
            {
                "authenticated": True,
                "expires_in_seconds": resolved.management_session_ttl_seconds,
            }
        )
        response.set_cookie(
            resolved.management_cookie_name,
            session_id,
            max_age=resolved.management_session_ttl_seconds,
            httponly=True,
            secure=base_url.startswith("https://"),
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/v1/auth/logout")
    def management_logout(request: Request) -> JSONResponse:
        session_id = request.cookies.get(resolved.management_cookie_name, "")
        if session_id:
            with service.database.connect(write=True) as connection:
                connection.execute(
                    "DELETE FROM management_sessions WHERE session_hash = ?",
                    (_management_session_hash(session_id),),
                )
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(resolved.management_cookie_name, path="/")
        return response

    @app.get("/api/v1/config/public")
    def public_config() -> dict[str, Any]:
        return {
            "product_name": resolved.product_name,
            "deployment_profile": resolved.deployment_profile,
            "default_theme": resolved.default_theme,
            "heartbeat_timeout_seconds": resolved.heartbeat_timeout_seconds,
            "presence_keepalive_enabled": resolved.presence_keepalive_enabled,
            "presence_keepalive_interval_seconds": (
                resolved.presence_keepalive_interval_seconds
            ),
            "config_schema_version": resolved.config_schema_version,
            "default_lease_ttl_seconds": resolved.default_lease_ttl_seconds,
            "presence_refresh_interval_seconds": resolved.presence_refresh_interval_seconds,
            "capabilities": {
                "mcp": True,
                "streamable_http_mcp": resolved.mcp_http_enabled,
                "streamable_http_mcp_path": resolved.mcp_http_path,
                "streamable_http_mcp_json_response": resolved.mcp_http_json_response,
                "streamable_http_mcp_auth_required": resolved.mcp_http_auth_required,
                "sse": True,
                "cli": True,
                "api_version": "v1",
            },
            "proxy": {
                "trusted_headers": resolved.trusted_proxy_headers,
                "trusted_ips_configured": bool(resolved.trusted_proxy_ips),
            },
            "domain": {
                "schema_version": DOMAIN_SCHEMA_VERSION,
                "task_statuses": sorted(TASK_STATUSES),
                "task_transitions": {
                    status: sorted(next_states)
                    for status, next_states in TASK_TRANSITIONS.items()
                },
                "lease_modes": sorted(LEASE_MODES),
                "message_kinds": sorted(MESSAGE_KINDS),
                "message_channels": sorted(MESSAGE_CHANNELS),
                "model_display_name_max_length": MODEL_DISPLAY_NAME_MAX_LENGTH,
                "task_execution_statuses": sorted(TASK_EXECUTION_STATUSES),
                "task_verification_statuses": sorted(TASK_VERIFICATION_STATUSES),
                "task_integration_statuses": sorted(TASK_INTEGRATION_STATUSES),
                "assignment_statuses": sorted(ASSIGNMENT_STATUSES),
                "assignment_responses": sorted(ASSIGNMENT_RESPONSES),
                "handoff_statuses": sorted(HANDOFF_STATUSES),
                "handoff_responses": sorted(HANDOFF_RESPONSES),
                "integration_results": sorted(INTEGRATION_RESULTS),
                "agent_permissions": sorted(AGENT_PERMISSIONS),
                "agent_session_statuses": sorted(AGENT_STATUSES),
                "agent_connection_statuses": ["connected", "disconnected"],
                "agent_identity_statuses": [
                    "registered",
                    "online",
                    "idle",
                    "working",
                    "blocked",
                ],
                "project_member_schema_version": PROJECT_MEMBER_SCHEMA_VERSION,
                "project_member_statuses": sorted(PROJECT_MEMBER_STATUSES),
            },
            "project_settings_defaults": PROJECT_SETTINGS_DEFAULTS,
        }

    @app.get("/api/v1/integrations/mcp")
    def mcp_integration() -> dict[str, Any]:
        return build_mcp_integration(resolved)

    @app.get("/api/v1/projects/{project_id}/integrations/mcp")
    def project_mcp_integration(project_id: str) -> dict[str, Any]:
        return build_mcp_integration(
            resolved,
            project=service.get_project(project_id),
        )

    @app.get("/api/v1/admin/runtime")
    def admin_runtime(lines: int = Query(80, ge=0, le=500)) -> dict[str, Any]:
        """Expose safe operational state for the authenticated management UI."""
        log_path = resolved.data_dir / "server.log"
        log_lines: list[str] = []
        if lines and log_path.is_file():
            log_lines = [
                _redact_log_line(line)
                for line in log_path.read_text(encoding="utf-8", errors="replace")
                .splitlines()[-lines:]
            ]
        pid_path = resolved.data_dir / "server.pid"
        pid = pid_path.read_text(encoding="ascii").strip() if pid_path.is_file() else ""
        return {
            "settings": resolved.public_dict(),
            "paths": {
                "data_dir": str(resolved.data_dir),
                "config_path": str(resolved.config_path) if resolved.config_path else None,
                "log_path": str(log_path),
                "pid_path": str(pid_path),
            },
            "process": {"pid": pid, "managed": bool(pid)},
            "log": {"lines": log_lines, "returned": len(log_lines)},
        }

    @app.get("/api/v1/runtime/effective-config")
    def effective_runtime_config() -> dict[str, Any]:
        """Backward-compatible configuration-only view for operators and scripts."""
        runtime = admin_runtime(lines=0)
        return {
            "settings": runtime["settings"],
            "paths": runtime["paths"],
            "process": runtime["process"],
        }

    @app.get("/api/v1/projects")
    def list_projects() -> dict[str, Any]:
        return {"projects": service.list_projects()}

    @app.post("/api/v1/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        return service.create_project(**body.model_dump())

    @app.delete("/api/v1/projects/{project_id}")
    def remove_project(
        project_id: str,
        permanent: bool = Query(False),
    ) -> dict[str, Any]:
        if permanent:
            return service.delete_project(project_id)
        return service.archive_project(project_id)

    @app.patch("/api/v1/projects/{project_id}")
    def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
        return service.update_project(project_id, **body.model_dump())

    @app.get("/api/v1/projects/{project_id}/export")
    def export_project(project_id: str) -> JSONResponse:
        return JSONResponse(
            service.export_project(project_id),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="agentchatroom-{project_id}.json"'
                )
            },
        )

    @app.get("/api/v1/projects/{project_id}/snapshot")
    def project_snapshot(project_id: str) -> dict[str, Any]:
        return service.snapshot(project_id)

    @app.get("/api/v1/projects/{project_id}/members")
    def list_project_members(
        project_id: str,
        include_revoked: bool = Query(True),
    ) -> dict[str, Any]:
        return {
            "members": service.list_project_members(
                project_id, include_revoked=include_revoked
            )
        }

    @app.post("/api/v1/projects/{project_id}/members", status_code=201)
    def create_project_member(
        request: Request,
        project_id: str,
        body: ProjectMemberCreate,
    ) -> dict[str, Any]:
        return service.create_project_member(
            project_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.patch("/api/v1/projects/{project_id}/members/{member_id}")
    def update_project_member(
        request: Request,
        project_id: str,
        member_id: str,
        body: ProjectMemberUpdate,
    ) -> dict[str, Any]:
        return service.update_project_member(
            project_id,
            member_id,
            **body.model_dump(exclude_unset=True),
            request_id=request.state.request_id,
        )

    @app.delete("/api/v1/projects/{project_id}/members/{member_id}")
    def revoke_project_member(
        request: Request,
        project_id: str,
        member_id: str,
    ) -> dict[str, Any]:
        return service.revoke_project_member(
            project_id,
            member_id,
            request_id=request.state.request_id,
        )

    @app.get("/api/v1/projects/{project_id}/events")
    def list_events(
        project_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return service.list_events(project_id, after=after, limit=limit)

    @app.get("/api/v1/projects/{project_id}/events/stream")
    async def stream_events(
        request: Request,
        project_id: str,
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        service.get_project(project_id)
        header_cursor = request.headers.get("last-event-id", "")
        if header_cursor.isdigit():
            after = max(after, int(header_cursor))

        async def event_stream():
            cursor = after
            idle_ticks = 0
            while not await request.is_disconnected():
                result = service.list_events(project_id, after=cursor, limit=200)
                if result["events"]:
                    for event in result["events"]:
                        cursor = event["id"]
                        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {cursor}\nevent: room_event\ndata: {payload}\n\n"
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    if idle_ticks * resolved.sse_poll_interval_seconds >= 15:
                        yield ": heartbeat\n\n"
                        idle_ticks = 0
                await asyncio.sleep(resolved.sse_poll_interval_seconds)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/projects/{project_id}/agents/join", status_code=201)
    def join_agent(project_id: str, body: AgentJoin) -> dict[str, Any]:
        return service.join_room(project_id, **body.model_dump())

    @app.get("/api/v1/projects/{project_id}/agent-tokens")
    def list_agent_tokens(project_id: str) -> dict[str, Any]:
        return {"credentials": service.list_agent_tokens(project_id)}

    @app.post("/api/v1/projects/{project_id}/agent-tokens", status_code=201)
    def issue_agent_token(
        project_id: str, body: AgentTokenCreate
    ) -> dict[str, Any]:
        return service.issue_agent_token(project_id, **body.model_dump())

    @app.delete(
        "/api/v1/projects/{project_id}/agent-tokens/{credential_id}"
    )
    def revoke_agent_token(
        project_id: str, credential_id: str
    ) -> dict[str, Any]:
        return service.revoke_agent_token(project_id, credential_id)

    @app.post(
        "/api/v1/projects/{project_id}/agent-tokens/{credential_id}/rotate",
        status_code=201,
    )
    def rotate_agent_token(
        project_id: str,
        credential_id: str,
        body: AgentTokenRotate,
    ) -> dict[str, Any]:
        return service.rotate_agent_token(
            project_id,
            credential_id,
            **body.model_dump(),
        )

    @app.get("/api/v1/projects/{project_id}/workspaces")
    def list_workspaces(project_id: str) -> dict[str, Any]:
        return {"workspaces": service.list_workspaces(project_id)}

    @app.post("/api/v1/projects/{project_id}/workspaces", status_code=201)
    def register_workspace(
        project_id: str, body: WorkspaceRegister
    ) -> dict[str, Any]:
        return service.register_workspace(project_id, **body.model_dump())

    @app.get("/api/v1/projects/{project_id}/audit")
    def query_audit(
        project_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        event_type: str | None = None,
        actor_session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return service.query_audit(
            project_id,
            after=after,
            limit=limit,
            event_type=event_type,
            actor_session_id=actor_session_id,
            task_id=task_id,
        )

    @app.post("/api/v1/projects/{project_id}/agents/{session_id}/heartbeat")
    def heartbeat(
        request: Request,
        project_id: str,
        session_id: str,
        body: AgentHeartbeat,
    ) -> dict[str, Any]:
        return service.heartbeat(
            project_id,
            session_id,
            body.token,
            status=body.status,
            request_id=request.state.idempotency_request_id,
        )

    @app.post("/api/v1/projects/{project_id}/agents/{session_id}/leave")
    def leave_session(
        request: Request,
        project_id: str,
        session_id: str,
        body: AgentLeave,
    ) -> dict[str, Any]:
        return service.leave_session(
            project_id,
            session_id,
            body.token,
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/sync")
    def room_sync(project_id: str, body: RoomSync) -> dict[str, Any]:
        return service.room_sync(project_id, **body.model_dump())

    @app.post("/api/v1/projects/{project_id}/messages", status_code=201)
    def post_message(
        request: Request, project_id: str, body: MessageCreate
    ) -> dict[str, Any]:
        return service.post_message(
            project_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/events/{event_id}/acknowledge")
    def acknowledge_event(
        request: Request,
        project_id: str,
        event_id: int,
        body: EventAcknowledge,
    ) -> dict[str, Any]:
        return service.acknowledge_event(
            project_id,
            event_id,
            body.session_id,
            body.token,
            request_id=request.state.request_id,
        )

    @app.get("/api/v1/projects/{project_id}/tasks")
    def list_tasks(project_id: str, status: str | None = None) -> dict[str, Any]:
        return {"tasks": service.list_tasks(project_id, status=status)}

    @app.get("/api/v1/projects/{project_id}/tasks/{task_id}")
    def get_task(project_id: str, task_id: str) -> dict[str, Any]:
        return {"task": service.get_task(project_id, task_id)}

    @app.post("/api/v1/projects/{project_id}/tasks", status_code=201)
    def create_task(
        request: Request, project_id: str, body: TaskCreate
    ) -> dict[str, Any]:
        return service.create_task(
            project_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/tasks/{task_id}/claim")
    def claim_task(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskClaim,
    ) -> dict[str, Any]:
        return service.claim_task(
            project_id,
            task_id,
            body.session_id,
            body.token,
            request_id=request.state.request_id,
        )

    @app.post(
        "/api/v1/projects/{project_id}/tasks/{task_id}/assignments",
        status_code=201,
    )
    def assign_task(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskAssign,
    ) -> dict[str, Any]:
        return service.assign_task(
            project_id,
            task_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post(
        "/api/v1/projects/{project_id}/tasks/{task_id}/assignments/"
        "{assignment_id}/acknowledge"
    )
    def acknowledge_task_assignment(
        request: Request,
        project_id: str,
        task_id: str,
        assignment_id: str,
        body: TaskAssignmentAcknowledge,
    ) -> dict[str, Any]:
        return service.acknowledge_task_assignment(
            project_id,
            task_id,
            assignment_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post(
        "/api/v1/projects/{project_id}/tasks/{task_id}/handoffs",
        status_code=201,
    )
    def create_task_handoff(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskHandoffCreate,
    ) -> dict[str, Any]:
        return service.handoff_task(
            project_id,
            task_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post(
        "/api/v1/projects/{project_id}/tasks/{task_id}/handoffs/"
        "{handoff_id}/acknowledge"
    )
    def acknowledge_task_handoff(
        request: Request,
        project_id: str,
        task_id: str,
        handoff_id: str,
        body: TaskHandoffAcknowledge,
    ) -> dict[str, Any]:
        return service.acknowledge_task_handoff(
            project_id,
            task_id,
            handoff_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.patch("/api/v1/projects/{project_id}/tasks/{task_id}")
    def update_task(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskUpdate,
    ) -> dict[str, Any]:
        return service.update_task(
            project_id,
            task_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.get("/api/v1/projects/{project_id}/leases")
    def list_leases(project_id: str, include_inactive: bool = False) -> dict[str, Any]:
        return {"leases": service.list_leases(project_id, include_inactive=include_inactive)}

    @app.post("/api/v1/projects/{project_id}/leases", status_code=201)
    def acquire_lease(
        request: Request, project_id: str, body: LeaseCreate
    ) -> dict[str, Any]:
        return service.acquire_lease(
            project_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/leases/check")
    def check_leases(project_id: str, body: LeaseCheck) -> dict[str, Any]:
        return service.check_leases(project_id, **body.model_dump())

    @app.delete("/api/v1/projects/{project_id}/leases/{lease_id}")
    def release_lease(
        request: Request,
        project_id: str,
        lease_id: str,
        session_id: str,
        token: str,
    ) -> dict[str, Any]:
        return service.release_lease(
            project_id,
            lease_id,
            session_id,
            token,
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/tasks/{task_id}/reports", status_code=201)
    def work_report(
        request: Request,
        project_id: str,
        task_id: str,
        body: WorkReportCreate,
    ) -> dict[str, Any]:
        return service.submit_work_report(
            project_id,
            task_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/tasks/{task_id}/reviews", status_code=201)
    def review_submit(
        request: Request,
        project_id: str,
        task_id: str,
        body: ReviewCreate,
    ) -> dict[str, Any]:
        return service.submit_review(
            project_id,
            task_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post(
        "/api/v1/projects/{project_id}/tasks/{task_id}/integrations",
        status_code=201,
    )
    def integration_submit(
        request: Request,
        project_id: str,
        task_id: str,
        body: IntegrationCreate,
    ) -> dict[str, Any]:
        return service.submit_integration(
            project_id,
            task_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    web_dir = Path(__file__).with_name("web")
    if web_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

        @app.get("/", include_in_schema=False)
        def web_index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

    if mcp_http_app is not None:
        app.mount("/", mcp_http_app, name="mcp")

    return app
