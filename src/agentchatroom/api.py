from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders

from . import __version__
from .config import Settings, build_service_base_url, load_settings, rewrite_config_text
from .database import create_database
from .desktop import DirectoryPickerUnavailable, pick_directory
from .errors import DomainError
from .integrations import build_mcp_integration
from .local_mcp import LocalMcpConfigurator
from .runtime_log import tail_text_lines
from .mcp_server import (
    adopt_transport_binding,
    configure_session_bindings,
    configure_transport_tombstones,
    create_mcp,
    http_transport_binding_alive,
    lookup_transport_tombstone,
    retain_transport_tombstone,
    session_transport_state,
    transport_session_active,
)
from .mcp_http_adoption import with_session_adoption
from .mcp_http_recovery import with_expired_session_hints
from .presence import LocalPresenceManager
from .service_lifetime import running_service
from .project_registration import (
    register_checkout_project,
    remove_project_coordination_instructions,
    remove_checkout_project_registration,
    validate_project_coordination_instructions,
    write_project_coordination_instructions,
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


def _is_expected_windows_proactor_disconnect(
    context: dict[str, Any], *, platform_name: str | None = None
) -> bool:
    """Identify the harmless Windows callback raised by an HTTP peer closing."""
    if (platform_name or os.name) != "nt":
        return False
    error = context.get("exception")
    if not isinstance(error, ConnectionResetError):
        return False
    if getattr(error, "winerror", None) != 10054:
        return False
    return "_ProactorBasePipeTransport._call_connection_lost" in repr(
        context.get("handle")
    )


def _windows_http_exception_handler(
    previous_handler: Callable[[asyncio.AbstractEventLoop, dict[str, Any]], None] | None,
) -> Callable[[asyncio.AbstractEventLoop, dict[str, Any]], None]:
    def handle_exception(
        loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        if _is_expected_windows_proactor_disconnect(context):
            return
        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    return handle_exception


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
        # Idempotency and correlation are different keys and must not share
        # one: X-Request-ID is a correlation header that gateways commonly
        # reuse across a whole connection, so treating it as an idempotency
        # key made two genuinely different writes collide with
        # idempotency_conflict 409 for the whole retention window. Only an
        # explicit Idempotency-Key deduplicates a write.
        idempotency_key = (request.headers.get("idempotency-key") or "").strip()
        correlation_id = (request.headers.get("x-request-id") or "").strip()
        supplied = idempotency_key or correlation_id
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
        if idempotency_key and not REQUEST_ID_PATTERN.fullmatch(idempotency_key):
            await JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_request_id",
                        "message": (
                            "Idempotency-Key must be 1-128 safe ASCII characters"
                        ),
                        "details": {},
                    }
                },
            )(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        # Every write routes on this, not on request_id: only an explicit
        # Idempotency-Key makes a write replayable.
        state["idempotency_request_id"] = idempotency_key or None

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                message = dict(message)
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                headers["X-Request-ID"] = request_id
                message["headers"] = headers.raw
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def bearer_token(authorization: str) -> str:
    """Extract a bearer credential, accepting any spelling of the scheme.

    RFC 7235 defines the scheme as case-insensitive, and the two authentication
    entry points used to disagree about it: management required the literal
    "Bearer " while the event stream accepted any case, so the same valid
    lowercase header was a 401 on one path and accepted on the other.
    """
    text = str(authorization or "").strip()
    if text.lower().startswith("bearer "):
        return text[len("bearer ") :].strip()
    return ""


def _same_origin_browser_request(request: Request) -> bool:
    """Did the browser itself send this from the app's own page?

    ``Sec-Fetch-Site`` is attached by the browser to every subresource request and
    a cross-site document cannot forge it, so it is the cheapest way to keep the
    local Web UI's event stream working without minting a session row. Missing or
    unknown values are refused.
    """
    site = str(request.headers.get("sec-fetch-site", "")).strip().lower()
    return site in {"same-origin", "none"}


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
        if protected:
            authorized = self.authenticated(Request(scope, receive))
            if inspect.isawaitable(authorized):
                authorized = await authorized
            if not authorized:
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


class ProjectDocumentUpsert(StrictModel):
    doc_key: str
    kind: str
    title: str
    content: str


class BackupSettingsUpdate(StrictModel):
    auto_backup_enabled: bool | None = None
    auto_backup_interval_seconds: int | None = None
    auto_backup_max_kept: int | None = None


class BackupRestoreRequest(StrictModel):
    backup_path: str
    confirm: str
    allow_data_loss: bool = False


# A credential value: any run of characters that is not whitespace, a
# separator, a closing bracket, a quote, or the "[" that opens this policy's
# own marker. Excluding "[" is what makes redaction idempotent - without it a
# value that already reads "[REDACTED]" is matched again and the trailing "]"
# is left behind as "[REDACTED]]".
_CREDENTIAL_VALUE = r"[^\s,;}\]&\"'\[]+"

# Stand-in for the marker while the rules run. It contains a NUL, which no
# rule's value class accepts, so a rule cannot match another rule's output.
_REDACTION_SENTINEL = "\x00redacted\x00"

# How long the backup scheduler sleeps between reschedule checks. Short enough
# that a shortened interval takes effect within a second of the save, long
# enough that a period spanning hours still costs a handful of wakeups.
BACKUP_TICK_SLICE_SECONDS = 1.0


def redact_log_line(line: str) -> str:
    """Single log-redaction policy shared by API filters and the GUI log view.

    Keeps operational context while masking authorization/bearer/cookie/token
    assignments, provider API keys (sk-/ghp_/gho_/xox*), and bare ``acr.*``
    Agent Token literals so server.log, admin_runtime, and GUI tails match.

    Redaction is idempotent: a value this policy already replaced is left
    alone, because the assignment rules would otherwise read
    "token=[REDACTED]" as a fresh value and append another "[REDACTED]".
    Existing markers are parked before the rules run and restored after, so a
    line mixing an already-masked value with a real secret still has the
    secret masked.
    """
    # Everything this policy already replaced is parked behind a sentinel for
    # the duration, and every rule below writes the sentinel rather than the
    # marker itself. Two rules can then never see each other's output: the
    # second cookie rule used to fire on what the first one had just produced,
    # yielding "cookie:[REDACTED][REDACTED]".
    # Only the marker is parked, never the blanks in front of it: a value
    # pattern starting at the space in "cookie: [REDACTED]" would consume the
    # separator and rewrite the line as "cookie:[REDACTED]" on every pass.
    redacted = line.replace("[REDACTED]", _REDACTION_SENTINEL)
    redacted = re.sub(
        r"(?i)(authorization[ \t]*[:=][ \t]*)[^\r\n\x00]+",
        r"\1" + _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"(?i)(bearer\s+)" + _CREDENTIAL_VALUE,
        r"\1" + _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"(?i)(cookie[ \t]*[:=][ \t]*)[^\r\n\x00]+",
        r"\1" + _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"(?i)((?:^|[;,\s])[a-z0-9_-]*cookie[a-z0-9_-]*\s*[:=]\s*)"
        + _CREDENTIAL_VALUE,
        r"\1" + _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"(?i)(token|secret|password|api[_-]?key)(\s*[:=]\s*)" + _CREDENTIAL_VALUE,
        r"\1\2" + _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{8,}\b",
        _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b",
        _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
        _REDACTION_SENTINEL,
        redacted,
    )
    redacted = re.sub(
        r"\bacr\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", _REDACTION_SENTINEL, redacted
    )
    return redacted.replace(_REDACTION_SENTINEL, "[REDACTED]")


# Internal alias so existing call sites and tests keep one name while the
# GUI imports the public shared policy.
_redact_log_line = redact_log_line


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
        self._redact_exception(record)
        return True

    @staticmethod
    def _redact_exception(record: logging.LogRecord) -> None:
        """Redact a formatted traceback attached to the record.

        ``logger.exception(...)`` keeps the text out of ``msg`` and puts it in
        ``exc_info``; the handler formats that only when it emits. A failing
        database call is exactly where a connection string ends up in the
        traceback, and it is only ever seen here - no ``msg`` rewrite reaches
        it. The redacted text is pushed back onto the record so the formatter
        uses it instead of the original.
        """
        exc_info = getattr(record, "exc_info", None)
        if not exc_info:
            return
        # Already handled by a filter upstream: the tuple is gone, and
        # re-formatting would append a second traceback.
        if not isinstance(exc_info, tuple):
            return
        try:
            text = "".join(traceback.format_exception(*exc_info))
        except Exception:
            # Never let redaction break logging itself.
            return
        if not text:
            return
        record.exc_text = _redact_log_line(text)


def install_credential_log_redaction() -> None:
    """Redact credentials on the way to every handler that writes server.log.

    A filter attached to a *logger* only runs for records logged through that
    exact logger. The application logs through ``agentchatroom.api`` and
    ``agentchatroom.services``, which are children of ``agentchatroom``: their
    records reach a parent handler without the parent's ``Logger.filters``
    being consulted, so a traceback carrying a connection string landed in
    server.log verbatim. The display path had its own redaction, which is why
    this stayed invisible on screen and unsafe on disk.

    Handlers are the choke point every record passes on its way to a file, so
    that is where redaction belongs. The named loggers are kept as well: a
    logger without a handler of its own has no other place to apply this, and
    keeping them costs one extra pass on an already-redacted message.
    """
    log_filter = CredentialRedactingLogFilter()
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "agentchatroom"):
        logger = logging.getLogger(name)
        if not any(
            isinstance(existing, CredentialRedactingLogFilter)
            for existing in logger.filters
        ):
            logger.addFilter(log_filter)
        for handler in logger.handlers:
            _attach_redaction(handler, log_filter)
    # A record can reach the root logger's handlers with none of the above
    # filters on its own logger, which is the case for a library logging under
    # its own namespace. Redact there too rather than trust the caller.
    _attach_redaction(logging.getLogger(), log_filter)


def _attach_redaction(
    handler: logging.Handler, log_filter: CredentialRedactingLogFilter
) -> None:
    """Add the redacting filter to one handler, once per handler."""
    if any(
        isinstance(existing, CredentialRedactingLogFilter)
        for existing in handler.filters
    ):
        return
    # handler filters run last, after the record is fully formatted, so they
    # see exception tracebacks and %-style args that a logger-level filter
    # can miss.
    handler.addFilter(log_filter)


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


def _cleanup_failure(error: DomainError) -> dict[str, Any]:
    """Surface a checkout-cleanup failure with a required next step."""
    payload = dict(error.as_dict()["error"])
    details = dict(payload.get("details") or {})
    details.setdefault(
        "required_action",
        "retry_checkout_cleanup_or_remove_the_generated_files",
    )
    payload["details"] = details
    return payload


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
    # Presented project credential (acr.*). When set, the server authenticates
    # it and that credential's member is the identity. Body member_id and
    # credential_id are ignored on this path.
    token: str = ""


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


class AgentTokenPermissionsUpdate(StrictModel):
    permissions: list[str]


class AgentTokenExtend(StrictModel):
    extend_by_seconds: int


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
    # An Agent maintaining its own roster entry proves its identity here;
    # anything beyond that has to come through the management surface.
    actor_session_id: str = ""
    token: str = ""


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
    mcp_context: bool = False


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
    priority: int | None = None
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
    session_id: str | None = None
    token: str | None = None


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
    reclaim: bool = False
    takeover: bool = False


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
    assigned_to_member_id: str | None = None
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
    expected_version_id: str = ""


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

    def ensure_available(self, project_id: str, client_ip: str) -> None:
        """Raise 429 when a new stream would exceed limits, without taking a slot."""
        with self._guard:
            self._check_limits(project_id, client_ip)

    def _check_limits(self, project_id: str, client_ip: str) -> None:
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

    def acquire(self, project_id: str, client_ip: str) -> None:
        """Atomically reserve a slot before response headers (#181).

        Concurrent callers that both pass a dry-run check must not both receive
        HTTP 200; the loser gets 429 here. Release is guaranteed by the response
        wrapper even if the body generator never starts.
        """
        with self._guard:
            self._check_limits(project_id, client_ip)
            self._project_counts[project_id] = (
                self._project_counts.get(project_id, 0) + 1
            )
            self._ip_counts[client_ip] = self._ip_counts.get(client_ip, 0) + 1

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


class LoginRateLimiter:
    """In-process failure throttle for management login (IP + global keys)."""

    def __init__(
        self,
        *,
        max_failures: int,
        window_seconds: float,
        lockout_seconds: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._now = now or time.time
        self._guard = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        stamps = [
            stamp
            for stamp in self._failures.get(key, ())
            if 0.0 <= now - stamp < self.window_seconds
        ]
        if stamps:
            self._failures[key] = stamps
        else:
            self._failures.pop(key, None)
        return stamps

    def retry_after(self, key: str) -> float | None:
        """Seconds until this key may attempt login again, or None if allowed."""
        now = self._now()
        with self._guard:
            locked_until = self._locked_until.get(key, 0.0)
            if locked_until > now:
                return locked_until - now
            if locked_until:
                self._locked_until.pop(key, None)
                self._failures.pop(key, None)
            stamps = self._prune(key, now)
            if len(stamps) >= self.max_failures:
                lock_until = stamps[-1] + self.lockout_seconds
                if lock_until > now:
                    self._locked_until[key] = lock_until
                    return lock_until - now
                self._failures.pop(key, None)
            return None

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._guard:
            stamps = self._prune(key, now)
            stamps.append(now)
            self._failures[key] = stamps
            if len(stamps) >= self.max_failures:
                self._locked_until[key] = now + self.lockout_seconds

    def record_success(self, key: str) -> None:
        with self._guard:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


def upsert_toml_section(text: str, section: str, values: dict[str, Any]) -> str:
    """Insert or replace key/value pairs inside one ``[section]`` block.

    Only bool/int/str scalars are produced by the management settings writer;
    unknown sections and unrelated content are preserved verbatim.
    """
    if not values:
        return text
    lines = text.splitlines()
    header = f"[{section}]"
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)

    def _render_value(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, str):
            # Quote str so spaces/中文/quotes/newlines stay valid TOML.
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    rendered = [f"{key} = {_render_value(value)}" for key, value in values.items()]
    if start is None:
        block = [header, *rendered]
        prefix = "\n".join(lines) + "\n" if lines else ""
        return prefix + "\n".join(block) + "\n"
    end = start + 1
    while end < len(lines) and not (lines[end].startswith("[") and lines[end].rstrip().endswith("]")):
        end += 1
    body = lines[start + 1 : end]
    kept = [
        line
        for line in body
        if line.strip() and not line.split("=")[0].strip() in {key for key in values}
    ]
    return "\n".join([*lines[: start + 1], *kept, *rendered, *lines[end :]]) + "\n"


def run_auto_backup_cycle(service: Any, max_kept: int | None = None) -> dict[str, Any] | None:
    """Run one automatic-backup cycle; failures are logged, never raised."""
    try:
        return service.create_backup(source="auto", max_kept=max_kept)
    except Exception:
        logging.getLogger(__name__).exception("automatic backup failed")
        return None


def effective_backup_settings(
    settings: Any, overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Runtime backup settings: management overrides over loaded config.

    The overrides belong to one application instance. They used to live in a
    module-level dict, so a second ``create_app`` in the same process - a test,
    a GUI window, a management call - silently changed the first one's backup
    worker.
    """
    values = overrides or {}
    return {
        "auto_backup_enabled": values.get(
            "auto_backup_enabled", settings.auto_backup_enabled
        ),
        "auto_backup_interval_seconds": values.get(
            "auto_backup_interval_seconds", settings.auto_backup_interval_seconds
        ),
        "auto_backup_max_kept": values.get(
            "auto_backup_max_kept", settings.auto_backup_max_kept
        ),
    }


def auto_backup_worker(
    service: Any,
    settings: Any,
    stop_event: "threading.Event",
    overrides: Mapping[str, Any] | None = None,
    *,
    reschedule: "threading.Event | None" = None,
) -> None:
    """Backup loop: re-reads effective settings every cycle, so management
    toggles (enable / interval / retention) apply without a restart.

    A management save signals ``reschedule`` so a shortened interval takes
    effect on the next tick instead of after the old, longer one expires. The
    wait is split into short slices only when such a signal exists to observe:
    a single ``wait(timeout=interval)`` evaluates its timeout once, on entry,
    which is exactly why lowering the interval appeared to do nothing for up to
    a full period. Without a reschedule signal there is nothing to poll, so the
    wait stays whole.
    """
    while True:
        state = effective_backup_settings(settings, overrides)
        interval = max(60, int(state["auto_backup_interval_seconds"]))
        if reschedule is None:
            if stop_event.wait(timeout=interval):
                return
        elif _wait_for_backup_tick(stop_event, reschedule, interval):
            return
        state = effective_backup_settings(settings, overrides)
        if not state["auto_backup_enabled"]:
            continue
        run_auto_backup_cycle(service, state["auto_backup_max_kept"])


def _wait_for_backup_tick(
    stop_event: "threading.Event",
    reschedule: "threading.Event",
    interval_seconds: int,
) -> bool:
    """Sleep out one backup period in slices; True when the worker must stop.

    The deadline is recomputed from the wall clock on every slice so a signal
    that arrives part-way through does not extend the period, and a settings
    change that lands while the worker is running a backup is still observed
    before the next wait begins.
    """
    deadline = time.monotonic() + interval_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if stop_event.wait(timeout=min(remaining, BACKUP_TICK_SLICE_SECONDS)):
            return True
        if reschedule.is_set():
            reschedule.clear()
            return False


def start_auto_backup_worker(
    service: Any,
    settings: Any,
    stop_event: "threading.Event",
    overrides: dict[str, Any] | None = None,
    *,
    reschedule: "threading.Event | None" = None,
) -> "threading.Thread":
    """Start the daemon backup worker; per-cycle gating happens in the loop."""
    # functools.partial rather than a keyword on Thread: the reschedule signal
    # belongs to the worker's own signature, and Thread takes no such keyword.
    target = auto_backup_worker
    if reschedule is not None:
        target = partial(auto_backup_worker, reschedule=reschedule)
    worker = threading.Thread(
        target=target,
        args=(service, settings, stop_event, overrides),
        name="agentchatroom-auto-backup",
        daemon=True,
    )
    worker.start()
    return worker


def create_app(
    settings: Settings | None = None,
    *,
    directory_picker: Callable[[str], str | None] | None = None,
    local_mcp_configurator: LocalMcpConfigurator | None = None,
) -> FastAPI:
    # sse_starlette latches a global "should exit" flag the first time one of
    # its watchers observes any uvicorn server shutting down, and never clears
    # it. In a long-lived process that serves, stops and serves again (desktop
    # GUI restart, test suite), the stale latch instantly cancels every later
    # SSE response, so each new application resets it.
    try:
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit = False
    except ImportError:  # pragma: no cover - sse_starlette ships with the MCP SDK
        pass
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
    base_url = resolved.external_base_url or build_service_base_url(
        resolved.host, resolved.port
    )
    mcp_url = f"{base_url}{resolved.mcp_http_path}"
    http_presence_manager = LocalPresenceManager(
        service,
        enabled=resolved.mcp_http_enabled and resolved.presence_keepalive_enabled,
        interval_seconds=resolved.presence_keepalive_interval_seconds,
    )
    mcp_server = create_mcp(
        service,
        presence=http_presence_manager,
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
    if mcp_http_app is not None:
        # Stateful HTTP clients are expected to terminate their MCP Session.
        # Reap abandoned transports as a bounded fallback so their background
        # presence heartbeat cannot keep an old task owner online forever.
        mcp_server.session_manager.session_idle_timeout = (
            resolved.mcp_http_session_idle_timeout_seconds
        )
        http_presence_manager.transport_check = lambda key: http_transport_binding_alive(
            mcp_server.session_manager, key
        )
        # Task reclaim and lease takeover consult real transport state
        # (tombstones): presence heartbeats alone are display-only.
        service.transport_liveness_check = session_transport_state
        configure_transport_tombstones(
            limit=resolved.mcp_http_tombstone_limit,
            ttl_seconds=resolved.mcp_http_tombstone_ttl_seconds,
        )
        # The live binding table answers the same lifetime question as the
        # tombstone table, so it takes the same configured window: a client that
        # disconnects without a reap would otherwise leave one entry behind for
        # the whole process lifetime.
        configure_session_bindings(
            limit=resolved.mcp_http_tombstone_limit,
            ttl_seconds=resolved.mcp_http_tombstone_ttl_seconds,
        )
        # Tolerant adoption sits inside the #116 expiry contract: in the default
        # mode an unknown/reaped bookmark is adopted transparently (200, same
        # Room Session when the identity matches), while strict mode lets the
        # spec-compatible 404 plus mcp_session_expired contract through.
        def _adoption_binding(old_session_id: str, software_key: str):
            # The presence heartbeat normally retains tombstones while polling
            # transport liveness; retain here as well so adoption also works
            # when presence keepalive is disabled.
            retain_transport_tombstone(old_session_id)
            return lookup_transport_tombstone(old_session_id, software_key)

        mcp_http_app = with_expired_session_hints(
            with_session_adoption(
                mcp_http_app,
                session_manager=mcp_server.session_manager,
                enabled=resolved.mcp_http_session_adoption,
                is_active=lambda transport_id: transport_session_active(
                    mcp_server.session_manager, transport_id
                ),
                lookup_binding=_adoption_binding,
                adopt_binding=adopt_transport_binding,
                map_limit=resolved.mcp_http_tombstone_limit,
                map_ttl_seconds=resolved.mcp_http_tombstone_ttl_seconds,
            )
        )

    auto_backup_stop = threading.Event()
    # Signalled by the management save so a shortened interval is re-evaluated
    # immediately instead of after the period that was already in flight.
    auto_backup_reschedule = threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        event_loop = asyncio.get_running_loop()
        previous_exception_handler = event_loop.get_exception_handler()
        if os.name == "nt":
            event_loop.set_exception_handler(
                _windows_http_exception_handler(previous_exception_handler)
            )
        auto_backup_worker = start_auto_backup_worker(
            service,
            resolved,
            auto_backup_stop,
            backup_settings_override,
            reschedule=auto_backup_reschedule,
        )
        try:
            with running_service(resolved):
                http_presence_manager.start()
                if mcp_http_app is None:
                    yield
                else:
                    async with mcp_server.session_manager.run():
                        yield
        finally:
            http_presence_manager.stop()
            auto_backup_stop.set()
            if auto_backup_worker is not None:
                auto_backup_worker.join(timeout=5)
            service.close()
            if os.name == "nt":
                event_loop.set_exception_handler(previous_exception_handler)

    app = FastAPI(
        title=resolved.product_name,
        version=__version__,
        description="Local-first coordination service for heterogeneous coding agents",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.service = service
    app.state.mcp_server = mcp_server
    app.state.http_presence_manager = http_presence_manager
    app.state.directory_picker = resolved_directory_picker
    # Per-application backup overrides: the management writer and this app's own
    # worker share this dict and nothing else does.
    backup_settings_override: dict[str, Any] = {}
    app.state.backup_settings_override = backup_settings_override
    app.state.local_mcp_configurator = resolved_local_mcp_configurator

    def management_session_valid(session_id: str) -> bool:
        if not session_id:
            return False
        with service.database.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM management_sessions WHERE session_hash = ?",
                (_management_session_hash(session_id),),
            ).fetchone()
        return row is not None and float(row["expires_at"]) > time.time()

    def management_bearer_accepted(request: Request) -> bool:
        """The credential checks that need no database access."""
        if not resolved.management_auth_required:
            return True
        token = bearer_token(request.headers.get("authorization", ""))
        return bool(token) and hmac.compare_digest(token, management_token)

    def management_authenticated(request: Request) -> bool:
        """Synchronous check for the endpoints that already run off the loop."""
        if management_bearer_accepted(request):
            return True
        return management_session_valid(
            request.cookies.get(resolved.management_cookie_name, "")
        )

    async def management_authenticated_async(request: Request) -> bool:
        """Cookie authentication without a database read on the event loop.

        Every request carrying a management cookie passes through this check, so
        a synchronous query here serialises the whole ASGI application - including
        the SSE streams - behind one round trip (#271, same rule as the #184
        polling fix).
        """
        if management_bearer_accepted(request):
            return True
        session_id = request.cookies.get(resolved.management_cookie_name, "")
        if not session_id:
            return False
        return await asyncio.to_thread(management_session_valid, session_id)

    def browser_session_valid(session_id: str) -> bool:
        return management_session_valid(session_id)

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
        bearer = bearer_token(authorization)
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
        if not resolved.management_auth_required and _same_origin_browser_request(
            request
        ):
            # A local deployment is already trusted by the management middleware,
            # so an in-app browser stream needs no session row. The same-origin
            # check replaces the cookie that GET /auth/status used to mint (#281):
            # a cross-site page driving the user's browser still cannot open a
            # stream, and no anonymous GET can grow management_sessions.
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
    login_rate_limiter = LoginRateLimiter(
        max_failures=resolved.management_login_max_failures,
        window_seconds=resolved.management_login_window_seconds,
        lockout_seconds=resolved.management_login_lockout_seconds,
    )

    app.add_middleware(
        ManagementAuthASGIMiddleware,
        authenticated=management_authenticated_async,
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

    @app.get("/api/v1/version")
    def server_version() -> dict[str, Any]:
        return {
            "version": __version__,
            "schema_version": DOMAIN_SCHEMA_VERSION,
            "product_name": resolved.product_name,
        }

    @app.get("/api/v1/auth/status")
    def management_auth_status(request: Request) -> JSONResponse:
        # Read-only: a GET must not hand out management sessions. Issuance lives
        # in the login flow, so an anonymous or cross-origin simple request can
        # no longer insert 12h management_sessions rows without bound (#281).
        return JSONResponse(
            {
                "required": resolved.management_auth_required,
                "authenticated": management_authenticated(request),
            }
        )

    @app.post("/api/v1/auth/login")
    def management_login(request: Request, body: AdminLogin) -> JSONResponse:
        if resolved.management_auth_required:
            client_ip = event_stream_client_ip(request)
            ip_key = f"ip:{client_ip}"
            for key in (ip_key, "global"):
                wait = login_rate_limiter.retry_after(key)
                if wait is not None:
                    retry_after = max(1, int(wait + 0.999))
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": {
                                "code": "management_login_rate_limited",
                                "message": "Too many failed management login attempts",
                                "details": {"retry_after": retry_after},
                            }
                        },
                        headers={"Retry-After": str(retry_after)},
                    )
            if not hmac.compare_digest(body.token, management_token):
                login_rate_limiter.record_failure(ip_key)
                login_rate_limiter.record_failure("global")
                logging.getLogger(__name__).warning(
                    "management login failed client_ip=%s", client_ip
                )
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
            login_rate_limiter.record_success(ip_key)
            login_rate_limiter.record_success("global")
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
            "mcp_http_session_idle_timeout_seconds": (
                resolved.mcp_http_session_idle_timeout_seconds
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
        log_size_bytes = 0
        log_truncated = False
        if log_path.is_file():
            try:
                log_size_bytes = log_path.stat().st_size
            except OSError:
                log_size_bytes = 0
            tail, log_truncated = tail_text_lines(log_path, lines)
            log_lines = [_redact_log_line(line) for line in tail]
        pid_path = resolved.data_dir / "server.pid"
        # A pid file is written by another process: it can be empty, half-written,
        # or hold bytes that are not ASCII. Decoding must never turn the management
        # view into a 500, and a non-numeric value is not a live pid.
        raw_pid = ""
        if pid_path.is_file():
            try:
                raw_pid = pid_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                raw_pid = ""
        pid = raw_pid if raw_pid.isdigit() else ""
        return {
            "settings": resolved.public_dict(),
            "paths": {
                "data_dir": str(resolved.data_dir),
                "config_path": str(resolved.config_path) if resolved.config_path else None,
                "log_path": str(log_path),
                "pid_path": str(pid_path),
            },
            "process": {"pid": pid, "managed": bool(pid)},
            "log": {
                "lines": log_lines,
                "returned": len(log_lines),
                "size_bytes": log_size_bytes,
                "truncated": log_truncated,
            },
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

    def _require_management(request: Request) -> None:
        if not management_authenticated(request):
            raise DomainError(
                "management_auth_required",
                "Management authentication is required for this endpoint",
                status_code=401,
            )

    @app.get("/api/v1/admin/backups")
    def list_managed_backups(request: Request) -> dict[str, Any]:
        _require_management(request)
        effective = effective_backup_settings(resolved, backup_settings_override)
        return {
            "backups": service.list_backups(),
            "last_failure": service.latest_backup_failure(),
            "auto_backup": {
                "enabled": effective["auto_backup_enabled"],
                "interval_seconds": effective["auto_backup_interval_seconds"],
                "max_kept": effective["auto_backup_max_kept"],
            },
        }

    @app.post("/api/v1/admin/backups")
    def create_managed_backup(request: Request) -> dict[str, Any]:
        _require_management(request)
        effective = effective_backup_settings(resolved, backup_settings_override)
        return service.create_backup(
            source="management",
            max_kept=int(effective["auto_backup_max_kept"]),
        )

    @app.post("/api/v1/admin/backups/restore")
    def restore_managed_backup(request: Request, body: BackupRestoreRequest) -> dict[str, Any]:
        _require_management(request)
        return service.restore_backup(
            body.backup_path,
            confirm=body.confirm,
            allow_data_loss=body.allow_data_loss,
        )

    @app.delete("/api/v1/admin/backups/{filename}")
    def delete_managed_backup(request: Request, filename: str) -> dict[str, Any]:
        _require_management(request)
        return service.delete_backup(filename, actor="management")

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
        validate_project_coordination_instructions(body.root_path)
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
            # Which checkouts this Project may own is a domain decision; Session
            # worktrees are caller-reported strings and must not widen the set.
            cleanup_scope = service.checkout_cleanup_scope(project_id)
            checkout_paths = list(cleanup_scope["paths"])
            deleted = service.delete_project(project_id)
            removed = 0
            instructions_removed = 0
            registration_cleanup_errors: list[dict[str, Any]] = []
            instruction_cleanup_errors: list[dict[str, Any]] = []
            for checkout_path in sorted(checkout_paths):
                try:
                    removed += int(
                        remove_checkout_project_registration(
                            checkout_path,
                            project_key=str(cleanup_scope["project_key"]),
                            logical_path=str(cleanup_scope["logical_path"]),
                        )
                    )
                except DomainError as error:
                    registration_cleanup_errors.append(_cleanup_failure(error))
                try:
                    instructions_removed += int(
                        remove_project_coordination_instructions(checkout_path)
                    )
                except DomainError as error:
                    instruction_cleanup_errors.append(_cleanup_failure(error))
            deleted["project_registration"] = {
                "removed": removed,
                "cleanup_errors": registration_cleanup_errors,
            }
            deleted["project_instructions"] = {
                "removed": instructions_removed,
                "cleanup_errors": instruction_cleanup_errors,
            }
            return deleted
        return service.archive_project(project_id)

    @app.patch("/api/v1/projects/{project_id}")
    def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
        if body.name is not None:
            current = service.get_project(project_id)
            validate_project_coordination_instructions(current["root_path"])
        result = service.update_project(project_id, **body.model_dump())
        if body.name is not None:
            project = result["project"]
            result["project_instructions"] = write_project_coordination_instructions(
                project["root_path"],
                project,
            )
        return result

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
    def project_snapshot(
        project_id: str,
        response: Response,
        reports_before: str | None = Query(None),
        reports_after: str | None = Query(None),
        reviews_before: str | None = Query(None),
        reviews_after: str | None = Query(None),
        acknowledgements_before: str | None = Query(None),
        acknowledgements_after: str | None = Query(None),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return service.snapshot(
            project_id,
            reports_before=reports_before,
            reports_after=reports_after,
            reviews_before=reviews_before,
            reviews_after=reviews_after,
            acknowledgements_before=acknowledgements_before,
            acknowledgements_after=acknowledgements_after,
        )

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
        _reject_credentials_in_query(request)
        # The roster carries durable identities and this model can carry no
        # Session credentials, so an unauthenticated caller would otherwise be
        # able to mint a member - including one in the reserved namespace.
        _require_management(request)
        return service.create_project_member(
            project_id,
            **body.model_dump(),
            management_authorized=True,
            request_id=request.state.idempotency_request_id,
        )

    @app.patch("/api/v1/projects/{project_id}/members/{member_id}")
    def update_project_member(
        request: Request,
        project_id: str,
        member_id: str,
        body: ProjectMemberUpdate,
    ) -> dict[str, Any]:
        _reject_credentials_in_query(request)
        payload = body.model_dump(exclude_unset=True)
        # Roster writes change durable software identities: either the caller
        # proves it is that member, or it is a management request.
        management = not str(payload.get("actor_session_id") or "").strip()
        if management:
            _require_management(request)
        return service.update_project_member(
            project_id,
            member_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
        )

    @app.delete("/api/v1/projects/{project_id}/members/{member_id}")
    def revoke_project_member(
        request: Request,
        project_id: str,
        member_id: str,
    ) -> dict[str, Any]:
        _require_management(request)
        return service.revoke_project_member(
            project_id,
            member_id,
            management_authorized=True,
            request_id=request.state.idempotency_request_id,
        )

    @app.get("/api/v1/projects/{project_id}/events")
    def list_events(
        project_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return service.list_events(project_id, after=after, limit=limit)

    @app.get("/api/v1/projects/{project_id}/events/stream")
    def stream_events(
        request: Request,
        project_id: str,
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        # A synchronous route runs in Starlette's worker threadpool, so the
        # preamble below - project read, credential check - cannot put a database
        # round trip on the event loop while 64 reconnecting EventSources arrive
        # (#273, same rule as the #184 polling fix and #271 cookie auth).
        # Only the streaming generator itself belongs on the loop.
        service.get_project(project_id)
        authorize_event_stream(request, project_id)
        client_ip = event_stream_client_ip(request)
        # Atomic reserve before headers so a concurrent loser gets 429, not an
        # empty 200. Release runs from the response wrapper even when the body
        # generator never starts (client disconnect before first byte).
        sse_limiter.acquire(project_id, client_ip)
        header_cursor = request.headers.get("last-event-id", "")
        if header_cursor.isdigit():
            after = max(after, int(header_cursor))

        released = False

        def release_slot() -> None:
            nonlocal released
            if not released:
                released = True
                sse_limiter.release(project_id, client_ip)

        async def event_stream():
            cursor = after
            idle_ticks = 0
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    # Sync SQLite must not run on the event loop (#184).
                    result = await asyncio.to_thread(
                        service.list_events, project_id, after=cursor, limit=200
                    )
                    if result["events"]:
                        for event in result["events"]:
                            # SSE id / Last-Event-ID use project_seq (same space as ?after=).
                            cursor = event["project_seq"]
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
                release_slot()

        class _EnsureCloseStreamingResponse(StreamingResponse):
            """Always aclose the body iterator so generator finally runs, then
            release the pre-header reservation if the generator never started."""

            async def stream_response(self, send) -> None:  # type: ignore[override]
                try:
                    await super().stream_response(send)
                finally:
                    aclose = getattr(self.body_iterator, "aclose", None)
                    if aclose is not None:
                        await aclose()
                    release_slot()

        return _EnsureCloseStreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/projects/{project_id}/agents/join", status_code=201)
    def join_agent(request: Request, project_id: str, body: AgentJoin) -> dict[str, Any]:
        # Creating a Session is how a software identity enters the Room.
        # Without a credential, this stays management-only: an unauthenticated
        # caller must not name somebody else's identity by choosing `client`
        # or `member_id`. A presented project credential is authenticated
        # here, and that credential — not the body — decides the member.
        payload = body.model_dump()
        presented = str(payload.pop("token", "") or "").strip()
        if presented:
            credential = service.authenticate_agent_token(
                presented, project_id=project_id
            )
            payload["credential_id"] = str(credential["id"])
            linked_member = str(credential.get("member_id") or "").strip()
            payload["member_id"] = linked_member or None
            return service.join_room(
                project_id, manage_member_reference=False, **payload
            )
        _require_management(request)
        return service.join_room(
            project_id, manage_member_reference=True, **payload
        )

    @app.get("/api/v1/projects/{project_id}/agent-tokens")
    def list_agent_tokens(request: Request, project_id: str) -> dict[str, Any]:
        # The listing is a credential inventory: it names every Token in the
        # Project and who it belongs to. Metadata is a manifest too, so it is
        # not readable anonymously.
        _require_management(request)
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

    @app.patch(
        "/api/v1/projects/{project_id}/agent-tokens/{credential_id}/permissions"
    )
    def update_agent_token_permissions(
        project_id: str,
        credential_id: str,
        body: AgentTokenPermissionsUpdate,
    ) -> dict[str, Any]:
        return service.update_agent_token_permissions(
            project_id,
            credential_id,
            permissions=body.permissions,
        )

    @app.post(
        "/api/v1/projects/{project_id}/agent-tokens/{credential_id}/extend"
    )
    def extend_agent_token(
        project_id: str,
        credential_id: str,
        body: AgentTokenExtend,
    ) -> dict[str, Any]:
        return service.extend_agent_token(
            project_id,
            credential_id,
            extend_by_seconds=body.extend_by_seconds,
        )

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
        project_id: str, body: WorkspaceRegister, request: Request
    ) -> dict[str, Any]:
        # The hosts table is shared by every Project, so renaming an existing
        # host is a Room-wide change: only an authenticated management surface
        # may ask for it. Anyone else refreshes last_seen_at and nothing else.
        _require_management(request)
        return service.register_workspace(
            project_id,
            **body.model_dump(),
            host_updates_allowed=True,
        )

    def _backup_settings_payload() -> dict[str, Any]:
        effective = effective_backup_settings(resolved, backup_settings_override)
        return {
            "auto_backup_enabled": effective["auto_backup_enabled"],
            "auto_backup_interval_seconds": effective["auto_backup_interval_seconds"],
            "auto_backup_max_kept": effective["auto_backup_max_kept"],
            "config_path": str(
                resolved.config_path
                if resolved.config_path
                else Path(resolved.data_dir) / "config.toml"
            ),
            "effective": "保存即写入配置文件 [backup]（与自动备份调度共用同一来源）；调度线程收到信号后按新间隔重新计时，无需重启服务",
        }

    @app.get("/api/v1/admin/backup-settings")
    def get_backup_settings(request: Request) -> dict[str, Any]:
        _require_management(request)
        return _backup_settings_payload()

    @app.put("/api/v1/admin/backup-settings")
    def put_backup_settings(request: Request, body: BackupSettingsUpdate) -> dict[str, Any]:
        _require_management(request)
        if body.auto_backup_interval_seconds is not None and body.auto_backup_interval_seconds < 60:
            raise DomainError(
                "invalid_backup_settings",
                "auto_backup_interval_seconds must be at least 60",
                status_code=422,
            )
        if body.auto_backup_max_kept is not None and body.auto_backup_max_kept < 1:
            raise DomainError(
                "invalid_backup_settings",
                "auto_backup_max_kept must be at least 1",
                status_code=422,
            )
        updates = {
            "auto_backup_enabled": body.auto_backup_enabled,
            "auto_backup_interval_seconds": body.auto_backup_interval_seconds,
            "auto_backup_max_kept": body.auto_backup_max_kept,
        }
        target = (
            Path(resolved.config_path)
            if resolved.config_path
            else Path(resolved.data_dir) / "config.toml"
        )
        applied = {key: value for key, value in updates.items() if value is not None}
        rewrite_config_text(
            target,
            lambda current: upsert_toml_section(current, "backup", applied),
        )
        backup_settings_override.update(applied)
        # Wake the scheduler so a shortened interval takes effect now rather
        # than after the old period; the retention prune below already applies
        # its new cap immediately.
        auto_backup_reschedule.set()
        payload = _backup_settings_payload()
        # 保留份数立即生效：保存即按新上限清理现存备份，无需等下一次备份。
        payload["pruned"] = service.enforce_backup_retention(
            payload["auto_backup_max_kept"]
        )
        return payload

    @app.get("/api/v1/projects/{project_id}/documents")
    def list_documents(project_id: str, include_archived: bool = False) -> dict[str, Any]:
        return service.list_project_documents(project_id, include_archived=include_archived)

    @app.get("/api/v1/projects/{project_id}/documents/{doc_key}")
    def get_document(
        project_id: str,
        doc_key: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        return service.get_project_document(project_id, doc_key, version=version)

    @app.post("/api/v1/projects/{project_id}/documents")
    def upsert_document(
        project_id: str, body: ProjectDocumentUpsert, request: Request
    ) -> dict[str, Any]:
        _require_management(request)
        return service.upsert_project_document(
            project_id,
            doc_key=body.doc_key,
            kind=body.kind,
            title=body.title,
            content=body.content,
            actor="management",
        )

    @app.post("/api/v1/projects/{project_id}/documents/{doc_key}/archive")
    def archive_document(
        project_id: str, doc_key: str, request: Request
    ) -> dict[str, Any]:
        _require_management(request)
        return service.archive_project_document(project_id, doc_key, actor="management")

    @app.get("/api/v1/projects/{project_id}/audit")
    def query_audit(
        project_id: str,
        after: int = Query(0, ge=0),
        before: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        event_type: str | None = None,
        actor_session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return service.query_audit(
            project_id,
            after=after,
            before=before,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
        )

    @app.get("/api/v1/projects/{project_id}/tasks")
    def list_tasks(
        project_id: str,
        response: Response,
        status: str | None = None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return {
            "tasks": service.list_tasks(project_id, status=status, phase=phase)
        }

    @app.get("/api/v1/projects/{project_id}/tasks/by-number/{task_number}")
    def get_task_by_number(project_id: str, task_number: int) -> dict[str, Any]:
        return {"task": service.get_task_by_number(project_id, task_number)}

    @app.get("/api/v1/projects/{project_id}/tasks/{task_id}")
    def get_task(project_id: str, task_id: str) -> dict[str, Any]:
        task = service.get_task(project_id, task_id)
        # Additive injection keeps the historical {"task": ...} response intact
        # while surfacing current binding documents for the caller.
        task["project_documents"] = service.injectable_project_documents(project_id)
        return {"task": task}

    @app.get("/api/v1/projects/{project_id}/tasks/{task_id}/history")
    def list_task_history(
        project_id: str,
        task_id: str,
        after: int = Query(0, ge=0),
        before: int = Query(0, ge=0),
        cursor: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        event_type: str | None = None,
    ) -> dict[str, Any]:
        return service.list_task_history(
            project_id,
            task_id,
            after=after,
            before=before,
            cursor=cursor,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
        )

    @app.post("/api/v1/projects/{project_id}/task-intakes/{intake_id}/reassign")
    def reassign_task_intake(
        request: Request,
        project_id: str,
        intake_id: str,
        body: TaskIntakeReassign,
    ) -> dict[str, Any]:
        _reject_credentials_in_query(request)
        payload = body.model_dump()
        # No Session credentials is a management surface, so it must clear the
        # management authentication; an anonymous request is not authority.
        management = not str(payload.get("session_id") or "").strip()
        if management:
            _require_management(request)
        return service.reassign_task_intake(
            project_id,
            intake_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
        )

    @app.post("/api/v1/projects/{project_id}/tasks", status_code=201)
    def create_task(
        request: Request, project_id: str, body: TaskCreate
    ) -> dict[str, Any]:
        return service.create_task(
            project_id,
            **body.model_dump(),
            request_id=request.state.idempotency_request_id,
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
            reclaim=body.reclaim,
            takeover=body.takeover,
            request_id=request.state.idempotency_request_id,
        )

    @app.post("/api/v1/projects/{project_id}/tasks/{task_id}/release")
    def release_task(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskRelease,
    ) -> dict[str, Any]:
        _reject_credentials_in_query(request)
        payload = body.model_dump()
        # 无 Session 凭据 = 管理侧代为释放，必须显式通过管理认证；
        # 缺少凭据不构成管理权限（域层同样拒绝未标记的调用）。
        management = not str(payload.get("session_id") or "").strip()
        if management:
            _require_management(request)
        return service.release_task(
            project_id,
            task_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
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
        payload = body.model_dump()
        management = not str(payload.get("assigned_by_session_id") or "").strip()
        if management:
            _require_management(request)
        return service.assign_task(
            project_id,
            task_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
        _reject_credentials_in_query(request)
        payload = body.model_dump()
        # Missing Session credentials are not management authority, and they are
        # certainly not the task owner either: handoff must pass the same gate
        # its sibling writes (release/assign/update) pass.
        management = not str(payload.get("from_session_id") or "").strip()
        if management:
            _require_management(request)
        return service.handoff_task(
            project_id,
            task_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
        )

    @app.patch("/api/v1/projects/{project_id}/tasks/{task_id}")
    def update_task(
        request: Request,
        project_id: str,
        task_id: str,
        body: TaskUpdate,
    ) -> dict[str, Any]:
        payload = body.model_dump()
        # 无 Session 凭据的字段/状态更新（含 status=todo 兼容释放）属于管理操作。
        management = not str(payload.get("session_id") or "").strip()
        if management:
            _require_management(request)
        return service.update_task(
            project_id,
            task_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
        _reject_credentials_in_query(request)
        payload = body.model_dump()
        # No integrator credentials is not the same thing as management
        # authority: the terminal integration state must say who wrote it.
        management = not str(payload.get("integrator_session_id") or "").strip()
        if management:
            _require_management(request)
        return service.submit_integration(
            project_id,
            task_id,
            **payload,
            management_authorized=management,
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
            request_id=request.state.idempotency_request_id,
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
        # A terminal write. The domain requires the submitting identity or
        # a management session, so the flag is passed as a fact rather than
        # enforced here: an author is allowed without management auth, and
        # _require_knowledge_final_write_authority turns down anyone else.
        return service.supersede_knowledge_asset(
            project_id,
            asset_id,
            **body.model_dump(),
            management_authorized=management_authenticated(request),
            request_id=request.state.idempotency_request_id,
        )

    @app.post("/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/archive")
    def knowledge_archive(
        request: Request,
        project_id: str,
        asset_id: str,
        body: KnowledgeAssetAction,
    ) -> dict[str, Any]:
        # A terminal write. The domain requires the submitting identity or
        # a management session, so the flag is passed as a fact rather than
        # enforced here: an author is allowed without management auth, and
        # _require_knowledge_final_write_authority turns down anyone else.
        return service.archive_knowledge_asset(
            project_id,
            asset_id,
            **body.model_dump(),
            management_authorized=management_authenticated(request),
            request_id=request.state.idempotency_request_id,
        )

    web_dir = Path(__file__).with_name("web")
    if web_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

        # #151：index.html 里的 ?v= 参数按资产内容 sha256 自动派生。
        # 固定版本串会在前端更新后让浏览器长期命中旧缓存（极端情况：
        # 编辑中途短暂损坏的 app.js 被缓存后无法自愈，页面表现为
        # 「数据全没了」）。指纹随资产内容变化自动更新，无需人工 bump。
        # 验收退回修正：favicon 使用自身文件内容的指纹，而非复用 js/css 指纹。
        asset_fingerprint_cache: dict[str, tuple[tuple[int, int], str]] = {}
        fingerprint_assets = ("app.js", "app.css", "favicon.svg")
        # One slot, not a dict keyed by mtimes: every distinct mtime combination
        # used to add an entry that nothing ever evicted, so a long-lived server
        # that touched index.html or an asset often enough kept every rendered
        # copy of the page in memory (#282).
        index_html_cache: tuple[tuple[int, ...], bytes] | None = None

        def _asset_fingerprint(name: str) -> str:
            asset = web_dir / name
            stat = asset.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            cached = asset_fingerprint_cache.get(name)
            if cached is None or cached[0] != key:
                cached = (key, hashlib.sha256(asset.read_bytes()).hexdigest()[:10])
                asset_fingerprint_cache[name] = cached
            return cached[1]

        @app.get("/", include_in_schema=False)
        def web_index() -> Response:
            nonlocal index_html_cache
            index_path = web_dir / "index.html"
            key = (
                index_path.stat().st_mtime_ns,
                *(
                    (web_dir / name).stat().st_mtime_ns
                    for name in fingerprint_assets
                ),
            )
            # Cache hits must reuse the stored document: computing ``html``
            # only inside the miss branch left it unbound on the second
            # request, turning every page refresh into a 500.
            if index_html_cache is None or index_html_cache[0] != key:
                html = index_path.read_bytes()
                for name in fingerprint_assets:
                    html = re.sub(
                        f"({re.escape(name)}\\?v=)[^\"?]*".encode(),
                        b"\\g<1>" + _asset_fingerprint(name).encode(),
                        html,
                    )
                index_html_cache = (key, html)
            else:
                html = index_html_cache[1]
            return Response(
                html,
                media_type="text/html",
                headers={"Cache-Control": "no-cache"},
            )

    if mcp_http_app is not None:
        app.mount("/", mcp_http_app, name="mcp")

    return app
