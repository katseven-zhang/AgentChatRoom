from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders

from . import __version__
from .config import Settings, load_settings
from .database import create_database
from .desktop import DirectoryPickerUnavailable, pick_directory
from .errors import DomainError
from .integrations import build_mcp_integration
from .local_mcp import LocalMcpConfigurator
from .mcp_server import create_mcp
from .project_registration import (
    register_checkout_project,
    remove_checkout_project_registration,
)
from .bootstrap import BOOTSTRAP_SCHEMA_VERSION, BOOTSTRAP_STATES, REQUIRED_ACTIONS
from .task_history import TASK_HISTORY_SCHEMA_VERSION
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
    TASK_INTAKE_STATUSES,
    TASK_INTAKE_TRANSITIONS,
    TASK_PHASES,
    TASK_PHASE_COMMANDS,
    TASK_VIEW_ALL_PHASES,
    TASK_VIEW_ATTENTION_PHASES,
    TASK_VIEW_GROUP_CODES,
    TASK_VIEW_SCHEMA_VERSION,
    TASK_VERIFICATION_STATUSES,
    knowledge_contract,
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

# ---------------------------------------------------------------------------
# Server-side versioned presentation metadata for the task view projection
# (Plan D, event #2788/#2795). The domain layer emits stable codes only;
# localized labels, group ordering, and attention semantics are served here
# as versioned config so REST / MCP / CLI / Web stay aligned by schema
# version instead of by hard-coded front-end maps.
# ---------------------------------------------------------------------------
TASK_VIEW_LABELS = {
    "todo": "待认领",
    "claimed": "已认领",
    "in_progress": "执行中",
    "blocked": "阻塞",
    "awaiting_review": "待验收",
    "changes_requested": "已退回",
    "pending_integration": "待集成",
    "integration_failed": "集成失败",
    "done": "已完成",
    "cancelled": "已取消",
    "unclassified": "未归类",
}
TASK_VIEW_GROUP_LABELS = {
    "claimable": "待认领",
    "active": "进行中",
    "review": "待验收",
    "integration": "待集成",
    "done": "已完成",
    "cancelled": "已取消",
    "unclassified": "未归类",
}
# Active sub-group ordering: 修改中(red) → 阻塞(orange) → 执行中 → 已认领.
TASK_VIEW_ACTIVE_SUBGROUP_ORDER = (
    "changes_requested",
    "blocked",
    "in_progress",
    "claimed",
)
TASK_VIEW_ATTENTION_LABEL = "需要处理"
# Legacy status wording kept for compatible outputs (event #2795 correction 5).
TASK_VIEW_LEGACY_STATUS_LABELS = {
    "todo": "待认领",
    "claimed": "已认领",
    "in_progress": "执行中",
    "blocked": "阻塞",
    "awaiting_review": "已提交",
    "verified": "已通过",
    "done": "已完成",
    "cancelled": "已取消",
}
TASK_VIEW_EXECUTION_LABELS = {
    "todo": "待开始",
    "claimed": "已认领",
    "in_progress": "执行中",
    "blocked": "阻塞",
    "completed": "执行完成",
    "cancelled": "已取消",
}
TASK_VIEW_VERIFICATION_LABELS = {
    "not_required": "无需验证",
    "pending": "待验证",
    "changes_requested": "需修改",
    "approved": "已通过",
}
TASK_VIEW_INTEGRATION_LABELS = {
    "pending": "待集成",
    "done": "已集成",
    "failed": "集成失败",
}


def task_view_presentation_config() -> dict[str, Any]:
    return {
        "schema_version": TASK_VIEW_SCHEMA_VERSION,
        "phases": list(TASK_VIEW_ALL_PHASES),
        "phase_labels": dict(TASK_VIEW_LABELS),
        "groups": list(TASK_VIEW_GROUP_CODES),
        "group_labels": dict(TASK_VIEW_GROUP_LABELS),
        "active_subgroup_order": list(TASK_VIEW_ACTIVE_SUBGROUP_ORDER),
        "attention_phases": sorted(TASK_VIEW_ATTENTION_PHASES),
        "attention_label": TASK_VIEW_ATTENTION_LABEL,
        "execution_labels": dict(TASK_VIEW_EXECUTION_LABELS),
        "verification_labels": dict(TASK_VIEW_VERIFICATION_LABELS),
        "integration_labels": dict(TASK_VIEW_INTEGRATION_LABELS),
        "status_labels": dict(TASK_VIEW_LEGACY_STATUS_LABELS),
    }


def trusted_proxy_peer(peer_host: str, trusted_proxy_ips: str) -> bool:
    raw = str(peer_host or "").strip()
    if not raw or raw == "unknown":
        return False
    try:
        peer = ipaddress.ip_address(raw)
    except ValueError:
        return False
    for item in str(trusted_proxy_ips or "").split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                if peer in ipaddress.ip_network(candidate, strict=False):
                    return True
            elif peer == ipaddress.ip_address(candidate):
                return True
        except ValueError:
            continue
    return False


def request_client_ip(
    request: Request,
    *,
    trusted_proxy_headers: bool,
    trusted_proxy_ips: str,
) -> str:
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_headers and trusted_proxy_peer(peer, trusted_proxy_ips):
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer or "unknown"


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


_CREDENTIAL_VALUE = r"[^\s,;}\]&\"']+"


def _redact_log_line(line: str) -> str:
    """Keep operational context while removing common credential-shaped values."""
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*)[^\r\n]+",
        r"\1[REDACTED]",
        line,
    )
    redacted = re.sub(
        r"(?i)(bearer\s+)" + _CREDENTIAL_VALUE,
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(cookie\s*[:=]\s*)[^\r\n]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)((?:^|[;,\s])[a-z0-9_-]*cookie[a-z0-9_-]*\s*[:=]\s*)" + _CREDENTIAL_VALUE,
        r"\1[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)" + _CREDENTIAL_VALUE,
        r"\1\2[REDACTED]",
        redacted,
    )


class CredentialRedactingLogFilter(logging.Filter):
    """Redact credentials in uvicorn access logs and other logger records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_log_line(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _redact_log_line(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    key: _redact_log_line(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
        return True


def install_credential_log_redaction() -> None:
    """Attach credential redaction to server loggers once per process."""
    log_filter = CredentialRedactingLogFilter()
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "agentchatroom"):
        logger = logging.getLogger(name)
        if not any(
            isinstance(existing, CredentialRedactingLogFilter)
            for existing in logger.filters
        ):
            logger.addFilter(log_filter)


def _reject_credentials_in_query(request: Request) -> None:
    keys = {key.lower() for key in request.query_params.keys()}
    if keys & {"token", "session_id", "access_token"}:
        raise DomainError(
            "credentials_in_query_rejected",
            "Agent credentials must be sent in the JSON body, not the URL query string",
            status_code=400,
        )


def _management_session_hash(session_id: str) -> str:
    """Store only a digest of the browser session cookie in the database."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class ProjectCreate(StrictModel):
    root_path: str
    name: str | None = None
    logical_path: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class LocalFolderPickRequest(StrictModel):
    initial_path: str = Field(default="", max_length=4096)


class LocalMcpApplyRequest(StrictModel):
    expected_current_sha256: str = Field(min_length=64, max_length=64)


class ProjectUpdate(StrictModel):
    name: str | None = None
    settings: dict[str, Any] | None = None


class AgentJoin(StrictModel):
    agent_key: str = ""
    software_key: str = ""
    name: str
    client: str
    model: str
    role: str = "executor"
    branch: str = ""
    worktree: str = ""
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    member_id: str | None = None
    host_id: str | None = None
    workspace_id: str | None = None
    credential_id: str | None = None


class AgentHeartbeat(StrictModel):
    token: str


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


class TaskIntakeCreate(StrictModel):
    raw_description: str
    target_member_id: str
    target_session_id: str | None = None
    created_by_session_id: str | None = None
    token: str | None = None


class TaskIntakeAcknowledge(StrictModel):
    session_id: str
    token: str
    response: Literal["accepted", "declined", "blocked"] = "accepted"
    note: str = ""


class TaskIntakeReassign(StrictModel):
    target_member_id: str
    target_session_id: str | None = None
    note: str = ""


class TaskIntakeDefine(StrictModel):
    session_id: str
    token: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=2, ge=0, le=4)
    note: str = ""


class TaskClaim(StrictModel):
    session_id: str
    token: str


class TaskRelease(StrictModel):
    reason_code: Literal[
        "quota_exhausted",
        "agent_unavailable",
        "user_requested",
        "reassignment_needed",
        "other",
    ]
    reason: str = ""
    session_id: str | None = None
    token: str | None = None


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


class LeaseRelease(StrictModel):
    session_id: str
    token: str


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


class KnowledgeCandidateCreate(StrictModel):
    session_id: str
    token: str
    title: str
    body: str
    kind: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    source_type: str = "manual"
    source_task_id: str = ""
    source_report_id: str = ""
    source_review_id: str = ""
    source_integration_id: str = ""
    source_event_ids: list[int] = Field(default_factory=list)
    asset_id: str = ""


class KnowledgeReviewCreate(StrictModel):
    reviewer_session_id: str
    token: str
    verdict: Literal["approved", "changes_requested"]
    criteria: list[dict[str, Any]]
    notes: str = ""


class KnowledgeAssetAction(StrictModel):
    session_id: str
    token: str
    reason: str = ""


class SSELimiter:
    """Bound in-process SSE occupancy by Project and client address."""

    def __init__(self, *, max_per_project: int, max_per_ip: int) -> None:
        self.max_per_project = max_per_project
        self.max_per_ip = max_per_ip
        self._guard = threading.Lock()
        self._project_counts: dict[str, int] = {}
        self._ip_counts: dict[str, int] = {}

    def acquire(self, project_id: str, client_ip: str) -> None:
        with self._guard:
            project_count = self._project_counts.get(project_id, 0)
            ip_count = self._ip_counts.get(client_ip, 0)
            if project_count >= self.max_per_project:
                raise DomainError(
                    "sse_limit_exceeded",
                    "Too many event stream clients for this Project",
                    status_code=429,
                    details={
                        "limit": self.max_per_project,
                        "scope": "project",
                    },
                )
            if ip_count >= self.max_per_ip:
                raise DomainError(
                    "sse_limit_exceeded",
                    "Too many event stream clients from this address",
                    status_code=429,
                    details={"limit": self.max_per_ip, "scope": "ip"},
                )
            self._project_counts[project_id] = project_count + 1
            self._ip_counts[client_ip] = ip_count + 1

    def release(self, project_id: str, client_ip: str) -> None:
        with self._guard:
            project_count = self._project_counts.get(project_id, 0) - 1
            ip_count = self._ip_counts.get(client_ip, 0) - 1
            if project_count > 0:
                self._project_counts[project_id] = project_count
            else:
                self._project_counts.pop(project_id, None)
            if ip_count > 0:
                self._ip_counts[client_ip] = ip_count
            else:
                self._ip_counts.pop(client_ip, None)


def create_app(
    settings: Settings | None = None,
    *,
    directory_picker: Callable[[str], str | None] | None = None,
    local_mcp_configurator: LocalMcpConfigurator | None = None,
) -> FastAPI:
    resolved = settings or load_settings()
    resolved_directory_picker = directory_picker or pick_directory
    resolved_local_mcp_configurator = (
        local_mcp_configurator or LocalMcpConfigurator()
    )
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
            service.close()

    app = FastAPI(
        title=resolved.product_name,
        version=__version__,
        description="Local-first coordination service for heterogeneous coding agents",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.service = service
    app.state.mcp_server = mcp_server
    app.state.directory_picker = resolved_directory_picker
    app.state.local_mcp_configurator = resolved_local_mcp_configurator

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

    def browser_session_valid(session_id: str) -> bool:
        if not session_id:
            return False
        with service.database.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM management_sessions WHERE session_hash = ?",
                (_management_session_hash(session_id),),
            ).fetchone()
        return row is not None and float(row["expires_at"]) > time.time()

    def issue_browser_session() -> tuple[str, JSONResponse]:
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
        payload = {
            "authenticated": True,
            "expires_in_seconds": resolved.management_session_ttl_seconds,
        }
        return session_id, JSONResponse(payload)

    def attach_browser_cookie(response: JSONResponse, session_id: str) -> JSONResponse:
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

    def event_stream_client_ip(request: Request) -> str:
        return request_client_ip(
            request,
            trusted_proxy_headers=resolved.trusted_proxy_headers,
            trusted_proxy_ips=resolved.trusted_proxy_ips,
        )

    def authorize_event_stream(request: Request, project_id: str) -> None:
        session_id = (
            request.headers.get("x-agentchatroom-session-id")
            or request.headers.get("x-session-id")
            or ""
        ).strip()
        token = (request.headers.get("x-agentchatroom-token") or "").strip()
        authorization = request.headers.get("authorization", "")
        bearer = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else ""
        )
        if session_id and not token:
            token = bearer
        if session_id and token:
            service.verify_session(project_id, session_id, token)
            return
        if bearer:
            service.authenticate_agent_token(
                bearer,
                project_id=project_id,
                required_permission="room:read",
            )
            return
        cookie = request.cookies.get(resolved.management_cookie_name, "")
        if browser_session_valid(cookie):
            return
        raise DomainError(
            "unauthorized_sse",
            "Event stream requires Agent session credentials or a browser session",
            status_code=401,
        )

    sse_limiter = SSELimiter(
        max_per_project=resolved.max_sse_clients_per_project,
        max_per_ip=resolved.sse_per_ip_limit,
    )

    app.add_middleware(
        ManagementAuthASGIMiddleware,
        authenticated=management_authenticated,
    )
    app.add_middleware(RequestIdASGIMiddleware)
    install_credential_log_redaction()

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
    def management_auth_status(request: Request) -> JSONResponse:
        cookie = request.cookies.get(resolved.management_cookie_name, "")
        authenticated = management_authenticated(request)
        response = JSONResponse(
            {
                "required": resolved.management_auth_required,
                "authenticated": authenticated,
            }
        )
        if (
            not resolved.management_auth_required
            and authenticated
            and not browser_session_valid(cookie)
        ):
            session_id, _issued = issue_browser_session()
            response = JSONResponse(
                {
                    "required": False,
                    "authenticated": True,
                    "expires_in_seconds": resolved.management_session_ttl_seconds,
                }
            )
            attach_browser_cookie(response, session_id)
        return response

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
        session_id, response = issue_browser_session()
        return attach_browser_cookie(response, session_id)

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
            "max_sse_clients_per_project": resolved.max_sse_clients_per_project,
            "sse_per_ip_limit": resolved.sse_per_ip_limit,
            "token_touch_interval_seconds": resolved.token_touch_interval_seconds,
            "token_touch_min_calls": resolved.token_touch_min_calls,
            "capabilities": {
                "mcp": True,
                "local_folder_picker": resolved.deployment_profile == "local",
                "local_mcp_config_assistant": (
                    resolved.deployment_profile == "local"
                ),
                "streamable_http_mcp": resolved.mcp_http_enabled,
                "streamable_http_mcp_path": resolved.mcp_http_path,
                "streamable_http_mcp_json_response": resolved.mcp_http_json_response,
                "streamable_http_mcp_auth_required": resolved.mcp_http_auth_required,
                "sse": True,
                "cli": True,
                "room_bootstrap": True,
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
                "task_phases": list(TASK_PHASES),
                "task_phase_commands": dict(TASK_PHASE_COMMANDS),
                "task_view": task_view_presentation_config(),
                "task_history_schema_version": TASK_HISTORY_SCHEMA_VERSION,
                "task_intake_statuses": sorted(TASK_INTAKE_STATUSES),
                "bootstrap_schema_version": BOOTSTRAP_SCHEMA_VERSION,
                "bootstrap_states": list(BOOTSTRAP_STATES),
                "bootstrap_required_actions": dict(REQUIRED_ACTIONS),
                "task_intake_transitions": {
                    status: sorted(next_states)
                    for status, next_states in TASK_INTAKE_TRANSITIONS.items()
                },
                "assignment_statuses": sorted(ASSIGNMENT_STATUSES),
                "assignment_responses": sorted(ASSIGNMENT_RESPONSES),
                "handoff_statuses": sorted(HANDOFF_STATUSES),
                "handoff_responses": sorted(HANDOFF_RESPONSES),
                "integration_results": sorted(INTEGRATION_RESULTS),
                "agent_permissions": sorted(AGENT_PERMISSIONS),
                "agent_session_statuses": ["online", "offline"],
                "agent_connection_statuses": ["connected", "disconnected"],
                "agent_identity_statuses": [
                    "registered",
                    "online",
                ],
                "project_member_schema_version": PROJECT_MEMBER_SCHEMA_VERSION,
                "project_member_statuses": sorted(PROJECT_MEMBER_STATUSES),
                "knowledge": knowledge_contract(kinds=resolved.knowledge_kinds),
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

    def local_mcp_profile(
        project_id: str, profile_id: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        integration = build_mcp_integration(
            resolved,
            project=service.get_project(project_id),
        )
        return integration, integration["profiles"].get(profile_id)

    @app.get(
        "/api/v1/projects/{project_id}/integrations/mcp/local/{profile_id}/plan"
    )
    def local_mcp_plan(project_id: str, profile_id: str) -> dict[str, Any]:
        _integration, profile = local_mcp_profile(project_id, profile_id)
        return resolved_local_mcp_configurator.plan(
            deployment_profile=resolved.deployment_profile,
            profile_id=profile_id,
            profile=profile,
        )

    @app.post(
        "/api/v1/projects/{project_id}/integrations/mcp/local/{profile_id}/apply"
    )
    def apply_local_mcp_config(
        project_id: str,
        profile_id: str,
        body: LocalMcpApplyRequest,
    ) -> dict[str, Any]:
        _integration, profile = local_mcp_profile(project_id, profile_id)
        return resolved_local_mcp_configurator.apply(
            deployment_profile=resolved.deployment_profile,
            profile_id=profile_id,
            profile=profile,
            expected_current_sha256=body.expected_current_sha256,
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

    @app.post("/api/v1/local/folders/pick")
    def pick_local_folder(body: LocalFolderPickRequest) -> dict[str, Any]:
        if resolved.deployment_profile != "local":
            raise DomainError(
                "local_folder_picker_unavailable",
                "The system folder picker is available only in local deployments",
                status_code=409,
            )
        try:
            selected = resolved_directory_picker(body.initial_path)
        except DirectoryPickerUnavailable as error:
            raise DomainError(
                "local_folder_picker_unavailable",
                "The system folder picker is unavailable; enter the project path manually",
                status_code=503,
            ) from error
        if selected is None:
            return {"cancelled": True, "path": ""}
        selected_path = Path(selected).expanduser().resolve()
        if not selected_path.is_dir():
            raise DomainError(
                "local_folder_picker_invalid_selection",
                "The selected project folder does not exist",
                status_code=500,
            )
        return {"cancelled": False, "path": str(selected_path)}

    @app.post("/api/v1/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        values = body.model_dump()
        project = service.create_project(**values)
        register_checkout_project(body.root_path, project, replace_existing=True)
        return project

    @app.delete("/api/v1/projects/{project_id}")
    def remove_project(
        project_id: str,
        permanent: bool = Query(False),
    ) -> dict[str, Any]:
        if permanent:
            snapshot = service.snapshot(project_id)
            project = snapshot["project"]
            checkout_paths = {
                str(project["root_path"]),
                *(
                    str(agent.get("worktree", "")).strip()
                    for agent in snapshot["agents"]
                    if str(agent.get("worktree", "")).strip()
                ),
            }
            deleted = service.delete_project(project_id)
            removed = 0
            cleanup_errors: list[dict[str, Any]] = []
            for checkout_path in sorted(checkout_paths):
                try:
                    removed += int(
                        remove_checkout_project_registration(
                            checkout_path,
                            project_key=str(project["project_key"]),
                            logical_path=str(project.get("logical_path", "") or ""),
                        )
                    )
                except DomainError as error:
                    cleanup_errors.append(error.as_dict()["error"])
            deleted["project_registration"] = {
                "removed": removed,
                "cleanup_errors": cleanup_errors,
            }
            return deleted
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
        authorize_event_stream(request, project_id)
        client_ip = event_stream_client_ip(request)
        sse_limiter.acquire(project_id, client_ip)
        header_cursor = request.headers.get("last-event-id", "")
        if header_cursor.isdigit():
            after = max(after, int(header_cursor))

        async def event_stream():
            cursor = after
            idle_ticks = 0
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    result = service.list_events(project_id, after=cursor, limit=200)
                    if result["events"]:
                        for event in result["events"]:
                            cursor = event["id"]
                            payload = json.dumps(
                                event, ensure_ascii=False, separators=(",", ":")
                            )
                            yield f"id: {cursor}\nevent: room_event\ndata: {payload}\n\n"
                        idle_ticks = 0
                    else:
                        idle_ticks += 1
                        if idle_ticks * resolved.sse_poll_interval_seconds >= 15:
                            yield ": heartbeat\n\n"
                            idle_ticks = 0
                    await asyncio.sleep(resolved.sse_poll_interval_seconds)
            finally:
                sse_limiter.release(project_id, client_ip)

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
    def list_tasks(
        project_id: str,
        status: str | None = None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        return {
            "tasks": service.list_tasks(project_id, status=status, phase=phase)
        }

    @app.get("/api/v1/projects/{project_id}/tasks/by-number/{task_number}")
    def get_task_by_number(project_id: str, task_number: int) -> dict[str, Any]:
        return {"task": service.get_task_by_number(project_id, task_number)}

    @app.get("/api/v1/projects/{project_id}/tasks/{task_id}")
    def get_task(project_id: str, task_id: str) -> dict[str, Any]:
        return {"task": service.get_task(project_id, task_id)}

    @app.get("/api/v1/projects/{project_id}/tasks/{task_id}/history")
    def list_task_history(
        project_id: str,
        task_id: str,
        after: int = Query(0, ge=0),
        before: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        event_type: str | None = None,
    ) -> dict[str, Any]:
        return service.list_task_history(
            project_id,
            task_id,
            after=after,
            before=before,
            limit=limit,
            event_type=event_type or "",
        )

    @app.get("/api/v1/projects/{project_id}/task-intakes/targets")
    def list_task_intake_targets(project_id: str) -> dict[str, Any]:
        return {"targets": service.list_task_intake_targets(project_id)}

    @app.get("/api/v1/projects/{project_id}/task-intakes")
    def list_task_intakes(project_id: str, status: str | None = None) -> dict[str, Any]:
        return {"intakes": service.list_task_intakes(project_id, status=status)}

    @app.get("/api/v1/projects/{project_id}/task-intakes/{intake_id}")
    def get_task_intake(project_id: str, intake_id: str) -> dict[str, Any]:
        return {"intake": service.get_task_intake(project_id, intake_id)}

    @app.post("/api/v1/projects/{project_id}/task-intakes", status_code=201)
    def submit_task_intake(
        request: Request, project_id: str, body: TaskIntakeCreate
    ) -> dict[str, Any]:
        return service.submit_task_intake(
            project_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/task-intakes/{intake_id}/acknowledge")
    def acknowledge_task_intake(
        request: Request,
        project_id: str,
        intake_id: str,
        body: TaskIntakeAcknowledge,
    ) -> dict[str, Any]:
        return service.acknowledge_task_intake(
            project_id,
            intake_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/task-intakes/{intake_id}/reassign")
    def reassign_task_intake(
        request: Request,
        project_id: str,
        intake_id: str,
        body: TaskIntakeReassign,
    ) -> dict[str, Any]:
        return service.reassign_task_intake(
            project_id,
            intake_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/task-intakes/{intake_id}/define", status_code=201)
    def define_task_from_intake(
        request: Request,
        project_id: str,
        intake_id: str,
        body: TaskIntakeDefine,
    ) -> dict[str, Any]:
        return service.define_task_from_intake(
            project_id,
            intake_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

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

    @app.post("/api/v1/projects/{project_id}/tasks/{task_id}/release")
    def release_task(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskRelease,
    ) -> dict[str, Any]:
        _reject_credentials_in_query(request)
        return service.release_task(
            project_id,
            task_id,
            **body.model_dump(),
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
        body: LeaseRelease,
    ) -> dict[str, Any]:
        _reject_credentials_in_query(request)
        return service.release_lease(
            project_id,
            lease_id,
            body.session_id,
            body.token,
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

    @app.get("/api/v1/projects/{project_id}/knowledge/assets")
    def list_knowledge_assets(
        project_id: str,
        status: str | None = None,
        kind: str | None = None,
        source_task_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "assets": service.list_knowledge_assets(
                project_id,
                status=status,
                kind=kind,
                source_task_id=source_task_id,
            )
        }

    @app.post("/api/v1/projects/{project_id}/knowledge/assets", status_code=201)
    def knowledge_candidate_submit(
        request: Request,
        project_id: str,
        body: KnowledgeCandidateCreate,
    ) -> dict[str, Any]:
        return service.submit_knowledge_candidate(
            project_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.get("/api/v1/projects/{project_id}/knowledge/assets/{asset_id}")
    def get_knowledge_asset(
        project_id: str, asset_id: str, version_id: str = ""
    ) -> dict[str, Any]:
        return service.get_knowledge_asset(project_id, asset_id, version_id=version_id)

    @app.post(
        "/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/reviews",
        status_code=201,
    )
    def knowledge_review_submit(
        request: Request,
        project_id: str,
        asset_id: str,
        body: KnowledgeReviewCreate,
    ) -> dict[str, Any]:
        return service.submit_knowledge_review(
            project_id,
            asset_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post(
        "/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/supersede"
    )
    def knowledge_supersede(
        request: Request,
        project_id: str,
        asset_id: str,
        body: KnowledgeAssetAction,
    ) -> dict[str, Any]:
        return service.supersede_knowledge_asset(
            project_id,
            asset_id,
            **body.model_dump(),
            request_id=request.state.request_id,
        )

    @app.post("/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/archive")
    def knowledge_archive(
        request: Request,
        project_id: str,
        asset_id: str,
        body: KnowledgeAssetAction,
    ) -> dict[str, Any]:
        return service.archive_knowledge_asset(
            project_id,
            asset_id,
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
