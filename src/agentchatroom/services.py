from __future__ import annotations

import atexit
import fnmatch
import hashlib
import hmac
import inspect
import json
import logging
import ntpath
import os
import posixpath
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from functools import wraps
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import Settings
from .backup import backup_postgresql, backup_sqlite, restore_postgresql
from .contracts import (
    ASSIGNMENT_RESPONSES,
    ASSIGNMENT_STATUSES,
    DOMAIN_SCHEMA_VERSION,
    HANDOFF_RESPONSES,
    HANDOFF_STATUSES,
    INTEGRATION_RESULTS,
    KNOWLEDGE_ASSET_STATUSES,
    KNOWLEDGE_ASSET_TRANSITIONS,
    KNOWLEDGE_OWNER_KINDS,
    KNOWLEDGE_REVIEW_VERDICTS,
    KNOWLEDGE_SCHEMA_VERSION,
    KNOWLEDGE_SOURCE_TYPES,
    LEGACY_TASK_STATUSES,
    LEGACY_TASK_TRANSITIONS,
    MCP_CONTEXT_EVENT_TYPES,
    MODEL_DISPLAY_NAME_MAX_LENGTH,
    PROJECT_MEMBER_SCHEMA_VERSION,
    PROJECT_MEMBER_STATUSES,
    TASK_EXECUTION_STATUSES,
    TASK_INTEGRATION_STATUSES,
    TASK_INTAKE_STATUSES,
    TASK_INTAKE_TRANSITIONS,
    TASK_RELEASE_COMPAT_REASON_CODE,
    TASK_RELEASE_EXECUTION_STATUSES,
    TASK_RELEASE_REASON_CODES,
    TASK_VERIFICATION_STATUSES,
    TASK_VIEW_ATTENTION_PHASES,
    TASK_VIEW_SCHEMA_VERSION,
    TASK_VIEW_UNCLASSIFIED_PHASE,
    task_contract,
    task_state_for_legacy_status,
    task_view,
)
from .database import SCHEMA_VERSION, DatabaseBackend
from .redaction import SENSITIVE_KEYS
from .errors import DomainError
from .project_registration import (
    detect_git_info,
    derive_logical_path,
    load_checkout_registration,
    stored_project_scope,
)
from .task_history import (
    TASK_HISTORY_LIMIT_MAX,
    TASK_HISTORY_SCHEMA_VERSION,
    actor_snapshot,
    project_history_item,
    redact_runtime_value,
)


TASK_STATUSES = LEGACY_TASK_STATUSES
TASK_TRANSITIONS = LEGACY_TASK_TRANSITIONS
AGENT_STATUSES = {"online", "idle", "working", "blocked", "offline"}
AGENT_ACTIVITY_PRIORITY = {"online": 0, "idle": 1, "working": 2, "blocked": 3}
LEASE_MODES = {"readonly", "shared", "exclusive"}
MESSAGE_KINDS = {"message", "decision", "blocker", "system"}
MESSAGE_CHANNELS = {"public", "task", "review", "system"}
REVIEW_VERDICTS = {"approved", "changes_requested"}
AGENT_PERMISSIONS = {
    "room:join",
    "room:read",
    "message:write",
    "task:write",
    "lease:write",
    "review:write",
    "knowledge:write",
    "integration:write",
    "audit:read",
    "member:read",
    "member:write",
    "document:write",
}
DEFAULT_AGENT_PERMISSIONS = AGENT_PERMISSIONS - {"audit:read"}
DEFAULT_CREDENTIAL_NAME_PATTERN = re.compile(
    # 仅识别向导真实派生的占位名（Agent 凭据/Token 系列与通用标准 MCP 标记），
    # 不再匹配任意「X · Y HTTP」——那会把用户自定义凭据静默改名（#165）。
    r"^(?:Agent\s*(?:凭据|Token)(?:\s+\d+)?|.*(?:通用（标准\s*MCP）|\bStandard\s+MCP\b).*)$",
    re.IGNORECASE,
)
PROJECT_SETTINGS_DEFAULTS: dict[str, Any] = {
    "lease_conflict_policy": "advisory",
    "roles": [],
    "extensions": {},
    "default_task_priority": 2,
    "audit_retention_days": 0,
    "mcp_message_limit": 5,
    "mcp_message_context_limit": 5,
}
SOFTWARE_MEMBER_PREFIX = "software:"
# One vocabulary for "this Project credential can no longer open a Session", so
# REST, MCP and CLI report the same failure instead of three different ones.
# The internal agent_token_* codes stay for token management operations.
PROJECT_CREDENTIAL_REVOKED = "project_credential_revoked"
PROJECT_CREDENTIAL_EXPIRED = "project_credential_expired"
PROJECT_CREDENTIAL_INVALID = "project_credential_invalid"
CREDENTIAL_FAILURE_CODES = {
    "agent_token_revoked": PROJECT_CREDENTIAL_REVOKED,
    "agent_token_expired": PROJECT_CREDENTIAL_EXPIRED,
    "agent_token_invalid": PROJECT_CREDENTIAL_INVALID,
}
CREDENTIAL_FAILURE_ACTIONS = {
    PROJECT_CREDENTIAL_REVOKED: "issue_new_project_token_and_update_bundle",
    PROJECT_CREDENTIAL_EXPIRED: "renew_project_token",
    PROJECT_CREDENTIAL_INVALID: "replace_project_token_in_bundle",
}
CREDENTIAL_FAILURE_MESSAGES = {
    PROJECT_CREDENTIAL_REVOKED: "The selected Project Token has been revoked",
    PROJECT_CREDENTIAL_EXPIRED: "The selected Project Token has expired",
    PROJECT_CREDENTIAL_INVALID: "The selected Project Token is invalid",
}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SENSITIVE_REQUEST_FIELDS = SENSITIVE_KEYS
logger = logging.getLogger(__name__)

_UNCLASSIFIED_TASK_VIEW_WARNED: set[str] = set()


def _management_authorization_required(action: str) -> DomainError:
    """Build the explicit-authority error for a management task mutation.

    A missing Session credential is not management authority. Adapters that
    expose a real management surface (authenticated management token, browser
    management session, or an equivalent local management command) must mark
    the call; anything else is rejected so a bare Session id or an anonymous
    request can never release, reassign, or rewrite another Session's task.
    """
    return DomainError(
        "management_auth_required",
        f"Owner credentials or an authenticated management request are required to {action}",
        status_code=401,
        details={
            "required_action": "authenticate_management_or_use_owner_credentials",
        },
    )


def _warn_unclassified_task_view(task: Mapping[str, Any]) -> None:
    """Alert once per task when the (E,V,I) triple maps to unclassified.

    Plan D requires legacy residue or unknown combinations to surface
    explicitly instead of being disguised as a normal phase.
    """
    task_id = str(task.get("id") or "")
    if task_id in _UNCLASSIFIED_TASK_VIEW_WARNED:
        return
    _UNCLASSIFIED_TASK_VIEW_WARNED.add(task_id)
    logger.warning(
        "task %s (#%s) has unclassified state view: execution=%r verification=%r "
        "integration=%r; extend TASK_VIEW projection in contracts.py",
        task_id,
        task.get("task_number"),
        task.get("execution_status"),
        task.get("verification_status"),
        task.get("integration_status"),
    )


def _is_unique_constraint_error(error: BaseException) -> bool:
    if isinstance(error, sqlite3.IntegrityError):
        return True
    name = error.__class__.__name__.lower()
    return "integrity" in name or "unique" in name


def _sql_placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


class TokenTouchBuffer:
    """Queue last_used_at updates so token verification stays on a read path."""

    def __init__(
        self,
        service: "AgentChatRoomService",
        *,
        interval_seconds: float,
        min_calls: int,
    ) -> None:
        self._service = service
        self._interval_seconds = max(1.0, float(interval_seconds))
        self._min_calls = max(1, int(min_calls))
        self._lock = threading.Lock()
        self._pending: dict[str, str] = {}
        self._calls_since_flush = 0
        self._last_flush = time.monotonic()
        self._closed = False
        self._wakeup = threading.Event()
        self._backoff_seconds = 0.05
        self._thread: threading.Thread | None = None
        atexit.register(self.flush)

    def note(self, credential_id: str) -> None:
        if not credential_id or self._closed:
            return
        used_at = iso_now()
        with self._lock:
            self._pending[credential_id] = used_at
            self._calls_since_flush += 1
            due = (
                self._calls_since_flush >= self._min_calls
                or (time.monotonic() - self._last_flush) >= self._interval_seconds
            )
            self._ensure_thread_locked()
        if due:
            self._wakeup.set()

    def flush(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = {}
            self._calls_since_flush = 0
            self._last_flush = time.monotonic()
        if not pending:
            return
        try:
            with self._service.database.connect(write=True) as connection:
                now = iso_now()
                for credential_id, used_at in pending.items():
                    connection.execute(
                        """
                        UPDATE agent_credentials
                        SET last_used_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (used_at or now, now, credential_id),
                    )
            self._backoff_seconds = 0.05
        except Exception:
            logger.exception("Failed to flush Agent token last_used_at updates")
            with self._lock:
                for credential_id, used_at in pending.items():
                    self._pending.setdefault(credential_id, used_at)
            self._backoff_seconds = min(self._backoff_seconds * 2, 5.0)
            if not self._closed:
                time.sleep(self._backoff_seconds)

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()
        self.flush()

    def _ensure_thread_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run,
                name="agentchatroom-token-touch",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._closed:
            self._wakeup.wait(timeout=self._interval_seconds)
            self._wakeup.clear()
            if self._closed:
                break
            self.flush()


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)


def request_payload_for_hash(value: Any, *, field_name: str = "") -> Any:
    if field_name.lower() in SENSITIVE_REQUEST_FIELDS:
        encoded = str(value or "").encode()
        return {"sha256": hashlib.sha256(encoded).hexdigest()}
    if isinstance(value, dict):
        return {
            str(key): request_payload_for_hash(item, field_name=str(key))
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, (list, tuple)):
        return [request_payload_for_hash(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def idempotent_write(operation: str):
    def decorate(callable_):
        call_signature = inspect.signature(callable_)

        @wraps(callable_)
        def wrapped(self, *args, **kwargs):
            request_id = kwargs.pop("request_id", None)
            if request_id is None:
                return callable_(self, *args, **kwargs)
            bound = call_signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            project_id = str(bound.arguments.get("project_id", ""))
            payload = {
                key: request_payload_for_hash(value, field_name=key)
                for key, value in bound.arguments.items()
                if key != "self"
            }
            return self._execute_idempotent(
                project_id=project_id,
                request_id=str(request_id),
                operation=operation,
                request_payload=payload,
                action=lambda: callable_(self, *args, **kwargs),
            )

        return wrapped

    return decorate


def row_dict(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def normalize_project_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    values = dict(settings or {})
    unknown = sorted(set(values) - set(PROJECT_SETTINGS_DEFAULTS))
    if unknown:
        raise DomainError(
            "invalid_project_settings",
            "Unknown project settings must be placed under extensions",
            details={"unknown": unknown},
        )
    policy = values.get(
        "lease_conflict_policy",
        PROJECT_SETTINGS_DEFAULTS["lease_conflict_policy"],
    )
    if policy not in {"advisory", "pre_commit_block"}:
        raise DomainError(
            "invalid_project_settings",
            "lease_conflict_policy must be advisory or pre_commit_block",
        )
    default_priority = values.get(
        "default_task_priority",
        PROJECT_SETTINGS_DEFAULTS["default_task_priority"],
    )
    if not isinstance(default_priority, int) or isinstance(default_priority, bool) or not 0 <= default_priority <= 4:
        raise DomainError(
            "invalid_project_settings",
            "default_task_priority must be an integer between 0 and 4",
        )
    retention = values.get(
        "audit_retention_days",
        PROJECT_SETTINGS_DEFAULTS["audit_retention_days"],
    )
    if not isinstance(retention, int) or isinstance(retention, bool) or retention not in {0, 30, 90}:
        raise DomainError(
            "invalid_project_settings",
            "audit_retention_days must be 0 (permanent), 30, or 90",
        )
    roles = values.get("roles", PROJECT_SETTINGS_DEFAULTS["roles"])
    if not isinstance(roles, list) or any(
        not isinstance(role, str) or not role.strip() for role in roles
    ):
        raise DomainError(
            "invalid_project_settings",
            "roles must be a list of non-empty strings",
        )
    extensions = values.get("extensions", PROJECT_SETTINGS_DEFAULTS["extensions"])
    if not isinstance(extensions, dict):
        raise DomainError("invalid_project_settings", "extensions must be an object")
    if "mcp_message_limit" in values:
        mcp_limit = values["mcp_message_limit"]
    elif "mcp_message_context_limit" in values:
        mcp_limit = values["mcp_message_context_limit"]
    else:
        mcp_limit = PROJECT_SETTINGS_DEFAULTS["mcp_message_limit"]
    if (
        not isinstance(mcp_limit, int)
        or isinstance(mcp_limit, bool)
        or not 1 <= mcp_limit <= 10
    ):
        raise DomainError(
            "invalid_project_settings",
            "mcp_message_limit must be an integer between 1 and 10",
        )
    return {
        "lease_conflict_policy": policy,
        "roles": list(dict.fromkeys(role.strip() for role in roles)),
        "extensions": extensions,
        "default_task_priority": default_priority,
        "audit_retention_days": retention,
        "mcp_message_limit": mcp_limit,
        "mcp_message_context_limit": mcp_limit,
    }


def normalize_remote(remote: str) -> str:
    value = remote.strip().replace("\\", "/")
    value = re.sub(r"^git@([^:]+):", r"https://\1/", value)
    value = re.sub(r"\.git$", "", value)
    return value.rstrip("/").lower()


def normalize_pattern(pattern: str) -> str:
    """Fold every spelling of one scope onto the one stored pattern.

    A leading ``./`` was stripped only once and inner ``.`` segments survived, so
    ``././src`` and ``src``, or ``src/./a.py`` and ``src/a.py``, were different
    strings: two Agents could then hold exclusive leases on the same file with no
    conflict event, the pre-commit check saw a different scope than the one the
    other lease actually covered, and the owning Session's idempotent fast path
    created a second row instead of renewing the first.
    """
    value = pattern.strip().replace("\\", "/")
    segments = [segment for segment in value.split("/") if segment not in ("", ".")]
    if not segments or any(segment == ".." for segment in segments):
        raise DomainError(
            "invalid_path_pattern", "Path pattern must stay inside the project"
        )
    return "/".join(segments)


def normalize_evidence_path(value: Any) -> str:
    """Fold one changed-file claim without lease-glob semantics.

    `files` on a message, handoff, or integration is a claim about concrete
    files that a commit can prove. Reusing the lease pattern for it accepted
    absolute and drive-letter paths (which reach outside the Project and end up
    in exported audit data), and answered a blank entry with the lease error
    code `invalid_path_pattern` on a surface that never mentioned a lease.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise DomainError(
            "invalid_changed_file",
            "A changed file entry must not be empty",
            status_code=422,
            details={
                "required_action": "list_project_relative_file_paths_without_globs",
                "field": "files",
            },
        )
    if any(marker in text for marker in "*?[]"):
        raise DomainError(
            "invalid_changed_file",
            "A changed file must name one concrete path, not a glob pattern",
            status_code=422,
            details={
                "required_action": "list_each_changed_file_separately",
                "field": "files",
                "value": text,
            },
        )
    if text.startswith("/") or text.startswith("~") or re.match(r"^[A-Za-z]:", text):
        raise DomainError(
            "invalid_changed_file",
            "A changed file must be relative to the project root",
            status_code=422,
            details={
                "required_action": "strip_the_absolute_prefix_and_keep_the_project_relative_path",
                "field": "files",
                "value": text,
            },
        )
    segments = [segment for segment in text.split("/") if segment not in ("", ".")]
    if not segments or any(segment == ".." for segment in segments):
        raise DomainError(
            "invalid_changed_file",
            "A changed file must stay inside the project",
            status_code=422,
            details={
                "required_action": "keep_the_path_inside_the_project",
                "field": "files",
                "value": text,
            },
        )
    return "/".join(segments)


def lease_modes_conflict(left: str, right: str) -> bool:
    if "readonly" in {left, right}:
        return False
    return "exclusive" in {left, right}


def _pattern_covers(broad: str, narrow: str) -> bool:
    """True when every path inside ``narrow`` is also inside ``broad``.

    A same-identity takeover must only retire leases the new scope actually
    replaces. Releasing a broader lease because it merely *overlaps* a narrower
    request silently shrank the caller's own protection.
    """
    broad = normalize_pattern(broad)
    narrow = normalize_pattern(narrow)
    if broad == narrow:
        return True
    if fnmatch.fnmatchcase(narrow, broad):
        return True
    prefix = _static_prefix(broad)
    return bool(prefix) and (
        narrow == prefix or narrow.startswith(f"{prefix}/") or narrow.startswith(prefix)
    )


# How much protection each mode buys, so re-acquiring one scope with a different
# mode is an explicit upgrade or a refused downgrade rather than a silent no-op.
LEASE_MODE_STRENGTH = {"readonly": 0, "shared": 1, "exclusive": 2}


def _static_prefix(pattern: str) -> str:
    wildcard = min(
        [index for token in "*[?" if (index := pattern.find(token)) >= 0]
        or [len(pattern)]
    )
    return pattern[:wildcard].rstrip("/")


def _patterns_can_cover_same_path(left: str, right: str) -> bool:
    """Compare two scopes segment by segment instead of giving up.

    A pattern that begins with a wildcard has no static prefix, and the old code
    read that as "overlaps everything": one Agent holding ``*.md`` then blocked an
    unrelated ``src/app.py``, including under the pre-commit policy. Walking the
    segments answers the real question instead - can one path satisfy both.
    """
    for left_segment, right_segment in zip(left.split("/"), right.split("/")):
        if left_segment == "**" or right_segment == "**":
            continue
        if (
            left_segment == right_segment
            or fnmatch.fnmatchcase(left_segment, right_segment)
            or fnmatch.fnmatchcase(right_segment, left_segment)
        ):
            continue
        return False
    return True


def patterns_overlap(left: str, right: str) -> bool:
    left = normalize_pattern(left)
    right = normalize_pattern(right)
    if left == right or fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left):
        return True
    left_prefix = _static_prefix(left)
    right_prefix = _static_prefix(right)
    if not left_prefix or not right_prefix:
        # No static prefix only means the pattern starts with a wildcard; it does
        # not make it overlap every other scope in the project.
        return _patterns_can_cover_same_path(left, right)
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(f"{right_prefix}/")
        or right_prefix.startswith(f"{left_prefix}/")
    )


GIT_SCOPE_RECHECK_TTL_SECONDS = 30.0


def _project_git_info(root: Path) -> tuple[str, Path, bool]:
    # Single shared implementation with checkout registration: Git writes
    # UTF-8 path bytes, which must never be decoded with the process locale.
    return detect_git_info(root)


def _project_source_from_disk(remote: str, is_work_tree: bool) -> str:
    if remote:
        return "git_remote"
    if is_work_tree:
        return "git_local"
    return "path"


def _project_scope(
    remote: str, git_root: Path, logical_path: str
) -> tuple[str, str, str]:
    if remote:
        return ("git", normalize_remote(remote), logical_path.casefold())
    return ("path", os.path.normcase(str(git_root.resolve())), logical_path.casefold())


def _stored_project_scope(project: Mapping[str, Any]) -> tuple[str, str, str]:
    """The stored row's scope, from the same implementation the registration and
    bootstrap paths use, so no second copy can drift again."""
    try:
        scope = stored_project_scope(dict(project))
    except (OSError, ValueError):
        # A root_path that cannot be resolved (legacy, corrupt, or deliberately
        # malformed) owns no scope at all: it must never match a candidate and
        # must never turn an ordinary read into a 500.
        return ("unreadable", "", "")
    return (
        scope["kind"],
        scope["identity"],
        scope["logical_path"].casefold(),
    )


def run_git_capture(
    command: list[str], *, timeout_seconds: float = 5
) -> tuple[int, str, str]:
    """Run a git command and return ``(returncode, stdout, stderr)`` as text.

    ``text=True`` decodes with the process locale, which on Windows is the
    console code page — not the UTF-8 that git actually writes. A branch or
    path with non-ASCII characters then becomes mojibake, and because
    UnicodeDecodeError is a ValueError it slips past the ``OSError`` /
    ``SubprocessError`` handling, surfacing as a 500 or as corrupted bytes
    written into append-only evidence. Decoding explicitly keeps the result
    readable and the failure total.
    """
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return 1, "", ""
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace").strip(),
        completed.stderr.decode("utf-8", "replace").strip(),
    )


def is_strict_int(value: Any) -> bool:
    """True only for a genuine integer.

    ``bool`` is a subclass of ``int`` in Python, so a JSON ``false`` passes an
    ``isinstance(value, int)`` check and is then stored as exit code 0 — turning
    a failed check into a recorded pass inside append-only evidence that can
    never be rewritten. Adapters share this one predicate so REST, MCP and CLI
    cannot disagree about what an integer is.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def require_int_field(value: Any, field: str) -> Any:
    """Pass an integer-typed input through, refusing booleans.

    REST bodies arrive as untyped dictionaries, so ``priority: true`` reaches
    the domain as a bool and still satisfies a range check such as
    ``0 <= True <= 4``. Every integer-typed entry point routes through this so
    REST, MCP and CLI cannot disagree about what an integer is.
    """
    if value is None:
        return None
    if not is_strict_int(value):
        raise DomainError(
            f"invalid_{field}",
            f"{field.replace('_', ' ')} must be an integer",
            status_code=422,
            details={"field": field},
        )
    return int(value)


def credential_state_error(reason: str) -> DomainError:
    """Map a credential failure onto the shared Project-credential vocabulary.

    Adapters raise the internal ``agent_token_*`` codes from the domain; every
    caller that wants to tell an Agent what to do next uses this instead, so
    REST, MCP and CLI all return the same code, message and required_action.
    """
    code = CREDENTIAL_FAILURE_CODES.get(reason, PROJECT_CREDENTIAL_INVALID)
    return DomainError(
        code,
        CREDENTIAL_FAILURE_MESSAGES[code],
        status_code=401,
        details={"required_action": CREDENTIAL_FAILURE_ACTIONS[code]},
    )


def _require_live_credential(row: Mapping[str, Any]) -> None:
    """Refuse a credential that was revoked or has already expired.

    Opening a Session is exactly as privileged as using the credential, so the
    same liveness rule that guards an authenticated call must guard a join.
    """
    if row["revoked_at"] is not None:
        raise credential_state_error("agent_token_revoked")
    if parse_time(row["expires_at"]) <= utc_now():
        raise credential_state_error("agent_token_expired")


class AgentChatRoomService:
    def __init__(self, database: DatabaseBackend, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        # Monotonic deadlines, not wall-clock timestamps: an NTP correction or a
        # resumed VM can move time backwards and stall retention for the whole
        # process lifetime.
        self._retention_next_run: dict[str, float] = {}
        # Serializes create_backup / restore_backup (including auto_backup_worker)
        # so a backup cannot snapshot a half-restored SQLite file and restore
        # stale checks cannot race concurrent in-process backups.
        self._backup_restore_lock = threading.RLock()
        # Retention as last applied through management, overriding the value
        # captured in Settings at startup. Without this, saving a larger
        # retention count would be silently undone by the next manual backup,
        # which prunes with the stale startup number and deletes backups
        # irreversibly.
        self._applied_backup_max_kept: int | None = None
        # Wired by the API layer to the MCP transport state lookup: "alive",
        # "gone" (reaped transport, tombstone retained) or "unknown" for
        # sessions without an HTTP transport. Presence heartbeats are
        # display-only, but reclaim and lease takeover need the harder
        # "is the owning transport actually gone" signal.
        self.transport_liveness_check: Callable[[str, str], str] | None = None
        self._token_touch = TokenTouchBuffer(
            self,
            interval_seconds=settings.token_touch_interval_seconds,
            min_calls=settings.token_touch_min_calls,
        )
        # project_id -> (monotonic_ts, project_source, remote_on_disk)
        # Caps disk/git re-probes on read paths so GET without a candidate
        # change does not spawn git or open a write connection every time.
        self._git_scope_probe_cache: dict[str, tuple[float, str, str]] = {}

    def initialize(self) -> None:
        self.database.initialize()
        self._purge_stale_credentials()
        self.enforce_idempotency_retention(force=True)

    def _purge_stale_credentials(self) -> None:
        """#147：启动维护时清理已过期与已吊销的失效凭据。

        只删除未被任何 Session 引用的凭据行，维持外键完整与历史关联；
        审计事件与成员记录不受影响。
        """
        with self.database.connect(write=True) as connection:
            connection.execute(
                """
                DELETE FROM agent_credentials
                WHERE (revoked_at IS NOT NULL OR expires_at <= ?)
                  AND id NOT IN (
                      SELECT credential_id FROM agent_sessions
                      WHERE credential_id IS NOT NULL
                  )
                """,
                (iso_now(),),
            )

    def enforce_idempotency_retention(self, *, force: bool = False) -> dict[str, Any]:
        """Delete idempotency_records older than the configured retention window.

        0 keeps records forever. Within the window, replaying the same
        request_id still hits the stored response; after cleanup, an old
        request_id is treated as a new request.
        """
        retention_days = int(getattr(self.settings, "idempotency_retention_days", 30))
        if retention_days <= 0:
            return {"deleted": 0, "retention_days": 0}
        now = time.monotonic()
        if not force and now < self._retention_next_run.get("idempotency", 0.0):
            return {"deleted": 0, "retention_days": retention_days, "skipped": True}
        self._retention_next_run["idempotency"] = (
            now + self.retention_sweep_interval_seconds()
        )
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.database.connect(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM idempotency_records WHERE created_at < ?",
                (cutoff,),
            )
            deleted = max(0, int(cursor.rowcount))
        return {"deleted": deleted, "retention_days": retention_days, "cutoff": cutoff}

    @staticmethod
    def _require_list_input_limit(
        field: str,
        items: Any,
        *,
        limit: int,
        status_code: int = 422,
    ) -> list[Any]:
        materialized = list(items or [])
        if len(materialized) > limit:
            raise DomainError(
                "list_input_too_long",
                f"{field} exceeds list_input_max_count ({limit})",
                status_code=status_code,
                details={"field": field, "limit": limit, "count": len(materialized)},
            )
        return materialized

    @staticmethod
    def _review_criteria_map(criteria: list[Any]) -> dict[str, str]:
        """Criterion name -> verdict, with the shape every review surface accepts.

        Reviews are append-only evidence: a blank criterion or a status outside
        passed/failed would otherwise be permanently readable as an assessment.
        """
        assessed: dict[str, str] = {}
        for item in criteria:
            if not isinstance(item, Mapping):
                raise DomainError(
                    "invalid_review_criteria",
                    "Every review criterion must be an object with criterion/status",
                    status_code=422,
                )
            criterion = str(item.get("criterion", "")).strip()
            status = str(item.get("status", "")).strip()
            if not criterion or status not in {"passed", "failed"}:
                raise DomainError(
                    "invalid_review_criteria",
                    "Every review criterion requires a criterion and passed/failed status",
                    status_code=422,
                    details={"criterion": criterion, "status": status},
                )
            assessed[criterion] = status
        return assessed

    def _validate_test_evidence_list(
        self, tests: Any, *, field: str = "tests"
    ) -> list[dict[str, Any]]:
        materialized = self._require_list_input_limit(
            field,
            tests,
            limit=int(getattr(self.settings, "list_input_max_count", 100)),
        )
        allowed_keys = {"command", "exit_code", "notes"}
        notes_limit = int(getattr(self.settings, "test_entry_notes_max_length", 4000))
        cleaned: list[dict[str, Any]] = []
        for test in materialized:
            if not isinstance(test, dict):
                raise DomainError(
                    "invalid_test_evidence",
                    "Every test entry must be an object with command/exit_code/notes",
                    status_code=422,
                    details={"field": field},
                )
            unexpected = sorted(set(test) - allowed_keys)
            if unexpected:
                raise DomainError(
                    "invalid_test_evidence",
                    "Test entries only accept command, exit_code, and notes keys",
                    status_code=422,
                    details={"field": field, "unexpected_keys": unexpected},
                )
            if not str(test.get("command", "")).strip() or not is_strict_int(
                test.get("exit_code")
            ):
                raise DomainError(
                    "invalid_test_evidence",
                    "Every test entry requires a command and integer exit_code",
                    status_code=422,
                    details={"field": field},
                )
            notes = str(test.get("notes") or "")
            if len(notes) > notes_limit:
                raise DomainError(
                    "invalid_test_evidence",
                    f"Test notes exceeds test_entry_notes_max_length ({notes_limit})",
                    status_code=422,
                    details={
                        "field": field,
                        "limit": notes_limit,
                        "notes_length": len(notes),
                    },
                )
            cleaned.append(
                {
                    "command": str(test.get("command")),
                    "exit_code": int(test.get("exit_code")),
                    "notes": notes,
                }
            )
        return cleaned

    def close(self) -> None:
        self._token_touch.close()
        self.database.close()

    def _execute_idempotent(
        self,
        *,
        project_id: str,
        request_id: str,
        operation: str,
        request_payload: dict[str, Any],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        clean_request_id = request_id.strip()
        if not REQUEST_ID_PATTERN.fullmatch(clean_request_id):
            raise DomainError(
                "invalid_request_id",
                "Request ID must be 1-128 safe ASCII characters",
            )
        canonical_request = json.dumps(
            {"operation": operation, "payload": request_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
        scope = f"project:{project_id}"
        # The reservation is deliberately its own short transaction. The action
        # can run git subprocesses and touch disk, so holding the database write
        # lock across it stalls every other writer — and freezes the
        # Streamable-HTTP event loop — for as long as that work takes.
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            existing = connection.execute(
                """
                SELECT operation, request_hash, response_json
                FROM idempotency_records
                WHERE scope = ? AND request_id = ?
                """,
                (scope, clean_request_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["operation"] != operation
                    or existing["request_hash"] != request_hash
                ):
                    raise DomainError(
                        "idempotency_conflict",
                        "Request ID was already used for different input",
                        status_code=409,
                        details={"request_id": clean_request_id},
                    )
                if not existing["response_json"]:
                    raise DomainError(
                        "idempotency_in_progress",
                        "The original request is still being processed",
                        status_code=409,
                        details={"request_id": clean_request_id},
                    )
                replay = json_load(existing["response_json"], {})
                return {
                    **replay,
                    "request_id": clean_request_id,
                    "idempotent_replay": True,
                }

            now = iso_now()
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    scope, request_id, operation, request_hash, response_json,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL)
                """,
                (scope, clean_request_id, operation, request_hash, now),
            )

        try:
            result = action()
            if not isinstance(result, dict):
                raise RuntimeError("Idempotent write results must be objects")
        except BaseException:
            # The action commits on its own now, so its failure can no longer
            # roll the reservation back. Drop the unfinished record instead:
            # leaving it behind would pin the request id to "in progress"
            # forever and block every corrected retry.
            self._release_idempotency_reservation(scope, clean_request_id)
            raise

        with self.database.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE idempotency_records
                SET response_json = ?, completed_at = ?
                WHERE scope = ? AND request_id = ?
                """,
                (json_dump(result), iso_now(), scope, clean_request_id),
            )
        return {
            **result,
            "request_id": clean_request_id,
            "idempotent_replay": False,
        }

    def _release_idempotency_reservation(self, scope: str, request_id: str) -> None:
        try:
            with self.database.connect(write=True) as connection:
                connection.execute(
                    """
                    DELETE FROM idempotency_records
                    WHERE scope = ? AND request_id = ? AND response_json IS NULL
                    """,
                    (scope, request_id),
                )
        except Exception:
            # Cleanup must never mask the failure that triggered it.
            pass

    def _require_project(self, connection: Any, project_id: str) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise DomainError("project_not_found", "Project does not exist", status_code=404)
        return row

    def _require_task(
        self, connection: Any, project_id: str, task_id: str
    ) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND project_id = ?", (task_id, project_id)
        ).fetchone()
        if row is None:
            raise DomainError("task_not_found", "Task does not exist", status_code=404)
        return row

    def _lock_task_number_sequence(
        self, connection: Any, project_id: str
    ) -> Mapping[str, Any] | None:
        if getattr(self.database, "backend", "sqlite") == "postgresql":
            return connection.execute(
                """
                SELECT next_value FROM task_number_sequences
                WHERE project_id = ? FOR UPDATE
                """,
                (project_id,),
            ).fetchone()
        return connection.execute(
            "SELECT next_value FROM task_number_sequences WHERE project_id = ?",
            (project_id,),
        ).fetchone()

    def _lock_event_number_sequence(
        self, connection: Any, project_id: str
    ) -> Mapping[str, Any] | None:
        if getattr(self.database, "backend", "sqlite") == "postgresql":
            return connection.execute(
                """
                SELECT next_value FROM event_number_sequences
                WHERE project_id = ? FOR UPDATE
                """,
                (project_id,),
            ).fetchone()
        return connection.execute(
            "SELECT next_value FROM event_number_sequences WHERE project_id = ?",
            (project_id,),
        ).fetchone()

    def _next_event_number(self, connection: Any, project_id: str) -> int:
        """Allocate the next Project-scoped event number inside the caller's write transaction.

        The counter seeds itself from the current maximum when a Project emits
        its first event after this feature, so databases migrated by schema 22
        and databases created fresh behave identically.
        """
        for _ in range(5):
            row = self._lock_event_number_sequence(connection, project_id)
            if row is None:
                maximum = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(project_seq), 0) AS project_seq
                        FROM events WHERE project_id = ?
                        """,
                        (project_id,),
                    ).fetchone()["project_seq"]
                )
                connection.execute(
                    """
                    INSERT INTO event_number_sequences(project_id, next_value)
                    VALUES (?, ?)
                    ON CONFLICT(project_id) DO NOTHING
                    """,
                    (project_id, maximum + 1),
                )
                continue
            allocated = int(row["next_value"])
            connection.execute(
                """
                UPDATE event_number_sequences
                SET next_value = ? WHERE project_id = ?
                """,
                (allocated + 1, project_id),
            )
            return allocated
        raise DomainError(
            "event_number_allocation_failed",
            "Could not allocate a Project-scoped event number",
            status_code=500,
        )

    def _next_task_number(self, connection: Any, project_id: str) -> int:
        """Allocate the next Project-scoped number inside the caller's write transaction."""
        for _ in range(5):
            row = self._lock_task_number_sequence(connection, project_id)
            if row is None:
                maximum = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(task_number), 0) AS task_number
                        FROM tasks WHERE project_id = ?
                        """,
                        (project_id,),
                    ).fetchone()["task_number"]
                )
                connection.execute(
                    """
                    INSERT INTO task_number_sequences(project_id, next_value)
                    VALUES (?, ?)
                    ON CONFLICT(project_id) DO NOTHING
                    """,
                    (project_id, maximum + 1),
                )
                continue
            allocated = int(row["next_value"])
            connection.execute(
                """
                UPDATE task_number_sequences
                SET next_value = ? WHERE project_id = ?
                """,
                (allocated + 1, project_id),
            )
            return allocated
        raise DomainError(
            "task_number_conflict",
            "Could not allocate a unique task number",
            status_code=409,
        )

    def _require_task_by_number(
        self, connection: Any, project_id: str, task_number: int
    ) -> Mapping[str, Any]:
        if task_number < 1:
            raise DomainError(
                "invalid_task_number", "Task number must be a positive integer"
            )
        row = connection.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND task_number = ?",
            (project_id, task_number),
        ).fetchone()
        if row is None:
            raise DomainError(
                "task_not_found",
                "Task number does not exist in this Project",
                status_code=404,
                details={"task_number": task_number},
            )
        return row

    def _authenticate(
        self,
        connection: Any,
        project_id: str,
        session_id: str,
        token: str,
        *,
        allow_closed: bool = False,
    ) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id),
        ).fetchone()
        if row is None or not hmac.compare_digest(
            row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise DomainError(
                "invalid_session_token",
                "Agent session or token is invalid",
                status_code=401,
            )
        if parse_time(row["token_expires_at"]) <= utc_now():
            raise DomainError(
                "session_token_expired",
                "Agent session token has expired; join the room again",
                status_code=401,
            )
        if row["left_at"] is not None and not allow_closed:
            raise DomainError(
                "session_closed",
                "Agent session has already left the Room",
                status_code=409,
            )
        return row

    def _actor_event_snapshot(
        self, connection: Any, project_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """Resolve the operator identity once, at event write time.

        The snapshot is embedded into the event payload so the collaboration
        timeline keeps showing the operator even when the agent_sessions row
        later disappears (append-only history may never be rewritten).
        """
        session = connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id),
        ).fetchone()
        if session is None:
            return None
        member = None
        member_id = session["member_id"] if "member_id" in session.keys() else None
        if member_id:
            member = connection.execute(
                "SELECT * FROM project_members WHERE id = ?", (member_id,)
            ).fetchone()
            if member is not None:
                member = dict(member)
                member["metadata"] = json_load(member.get("metadata_json"), {})
        return actor_snapshot(dict(session), member)

    def _emit(
        self,
        connection: Any,
        project_id: str,
        event_type: str,
        *,
        actor_session_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        event_payload = dict(payload or {})
        if actor_session_id and "actor" not in event_payload:
            snapshot = self._actor_event_snapshot(
                connection, project_id, actor_session_id
            )
            if snapshot is not None:
                event_payload["actor"] = snapshot
        if task_id and "task_number" not in event_payload:
            task = connection.execute(
                "SELECT task_number FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            ).fetchone()
            if task is not None and task["task_number"] is not None:
                event_payload["task_number"] = int(task["task_number"])
        row = connection.execute(
            """
            INSERT INTO events(project_id, event_type, actor_session_id, task_id, payload_json, project_seq, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id, project_seq
            """,
            (
                project_id,
                event_type,
                actor_session_id,
                task_id,
                json_dump(event_payload),
                self._next_event_number(connection, project_id),
                iso_now(),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Event insert did not return an id")
        # 对外事件编号统一为 project_seq；物理主键 id 仅作存储内部标识。
        return int(row["project_seq"])

    def _event_dict(
        self, row: Mapping[str, Any], connection: Any | None = None
    ) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json_load(data.pop("payload_json"), {})
        if connection is not None and data.get("task_id"):
            task = connection.execute(
                "SELECT task_number FROM tasks WHERE id = ?",
                (data["task_id"],),
            ).fetchone()
            data["task_number"] = int(task["task_number"]) if task else None
        if data["event_type"].startswith("review."):
            default_channel = "review"
        elif data["task_id"]:
            default_channel = "task"
        elif data["event_type"].startswith("message."):
            default_channel = "public"
        else:
            default_channel = "system"
        data["channel"] = data["payload"].get("channel", default_channel)
        # Public event number is project_seq only (#169): the physical global
        # AUTOINCREMENT id is storage-internal and is not exposed under any
        # name — one event, one citable number, on every adapter.
        data.pop("id", None)
        seq = data.get("project_seq")
        if seq is not None:
            data["id"] = int(seq)
            data["project_seq"] = int(seq)
        return data

    def _project_dict(
        self,
        row: Mapping[str, Any],
        *,
        project_source: str | None = None,
        git_scope_recheck: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = dict(row)
        data["settings"] = normalize_project_settings(
            json_load(data.pop("settings_json"), {})
        )
        if project_source is None:
            project_source = (
                "git_remote"
                if str(data.get("git_remote") or "").strip()
                else "path"
            )
        data["project_source"] = project_source
        if git_scope_recheck is not None:
            data["git_scope_recheck"] = dict(git_scope_recheck)
        return data

    def _agent_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data.pop("token_hash", None)
        data["capabilities"] = json_load(data.pop("capabilities_json"), {})
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        # Presence is derived from the most recent proof of life: either a
        # background heartbeat or any real activity (message, claim, report...).
        # A session that keeps acting must not be reported offline just because
        # its background keepalive stopped (e.g. after a stdio process restart).
        last_seen = parse_time(data["last_heartbeat"])
        last_activity = data.get("last_activity_at")
        if last_activity:
            activity_time = parse_time(last_activity)
            if activity_time > last_seen:
                last_seen = activity_time
        if data.get("left_at") is not None or utc_now() - last_seen > timedelta(
            seconds=self.settings.heartbeat_timeout_seconds
        ):
            data["status"] = "offline"
        return data

    def _agent_identities(
        self,
        agents: Iterable[Mapping[str, Any]],
        tasks: Iterable[Mapping[str, Any]] = (),
        members: Iterable[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        sessions_list = list(agents)
        tasks_list = list(tasks)
        member_by_id = {
            str(member["id"]): member
            for member in members
            if str(member.get("id") or "").strip()
        }
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for agent in sessions_list:
            member_id = str(agent.get("member_id") or "").strip()
            if member_id:
                grouped.setdefault(member_id, []).append(agent)

        identities: list[dict[str, Any]] = []
        for member_id, sessions in grouped.items():
            connected = [session for session in sessions if session["status"] != "offline"]
            representative_pool = connected or sessions
            representative = max(
                representative_pool,
                key=lambda session: (
                    str(session.get("last_activity_at") or ""),
                    str(session.get("last_heartbeat") or ""),
                    str(session.get("created_at") or ""),
                ),
            )
            session_ids = {str(session["id"]) for session in sessions}
            owned_tasks = [
                task
                for task in tasks_list
                if str(task.get("owner_session_id") or "") in session_ids
                and str(task.get("status") or "") not in {"done", "cancelled"}
            ]
            current_task = (
                max(owned_tasks, key=lambda task: str(task.get("updated_at") or ""))
                if owned_tasks
                else None
            )
            last_activity_values = [
                str(session["last_activity_at"])
                for session in sessions
                if session.get("last_activity_at")
            ]
            member = member_by_id.get(member_id, {})
            # Project members are the source of truth for the current roster.
            # Keep every historical session in ``agents`` below, but do not
            # promote a revoked member into the current identity projection.
            if member and str(member.get("status") or "") == "revoked":
                continue
            member_metadata = member.get("metadata") or {}
            identities.append(
                {
                    "schema_version": DOMAIN_SCHEMA_VERSION,
                    "id": member_id,
                    "member_id": member_id,
                    "member_status": str(member.get("status") or "active"),
                    "agent_key": member_id,
                    "software_key": str(member_metadata.get("software_key") or ""),
                    "managed_identity": bool(
                        member_metadata.get("managed_identity", False)
                    ),
                    "legacy_identity": False,
                    "legacy_session_count": 0,
                    "name": str(member.get("name") or representative["name"]),
                    "client": str(
                        member_metadata.get("client") or representative["client"]
                    ),
                    "role": representative["role"],
                    "connection_status": "connected" if connected else "disconnected",
                    "activity_status": None,
                    "status": "online" if connected else "registered",
                    "current_task_id": str(current_task.get("id") or "")
                    if current_task
                    else "",
                    "current_task_title": str(current_task.get("title") or "")
                    if current_task
                    else "",
                    "current_task_status": str(current_task.get("status") or "")
                    if current_task
                    else "",
                    "session_count": len(sessions),
                    "active_session_count": len(connected),
                    "session_ids": [str(session["id"]) for session in sessions],
                    "active_session_ids": [str(session["id"]) for session in connected],
                    "representative_session_id": representative["id"],
                    "created_at": min(str(session["created_at"]) for session in sessions),
                    "last_heartbeat": max(
                        str(session["last_heartbeat"]) for session in sessions
                    ),
                    "last_activity_at": max(last_activity_values)
                    if last_activity_values
                    else None,
                    "unread_count": min(
                        int(session.get("unread_count") or 0) for session in sessions
                    ),
                    "models": sorted(
                        {
                            str(session.get("model") or "").strip()
                            for session in sessions
                            if str(session.get("model") or "").strip()
                        }
                    ),
                    # 当前模型 = 最近活跃 Session 的自报模型；缺失或占位
                    # "unknown" 不计入，交给前端显示明确的 unknown。
                    "current_model": (
                        str(representative.get("model") or "").strip()
                        if str(representative.get("model") or "").strip().lower() != "unknown"
                        else ""
                    ),
                }
            )
        return sorted(
            identities,
            key=lambda identity: (
                identity["connection_status"] != "connected",
                str(identity["name"]).casefold(),
                str(identity["member_id"]),
            ),
        )

    def _credential_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data.pop("token_hash", None)
        data["permissions"] = json_load(data.pop("permissions_json"), [])
        data["active"] = (
            data["revoked_at"] is None and parse_time(data["expires_at"]) > utc_now()
        )
        return data

    @staticmethod
    def _is_default_credential_name(name: str) -> bool:
        return bool(DEFAULT_CREDENTIAL_NAME_PATTERN.match(name.strip()))

    def _derive_unique_credential_name(
        self,
        connection: Any,
        project_id: str,
        member_name: str,
        *,
        exclude_credential_id: str | None = None,
    ) -> str:
        clean_name = member_name.strip() or "Agent"
        base = f"{clean_name} 凭据"
        existing_rows = connection.execute(
            "SELECT id, name FROM agent_credentials WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        taken = {
            row["name"].strip().casefold()
            for row in existing_rows
            if exclude_credential_id is None or row["id"] != exclude_credential_id
        }
        if base.casefold() not in taken:
            return base
        ordinal = 2
        while f"{base.casefold()} {ordinal}" in taken:
            ordinal += 1
        return f"{base} {ordinal}"

    def _member_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        data["schema_version"] = PROJECT_MEMBER_SCHEMA_VERSION
        data["active"] = data["status"] in {"invited", "active", "suspended"}
        return data

    @staticmethod
    def _normalize_software_key(value: str) -> str:
        clean = re.sub(r"[^a-z0-9._-]+", "-", value.strip().casefold()).strip("-.")
        if not clean:
            raise DomainError(
                "invalid_software_identity",
                "A stable software identity is required",
            )
        return clean

    @staticmethod
    def _reject_reserved_member_key(
        value: str,
        *,
        kind: str,
        metadata: Mapping[str, Any] | None,
        management_authorized: bool,
    ) -> str:
        """Keep the durable identity namespace writable only by the backend.

        A row under the ``software:`` prefix is adopted as a permanent software
        identity the next time the matching client joins, so pre-staking one hands
        the writer somebody else's identity (display name, member_id, and
        therefore same-identity task reclaim).
        """
        clean = value.strip()
        if not clean.casefold().startswith(SOFTWARE_MEMBER_PREFIX):
            return clean
        if not management_authorized:
            raise DomainError(
                "reserved_member_key",
                "Member keys in the reserved software identity namespace are issued "
                "by the backend when that identity first joins, or by an "
                "authenticated management request",
                status_code=409,
                details={
                    "reserved_prefix": SOFTWARE_MEMBER_PREFIX,
                    "required_action": "use_management_identity",
                },
            )
        values = metadata or {}
        if (
            kind != "software_agent"
            or not values.get("managed_identity")
            or not str(values.get("software_key") or "").strip()
            or not str(values.get("client") or "").strip()
        ):
            raise DomainError(
                "software_identity_unmanaged",
                "A member in the reserved namespace must be issued as a managed "
                "software identity with a software key and client binding",
                status_code=409,
                details={
                    "required_action": "set_kind_software_agent_and_metadata_"
                    "managed_identity_software_key_client"
                },
            )
        return clean

    @staticmethod
    def _software_name_conflict(
        connection: Any,
        project_id: str,
        name: str,
        *,
        exclude_member_key: str,
    ) -> Mapping[str, Any] | None:
        """Return the active software identity already using ``name``, if any."""
        return connection.execute(
            """
            SELECT id, member_key FROM project_members
            WHERE project_id = ? AND kind = 'software_agent' AND status = 'active'
              AND lower(trim(name)) = lower(trim(?)) AND member_key != ?
            LIMIT 1
            """,
            (project_id, name, exclude_member_key),
        ).fetchone()

    def _ensure_software_member(
        self,
        connection: Any,
        project_id: str,
        *,
        software_key: str,
        name: str,
        client: str,
    ) -> tuple[Mapping[str, Any], bool]:
        normalized_key = self._normalize_software_key(software_key)
        member_key = f"{SOFTWARE_MEMBER_PREFIX}{normalized_key}"
        member = connection.execute(
            "SELECT * FROM project_members WHERE project_id = ? AND member_key = ?",
            (project_id, member_key),
        ).fetchone()
        if member is not None:
            if member["status"] != "active":
                raise DomainError(
                    "software_identity_inactive",
                    "This software identity is not active in the Project",
                    status_code=409,
                )
            metadata = json_load(member["metadata_json"], {})
            if (
                str(member["kind"] or "") != "software_agent"
                or not metadata.get("managed_identity")
            ):
                # Roster writes are blocked from this namespace, but rows can
                # predate that rule. Only a row the backend issued may become a
                # durable identity, never a pre-staked one.
                raise DomainError(
                    "software_identity_unmanaged",
                    "This member key is reserved but was never issued as a managed "
                    "software identity",
                    status_code=409,
                    details={
                        "member_id": str(member["id"]),
                        "required_action": "recreate_the_row_through_the_management_api_or_use_another_key",
                    },
                )
            expected_client = str(metadata.get("client") or "").strip().casefold()
            if not expected_client:
                raise DomainError(
                    "software_identity_client_unbound",
                    "The managed software identity has no client binding to verify",
                    status_code=409,
                    details={
                        "member_id": str(member["id"]),
                        "required_action": "set_metadata_client_through_management",
                    },
                )
            if expected_client != client.strip().casefold():
                raise DomainError(
                    "software_identity_client_mismatch",
                    "The software identity is already bound to another client",
                    status_code=409,
                )
            return member, False

        name_conflict = self._software_name_conflict(
            connection, project_id, name, exclude_member_key=member_key
        )
        if name_conflict is not None:
            # 拒绝静默创建第二个同名 active 成员（#139）：同名不同 key 的
            # 接入必须走既有成员合并或由调用方改名，否则成员列表会出现
            # 两个无法区分的同名身份。
            raise DomainError(
                "software_identity_name_conflict",
                "Another active software identity already uses this name in the Project",
                status_code=409,
            )

        now = iso_now()
        member_id = new_id("member")
        connection.execute(
            """
            INSERT INTO project_members(
                id, project_id, member_key, name, kind, role, status,
                metadata_json, created_at, updated_at, revoked_at
            ) VALUES (?, ?, ?, ?, 'software_agent', '', 'active', ?, ?, ?, NULL)
            """,
            (
                member_id,
                project_id,
                member_key,
                name.strip(),
                json_dump(
                    {
                        "managed_identity": True,
                        "software_key": normalized_key,
                        "client": client.strip(),
                    }
                ),
                now,
                now,
            ),
        )
        member = connection.execute(
            "SELECT * FROM project_members WHERE id = ?", (member_id,)
        ).fetchone()
        return member, True

    def _require_joinable_member_identity(
        self,
        member: Mapping[str, Any],
        *,
        software_key: str,
        management_reference: bool,
    ) -> None:
        """Refuse a member reference the caller cannot prove it owns.

        A software-managed member is only joinable by presenting the same
        durable software key that the member was registered with. A member
        that carries no software identity of its own is only reachable through
        an authenticated management reference, because nothing in a plain
        join request can distinguish "operator asked for this offline
        identity" from "caller guessed the member id".
        """
        data = dict(member)
        metadata = json_load(data.get("metadata_json"), {})
        pinned_key = str(metadata.get("software_key") or "").strip()
        member_key = str(data.get("member_key") or "").strip()
        if not pinned_key and member_key.startswith(SOFTWARE_MEMBER_PREFIX):
            pinned_key = member_key[len(SOFTWARE_MEMBER_PREFIX) :]
        requested = str(software_key or "").strip()
        if pinned_key:
            if requested and self._normalize_software_key(
                pinned_key
            ) == self._normalize_software_key(requested):
                return
        elif management_reference:
            return
        raise DomainError(
            "software_identity_mismatch",
            "Joining as this project member requires proving the same software "
            "identity. Restore the original identity configuration, or issue a "
            "management credential linked to that member.",
            status_code=403,
            details={
                "required_action": (
                    "restore_configured_software_identity_or_use_management_credential"
                ),
            },
        )

    @staticmethod
    def _same_agent_identity(
        first: Mapping[str, Any] | None,
        second: Mapping[str, Any] | None,
    ) -> bool:
        if first is None or second is None:
            return False
        first_data = dict(first)
        second_data = dict(second)
        first_member = str(first_data.get("member_id") or "").strip()
        second_member = str(second_data.get("member_id") or "").strip()
        if first_member and second_member:
            return first_member == second_member
        first_key = str(first_data.get("agent_key") or "").strip()
        second_key = str(second_data.get("agent_key") or "").strip()
        return bool(first_key and second_key and first_key == second_key)

    def _owner_liveness(
        self,
        connection: Any,
        project_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """How alive an owning Session is, by the evidence ``task_claim`` trusts.

        ``left_at`` alone is not proof of absence: a Session that stopped
        heartbeating without leaving still holds a transport, and a Session whose
        transport was reaped is gone even inside the heartbeat window. Every path
        that moves ownership between Sessions has to use this one judgement.
        """
        owner = connection.execute(
            """
            SELECT a.*,
                   (SELECT MAX(e.created_at) FROM events e
                    WHERE e.project_id = a.project_id
                      AND e.actor_session_id = a.id) AS last_activity_at
            FROM agent_sessions a
            WHERE a.id = ? AND a.project_id = ?
            """,
            (owner_session_id, project_id),
        ).fetchone()
        if owner is None:
            return {
                "owner": None,
                "connected": False,
                "transport_state": "owner_missing",
                "last_seen": None,
            }
        last_seen = parse_time(owner["last_heartbeat"])
        if owner["last_activity_at"]:
            last_seen = max(last_seen, parse_time(owner["last_activity_at"]))
        connected = owner["left_at"] is None and (
            utc_now() - last_seen
            <= timedelta(seconds=self.settings.heartbeat_timeout_seconds)
        )
        transport_state = "not_checked"
        if connected:
            state_check = self.transport_liveness_check
            if state_check is None:
                transport_state = "unavailable"
            else:
                transport_state = (
                    str(state_check(project_id, str(owner["id"])) or "") or "unknown"
                )
                if transport_state == "gone":
                    connected = False
        return {
            "owner": owner,
            "connected": connected,
            "transport_state": transport_state,
            "last_seen": last_seen,
        }

    def retention_sweep_interval_seconds(self) -> float:
        """Minimum gap between two automatic retention sweeps, in monotonic seconds."""
        return float(
            getattr(self.settings, "retention_sweep_interval_seconds", 3600.0)
        )

    def min_agent_token_ttl_seconds(self) -> int:
        """Smallest Agent-token lifetime this deployment accepts."""
        return int(getattr(self.settings, "min_agent_token_ttl_seconds", 300))

    def _host_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        return data

    def _workspace_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        return data

    def _task_intake_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["schema_version"] = DOMAIN_SCHEMA_VERSION
        return data

    def _require_task_intake(
        self, connection: Any, project_id: str, intake_id: str
    ) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM task_intakes WHERE id = ? AND project_id = ?",
            (intake_id, project_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "task_intake_not_found", "Task intake does not exist", status_code=404
            )
        return row

    def _require_intake_target(
        self,
        connection: Any,
        project_id: str,
        member_id: str,
        session_id: str | None,
    ) -> Mapping[str, Any]:
        member = self._require_member(connection, project_id, member_id)
        if member["status"] == "revoked":
            raise DomainError(
                "task_intake_target_unavailable",
                "The target Agent member has been revoked",
                status_code=409,
            )
        if session_id:
            target = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ? AND project_id = ? AND member_id = ?",
                (session_id, project_id, member_id),
            ).fetchone()
            if target is None:
                raise DomainError(
                    "task_intake_target_not_found",
                    "The target Agent session does not belong to the selected member",
                    status_code=404,
                )
            if self._agent_dict(target)["status"] == "offline":
                raise DomainError(
                    "task_intake_target_unavailable",
                    "The selected Agent session is not connected",
                    status_code=409,
                )
        return member

    def _transition_task_intake(
        self, connection: Any, current_status: str, next_status: str
    ) -> None:
        """The single gate every intake status write has to pass."""
        if next_status not in TASK_INTAKE_STATUSES:
            raise DomainError("invalid_task_intake_status", "Unsupported intake status")
        if next_status not in TASK_INTAKE_TRANSITIONS.get(str(current_status), set()):
            raise DomainError(
                "invalid_task_intake_transition",
                f"Task intake cannot move from {current_status} to {next_status}",
                status_code=409,
            )

    def _task_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["task_number"] = int(data["task_number"])
        data["acceptance_criteria"] = json_load(
            data.pop("acceptance_criteria_json"), []
        )
        data["schema_version"] = DOMAIN_SCHEMA_VERSION
        data["state"] = task_contract(
            execution_status=data["execution_status"],
            verification_status=data["verification_status"],
            integration_status=data["integration_status"],
            legacy_status=data["status"],
        )
        data["state_view"] = task_view(
            execution_status=data["execution_status"],
            verification_status=data["verification_status"],
            integration_status=data["integration_status"],
        )
        data["phase"] = data["state_view"]["phase"]
        if data["phase"] == TASK_VIEW_UNCLASSIFIED_PHASE:
            _warn_unclassified_task_view(data)
        return data

    def _handoff_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        for field in ("completed_items", "pending_items", "files", "risks"):
            data[field] = json_load(data.pop(f"{field}_json"), [])
        return data

    def _integration_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["files"] = json_load(data.pop("files_json"), [])
        data["tests"] = json_load(data.pop("tests_json"), [])
        return data

    def _knowledge_version_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json_load(data.pop("tags_json"), [])
        raw_refs = json_load(data.pop("source_event_ids_json"), [])
        # Envelope form (#169): {"space","ids",...}; bare list = legacy global ids
        # that migration already normalized (or pre-migration leftovers).
        if isinstance(raw_refs, dict):
            space = str(raw_refs.get("space") or "project_seq")
            data["source_event_ids"] = [
                int(item) for item in (raw_refs.get("ids") or [])
            ]
            data["source_event_ids_space"] = space
            if raw_refs.get("ambiguous"):
                data["source_event_ids_ambiguous"] = raw_refs["ambiguous"]
            if raw_refs.get("unresolved"):
                data["source_event_ids_unresolved"] = raw_refs["unresolved"]
            if raw_refs.get("raw") is not None:
                data["source_event_ids_raw"] = raw_refs["raw"]
        else:
            data["source_event_ids"] = [
                int(item) for item in (raw_refs or [])
            ]
            data["source_event_ids_space"] = "project_seq"
        return data

    def _knowledge_review_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["criteria"] = json_load(data.pop("criteria_json"), [])
        return data

    def _knowledge_asset_dict(
        self, connection: Any, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        data = dict(row)
        data["schema_version"] = KNOWLEDGE_SCHEMA_VERSION
        current = connection.execute(
            """
            SELECT id, version, title, summary, tags_json, content_hash,
                   supersedes_version_id, created_at
            FROM knowledge_asset_versions WHERE id = ?
            """,
            (row["current_version_id"],),
        ).fetchone() if row["current_version_id"] else None
        if current is not None:
            data["current_version"] = {
                "version_id": current["id"],
                "version": int(current["version"]),
                "title": current["title"],
                "summary": current["summary"],
                "tags": json_load(current["tags_json"], []),
                "content_hash": current["content_hash"],
                "supersedes_version_id": current["supersedes_version_id"],
                "created_at": current["created_at"],
            }
        else:
            data["current_version"] = None
        return data

    def _task_with_dependencies(
        self, connection: Any, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._tasks_with_relations(connection, [row])[0]

    def _tasks_with_relations(
        self, connection: Any, rows: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        materialized = list(rows)
        if not materialized:
            return []
        task_ids = [row["id"] for row in materialized]
        placeholders = _sql_placeholders(len(task_ids))
        dependencies_by_task: dict[str, list[Mapping[str, Any]]] = {
            task_id: [] for task_id in task_ids
        }
        for dependency in connection.execute(
            f"""
            SELECT d.task_id AS child_task_id, t.id, t.task_number, t.title,
                   t.status, t.execution_status, t.verification_status,
                   t.integration_status
            FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id IN ({placeholders})
            ORDER BY d.created_at
            """,
            task_ids,
        ).fetchall():
            dependencies_by_task[str(dependency["child_task_id"])].append(dependency)
        assignments_by_task: dict[str, list[dict[str, Any]]] = {
            task_id: [] for task_id in task_ids
        }
        for assignment in connection.execute(
            f"""
            SELECT * FROM task_assignments
            WHERE task_id IN ({placeholders})
            ORDER BY created_at
            """,
            task_ids,
        ).fetchall():
            assignments_by_task[str(assignment["task_id"])].append(dict(assignment))
        handoffs_by_task: dict[str, list[dict[str, Any]]] = {
            task_id: [] for task_id in task_ids
        }
        for handoff in connection.execute(
            f"""
            SELECT * FROM task_handoffs
            WHERE task_id IN ({placeholders})
            ORDER BY created_at
            """,
            task_ids,
        ).fetchall():
            handoffs_by_task[str(handoff["task_id"])].append(self._handoff_dict(handoff))
        integrations_by_task: dict[str, list[dict[str, Any]]] = {
            task_id: [] for task_id in task_ids
        }
        for integration in connection.execute(
            f"""
            SELECT * FROM task_integrations
            WHERE task_id IN ({placeholders})
            ORDER BY created_at
            """,
            task_ids,
        ).fetchall():
            integrations_by_task[str(integration["task_id"])].append(
                self._integration_dict(integration)
            )
        spec_receipts: dict[str, dict[str, int]] = {
            str(row["id"]): {} for row in materialized
        }
        project_id = str(materialized[0]["project_id"])
        for claim_event in connection.execute(
            f"""
            SELECT task_id, payload_json FROM events
            WHERE project_id = ? AND event_type = 'task.claimed'
              AND task_id IN ({placeholders})
            ORDER BY id ASC
            """,
            [project_id, *task_ids],
        ):
            snapshot = json_load(claim_event["payload_json"], {}).get(
                "project_documents"
            )
            if snapshot:
                spec_receipts[str(claim_event["task_id"])] = snapshot
        assembled: list[dict[str, Any]] = []
        for row in materialized:
            data = self._task_dict(row)
            dependencies = dependencies_by_task.get(row["id"], [])
            data["depends_on"] = [dependency["id"] for dependency in dependencies]
            data["dependency_details"] = [
                {
                    "id": dependency["id"],
                    "task_number": int(dependency["task_number"]),
                    "title": dependency["title"],
                    "status": dependency["status"],
                    "execution_status": dependency["execution_status"],
                    "verification_status": dependency["verification_status"],
                    "integration_status": dependency["integration_status"],
                }
                for dependency in dependencies
            ]
            data["assignments"] = assignments_by_task.get(row["id"], [])
            data["handoffs"] = handoffs_by_task.get(row["id"], [])
            data["integrations"] = integrations_by_task.get(row["id"], [])
            data["spec_receipt"] = spec_receipts.get(str(row["id"]), {})
            assembled.append(data)
        return assembled

    def _lease_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        # Presence is display-only: a lease is active purely by its own TTL and
        # ends by expiry or explicit release. A silent owner (long quiet
        # computation past the heartbeat window) keeps its lease, so the
        # heartbeat must never invalidate or hand out the lease here.
        data.pop("owner_last_heartbeat", None)
        data.pop("owner_member_id", None)
        ttl_active = data["released_at"] is None and parse_time(
            data["expires_at"]
        ) > utc_now()
        data["active"] = ttl_active
        return data

    def create_project(
        self,
        *,
        root_path: str,
        name: str | None = None,
        logical_path: str = "",
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        root = Path(root_path).expanduser().resolve()
        if not root.is_dir():
            raise DomainError(
                "project_path_not_found",
                "Project root must be an existing directory",
                details={"root_path": str(root)},
            )
        remote, git_root, is_work_tree = _project_git_info(root)
        logical = derive_logical_path(root, git_root, logical_path)
        candidate_scope = _project_scope(remote, git_root, logical)
        now = iso_now()
        normalized_settings = normalize_project_settings(settings)
        with self.database.connect(write=True) as connection:
            scope_projects = [
                project
                for project in connection.execute("SELECT * FROM projects").fetchall()
                if _stored_project_scope(project) == candidate_scope
            ]
            active_conflicts = [
                project
                for project in scope_projects
                if project["archived_at"] is None
            ]
            if len(active_conflicts) == 1:
                existing = active_conflicts[0]
                return self._project_dict(
                    existing,
                    project_source=_project_source_from_disk(
                        str(existing["git_remote"] or ""), is_work_tree
                    ),
                )
            if len(active_conflicts) > 1:
                raise DomainError(
                    "project_scope_conflict",
                    "This repository scope has multiple active Rooms and requires operator cleanup",
                    status_code=409,
                    details={
                        "project_ids": [project["id"] for project in active_conflicts],
                        "project_keys": [project["project_key"] for project in active_conflicts],
                    },
                )
            archived_scope = [
                project for project in scope_projects if project["archived_at"] is not None
            ]
            if len(archived_scope) == 1:
                archived = archived_scope[0]
                connection.execute(
                    "UPDATE projects SET archived_at = NULL, updated_at = ? WHERE id = ?",
                    (now, archived["id"]),
                )
                self._emit(
                    connection,
                    archived["id"],
                    "project.restored",
                    payload={"root_path": str(root)},
                )
                restored = connection.execute(
                    "SELECT * FROM projects WHERE id = ?", (archived["id"],)
                ).fetchone()
                return self._project_dict(
                    restored,
                    project_source=_project_source_from_disk(
                        str(restored["git_remote"] or ""), is_work_tree
                    ),
                )
            if len(archived_scope) > 1:
                raise DomainError(
                    "project_scope_conflict",
                    "This repository scope has multiple archived Rooms and requires operator cleanup",
                    status_code=409,
                    details={
                        "project_ids": [project["id"] for project in archived_scope],
                        "project_keys": [project["project_key"] for project in archived_scope],
                    },
                )
            project_id = new_id("project")
            project_key = f"prj_{project_id.removeprefix('project_')}"
            connection.execute(
                """
                INSERT INTO projects(
                    id, project_key, name, root_path, git_remote, logical_path,
                    settings_json, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    project_id,
                    project_key,
                    name or root.name,
                    str(root),
                    remote or None,
                    logical,
                    json_dump(normalized_settings),
                    now,
                    now,
                ),
            )
            self._emit(
                connection,
                project_id,
                "project.created",
                payload={"name": name or root.name, "root_path": str(root)},
            )
            connection.execute(
                """
                INSERT INTO task_number_sequences(project_id, next_value)
                VALUES (?, 1)
                ON CONFLICT(project_id) DO NOTHING
                """,
                (project_id,),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return self._project_dict(
                row, project_source=_project_source_from_disk(remote, is_work_tree)
            )

    def resolve_project_for_join(
        self,
        *,
        root_path: str,
        registered_project_key: str | None = None,
        logical_path: str = "",
    ) -> dict[str, Any]:
        root = Path(root_path).expanduser().resolve()
        if not root.is_dir():
            raise DomainError(
                "project_path_not_found",
                "Project root must be an existing directory",
                details={"root_path": str(root)},
            )
        remote, git_root, _is_work_tree = _project_git_info(root)
        logical = derive_logical_path(root, git_root, logical_path)
        candidate_scope = _project_scope(remote, git_root, logical)
        reg_key = str(registered_project_key or "").strip()
        with self.database.connect(write=True) as connection:
            all_projects = connection.execute("SELECT * FROM projects").fetchall()
            scope_projects = [
                project
                for project in all_projects
                if _stored_project_scope(project) == candidate_scope
            ]
            if scope_projects and reg_key and not any(
                str(p["project_key"]).strip() == reg_key for p in scope_projects
            ):
                # #166 fail-closed：checkout 登记把本工作区钉在唯一 Room 上；
                # 登记的 Room 真实存在却不拥有当前仓库 scope 时，意味着两个
                # Room 争用同一仓库，必须显式拒绝而不是静默串房。key 已不在
                # 库中（遗留登记迁移）且 scope 归属无歧义时保留原自愈语义。
                registered = next(
                    (
                        p
                        for p in all_projects
                        if str(p["project_key"]).strip() == reg_key
                    ),
                    None,
                )
                if registered is not None:
                    raise DomainError(
                        "project_registration_conflict",
                        "Checkout registration points to a different Room than this repository's git scope; resolve the duplicate explicitly",
                        status_code=409,
                        details={
                            "registered_project_id": registered["id"],
                            "registered_project_key": registered["project_key"],
                            "scope_project_ids": [p["id"] for p in scope_projects],
                            "scope_project_keys": [
                                p["project_key"] for p in scope_projects
                            ],
                        },
                    )
            if not scope_projects and remote:
                norm_root = os.path.normcase(str(root.resolve()))
                # One selector for both self-heal branches. It never considers an
                # archived Room (restoring one is an explicit management action
                # that emits project.restored), it treats an unreadable stored
                # root as non-matching instead of letting ValueError become a 500,
                # and it re-checks the derived logical path so a different
                # subproject of the same repository cannot be healed by accident.
                heal_candidates = []
                for candidate in all_projects:
                    if candidate["archived_at"] is not None:
                        continue
                    if reg_key and str(candidate["project_key"]).strip() != reg_key:
                        continue
                    if not reg_key and str(candidate["git_remote"] or "").strip():
                        continue
                    if (
                        str(candidate["logical_path"] or "").casefold()
                        != logical.casefold()
                    ):
                        continue
                    try:
                        candidate_root = os.path.normcase(
                            str(
                                Path(str(candidate["root_path"]))
                                .expanduser()
                                .resolve()
                            )
                        )
                    except (OSError, ValueError):
                        continue
                    if candidate_root == norm_root:
                        heal_candidates.append(candidate)
                heal_target = (
                    heal_candidates[0] if len(heal_candidates) == 1 else None
                )

                if heal_target is not None:
                    now = iso_now()
                    connection.execute(
                        """
                        UPDATE projects
                        SET git_remote = ?, logical_path = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (remote, logical, now, heal_target["id"]),
                    )
                    self._emit(
                        connection,
                        heal_target["id"],
                        "project.updated",
                        payload={
                            "git_remote": remote,
                            "logical_path": logical,
                            "scope_healed": True,
                        },
                    )
                    updated_row = connection.execute(
                        "SELECT * FROM projects WHERE id = ?", (heal_target["id"],)
                    ).fetchone()
                    scope_projects = [updated_row]
            active_scope = [
                project for project in scope_projects if project["archived_at"] is None
            ]
            if len(active_scope) == 1:
                healed_source = _project_source_from_disk(
                    str(active_scope[0]["git_remote"] or ""),
                    _is_work_tree,
                )
                return self._project_dict(
                    active_scope[0], project_source=healed_source
                )
            if len(active_scope) > 1:
                raise DomainError(
                    "project_scope_conflict",
                    "This repository scope has multiple active Rooms and requires operator cleanup",
                    status_code=409,
                    details={
                        "project_ids": [project["id"] for project in active_scope],
                        "project_keys": [project["project_key"] for project in active_scope],
                    },
                )
            archived_scope = [
                project for project in scope_projects if project["archived_at"] is not None
            ]
            if len(archived_scope) == 1:
                archived = archived_scope[0]
                raise DomainError(
                    "project_archived",
                    "Project is archived and must be restored explicitly before agents join",
                    status_code=409,
                    details={
                        "project_id": archived["id"],
                        "project_key": archived["project_key"],
                        "archived_at": archived["archived_at"],
                    },
                )
            if archived_scope:
                raise DomainError(
                    "project_scope_conflict",
                    "This repository scope has multiple archived Rooms and requires operator cleanup",
                    status_code=409,
                    details={
                        "project_ids": [project["id"] for project in archived_scope],
                        "project_keys": [
                            project["project_key"] for project in archived_scope
                        ],
                    },
                )
            if reg_key:
                # The registered Room exists but is archived, so it was excluded
                # from the self-heal above. Report that instead of the orphaned
                # registration error the fall-through would produce.
                registered_row = next(
                    (
                        p
                        for p in all_projects
                        if str(p["project_key"]).strip() == reg_key
                    ),
                    None,
                )
                if (
                    registered_row is not None
                    and registered_row["archived_at"] is not None
                ):
                    raise DomainError(
                        "project_archived",
                        "Project is archived and must be restored explicitly before agents join",
                        status_code=409,
                        details={
                            "project_id": registered_row["id"],
                            "project_key": registered_row["project_key"],
                            "archived_at": registered_row["archived_at"],
                        },
                    )
            if str(registered_project_key or "").strip():
                raise DomainError(
                    "project_registration_orphaned",
                    "Checkout registration points to a Project that no longer exists; create a new Project explicitly",
                    status_code=409,
                    details={"registered_project_key": registered_project_key},
                )
        return self.create_project(
            root_path=str(root),
            logical_path=logical,
        )

    def list_projects(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects WHERE archived_at IS NULL ORDER BY updated_at DESC, name"
            ).fetchall()
            projects = [self._project_dict(row) for row in rows]
        # Display-only recheck: cheap path-scope rows may already be Git on
        # disk. No write connection and no scope mutation on the list path.
        for project in projects:
            if project.get("project_source") == "path" and str(
                project.get("root_path") or ""
            ).strip():
                project["project_source"] = self._peek_project_source(project)
        return projects

    def git_scope_recheck_ttl_seconds(self) -> float:
        """Configured TTL for the display-only git scope probe cache."""
        return float(
            getattr(
                self.settings,
                "git_scope_recheck_ttl_seconds",
                GIT_SCOPE_RECHECK_TTL_SECONDS,
            )
        )

    def invalidate_git_scope_probe(self, project_id: str) -> None:
        """Drop a cached probe so the next read re-detects the scope."""
        self._git_scope_probe_cache.pop(str(project_id), None)

    def _peek_project_source(self, project: Mapping[str, Any]) -> str:
        project_id = str(project.get("id") or "")
        root_path = str(project.get("root_path") or "").strip()
        if not root_path:
            return "path"
        now = time.monotonic()
        cached = self._git_scope_probe_cache.get(project_id)
        if cached and (now - cached[0]) < self.git_scope_recheck_ttl_seconds():
            return cached[1]
        source = "path"
        try:
            root = Path(root_path).expanduser().resolve()
            if root.is_dir():
                remote, _git_root, is_work_tree = _project_git_info(root)
                source = _project_source_from_disk(remote, is_work_tree)
        except (OSError, DomainError):
            source = "path"
        # Cache every display-only probe result. Restricting the cache to "path"
        # left a local work tree (source "git_local") re-running git subprocesses
        # on every single read, which is exactly the cost this TTL cache exists to
        # avoid - and the GUI polls once per second.
        self._git_scope_probe_cache[project_id] = (now, source, "")
        return source

    def archive_project(self, project_id: str) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            project = self._require_project(connection, project_id)
            if project["archived_at"] is None:
                now = iso_now()
                cursor = self._emit(
                    connection,
                    project_id,
                    "project.archived",
                    payload={"name": project["name"]},
                )
                connection.execute(
                    "UPDATE projects SET archived_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, project_id),
                )
                event_id: int | None = cursor
            else:
                cursor = self.latest_cursor(connection, project_id)
                event_id = None
            return {
                "archived": True,
                "project_id": project_id,
                "event_id": event_id,
                "cursor": cursor,
            }

    def delete_project(self, project_id: str) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            project = self._require_project(connection, project_id)
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return {
                "deleted": True,
                "project_id": project_id,
                "project_name": project["name"],
            }

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Read a Project, with ownership-resolved git scope recheck (#170/#172).

        Read path rules:
        - Already stored as git scope → pure read, no git subprocess, no write.
        - Path candidates re-probe disk so a later `git remote add` is visible
          immediately; a write connection opens only for an ownership-clear heal.
        - list_projects uses the TTL cache for display-only rechecks.
        - Ambiguous ownership (another active Room owns the candidate scope,
          same root_path, or registration key mismatch) fails closed: no
          UPDATE, no project.updated event, response carries git_scope_recheck.
        """
        with self.database.connect() as connection:
            project = self._require_project(connection, project_id)
            if str(project["git_remote"] or "").strip():
                return self._project_dict(project, project_source="git_remote")
            if project["archived_at"] is not None or not str(
                project["root_path"] or ""
            ).strip():
                return self._project_dict(project)

        root_path = str(project["root_path"])
        now = time.monotonic()
        # Detail GET uses the same TTL cache as list for path candidates (#172):
        # avoid a git subprocess on every read when nothing has changed.
        cached = self._git_scope_probe_cache.get(project_id)
        if cached and (now - cached[0]) < self.git_scope_recheck_ttl_seconds():
            return self._project_dict(project, project_source=cached[1])
        try:
            root = Path(root_path).expanduser().resolve()
        except (OSError, ValueError):
            return self._project_dict(project)
        if not root.is_dir():
            return self._project_dict(project)

        try:
            remote, git_root, is_work_tree = _project_git_info(root)
        except (OSError, DomainError):
            return self._project_dict(project)

        source = _project_source_from_disk(remote, is_work_tree)
        if not remote:
            # Local work-tree or plain path: display-only, never write. Cached so
            # the next read within the TTL skips the git subprocess entirely.
            self._git_scope_probe_cache[project_id] = (now, source, "")
            return self._project_dict(project, project_source=source)

        try:
            logical = derive_logical_path(
                root, git_root, str(project["logical_path"] or "")
            )
        except DomainError:
            self._git_scope_probe_cache[project_id] = (now, source, remote)
            return self._project_dict(
                project,
                project_source=source,
                git_scope_recheck={
                    "status": "blocked",
                    "reason": "project_scope_conflict",
                    "detail": "logical_path does not match git work tree",
                },
            )

        candidate_scope = _project_scope(remote, git_root, logical)
        norm_root = os.path.normcase(str(root))

        # Ownership resolution before any write (#166/#172 fail-closed).
        with self.database.connect() as connection:
            current = self._require_project(connection, project_id)
            if str(current["git_remote"] or "").strip():
                return self._project_dict(current, project_source="git_remote")
            if current["archived_at"] is not None:
                return self._project_dict(current, project_source=source)

            all_projects = connection.execute("SELECT * FROM projects").fetchall()
            blockers: list[Any] = []
            for other in all_projects:
                if other["id"] == project_id or other["archived_at"] is not None:
                    continue
                if _stored_project_scope(other) == candidate_scope:
                    blockers.append(other)
                    continue
                try:
                    other_root = os.path.normcase(
                        str(Path(str(other["root_path"])).expanduser().resolve())
                    )
                except (OSError, ValueError):
                    continue
                if other_root == norm_root:
                    # Same directory already owned by another active Room.
                    blockers.append(other)

            registration = None
            registration_unreadable = False
            try:
                registration = load_checkout_registration(
                    root, logical_path=logical
                )
            except DomainError:
                registration = None
                registration_unreadable = True

            registration_conflict = registration_unreadable
            if registration is not None:
                reg_key = str(registration.get("project_key") or "").strip()
                current_key = str(current["project_key"]).strip()
                if reg_key and reg_key != current_key:
                    registration_conflict = True
                    registered = next(
                        (
                            p
                            for p in all_projects
                            if str(p["project_key"]).strip() == reg_key
                        ),
                        None,
                    )
                    if registered is None:
                        # Orphaned registration: treat as ambiguous, no heal.
                        registration_conflict = True

            if blockers or registration_conflict:
                reason = (
                    "project_registration_conflict"
                    if registration_conflict and not blockers
                    else "project_scope_conflict"
                    if blockers and not registration_conflict
                    else "project_registration_conflict"
                )
                recheck = {
                    "status": "blocked",
                    "reason": reason,
                    "conflicting_project_ids": [str(p["id"]) for p in blockers],
                    "conflicting_project_keys": [
                        str(p["project_key"]) for p in blockers
                    ],
                    "registered_project_key": (
                        str(registration.get("project_key") or "")
                        if registration is not None
                        else ""
                    ),
                }
                self._git_scope_probe_cache[project_id] = (now, source, remote)
                return self._project_dict(
                    current,
                    project_source=source,
                    git_scope_recheck=recheck,
                )

        # Unique ownership: heal inside a write transaction with re-check.
        with self.database.connect(write=True) as connection:
            current = self._require_project(connection, project_id)
            if str(current["git_remote"] or "").strip():
                return self._project_dict(current, project_source="git_remote")
            if current["archived_at"] is not None:
                return self._project_dict(current, project_source=source)

            all_projects = connection.execute("SELECT * FROM projects").fetchall()
            for other in all_projects:
                if other["id"] == project_id or other["archived_at"] is not None:
                    continue
                if _stored_project_scope(other) == candidate_scope:
                    self._git_scope_probe_cache[project_id] = (now, source, remote)
                    return self._project_dict(
                        current,
                        project_source=source,
                        git_scope_recheck={
                            "status": "blocked",
                            "reason": "project_scope_conflict",
                            "conflicting_project_ids": [str(other["id"])],
                            "conflicting_project_keys": [str(other["project_key"])],
                        },
                    )

            heal_time = iso_now()
            connection.execute(
                """
                UPDATE projects
                SET git_remote = ?, logical_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (remote, logical, heal_time, project_id),
            )
            self._emit(
                connection,
                project_id,
                "project.updated",
                payload={
                    "git_remote": remote,
                    "logical_path": logical,
                    "scope_healed": True,
                },
            )
            healed = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            self._git_scope_probe_cache[project_id] = (now, "git_remote", remote)
            return self._project_dict(
                healed,
                project_source="git_remote",
                git_scope_recheck={"status": "healed", "reason": ""},
            )

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if name is not None and not name.strip():
            raise DomainError("invalid_project", "Project name cannot be empty")
        with self.database.connect(write=True) as connection:
            project = self._require_project(connection, project_id)
            next_settings = (
                normalize_project_settings(settings)
                if settings is not None
                else normalize_project_settings(json_load(project["settings_json"], {}))
            )
            changed_fields: list[str] = []
            next_name = project["name"]
            if name is not None and name.strip() != project["name"]:
                next_name = name.strip()
                changed_fields.append("name")
            if settings is not None and next_settings != normalize_project_settings(
                json_load(project["settings_json"], {})
            ):
                changed_fields.append("settings")
            if changed_fields:
                now = iso_now()
                connection.execute(
                    "UPDATE projects SET name = ?, settings_json = ?, updated_at = ? WHERE id = ?",
                    (next_name, json_dump(next_settings), now, project_id),
                )
                event_id = self._emit(
                    connection,
                    project_id,
                    "project.updated",
                    payload={"changed_fields": changed_fields},
                )
            else:
                event_id = self.latest_cursor(connection, project_id)
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            result = {
                "project": self._project_dict(row),
                "event_id": event_id if changed_fields else None,
                "cursor": event_id,
            }
        if changed_fields:
            # 保留策略可能刚被修改：保存设置后立即按新策略清理一次。
            result["audit_retention"] = self.enforce_audit_retention(project_id)
        return result

    def _require_member(
        self, connection: Any, project_id: str, member_id: str
    ) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM project_members WHERE id = ? AND project_id = ?",
            (member_id, project_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "project_member_not_found",
                "Project member does not exist",
                status_code=404,
            )
        return row

    def _validate_member_status(self, status: str) -> str:
        clean = status.strip().lower()
        if clean not in PROJECT_MEMBER_STATUSES:
            raise DomainError(
                "invalid_project_member_status",
                "Unsupported project member status",
                details={"allowed": sorted(PROJECT_MEMBER_STATUSES)},
            )
        return clean

    @idempotent_write("project_member.create")
    def create_project_member(
        self,
        project_id: str,
        *,
        member_key: str,
        name: str,
        kind: str = "agent",
        role: str = "",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
        actor_session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        clean_kind = kind.strip()
        clean_role = role.strip()
        clean_status = self._validate_member_status(status)
        clean_key = self._reject_reserved_member_key(
            member_key,
            kind=clean_kind,
            metadata=metadata,
            management_authorized=management_authorized,
        )
        if not clean_key or not clean_name or not clean_kind:
            raise DomainError(
                "invalid_project_member",
                "Member key, name, and kind are required",
            )
        now = iso_now()
        member_id = new_id("member")
        revoked_at = now if clean_status == "revoked" else None
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            if actor_session_id:
                self._authenticate(connection, project_id, actor_session_id, token or "")
            duplicate = connection.execute(
                "SELECT id FROM project_members WHERE project_id = ? AND member_key = ?",
                (project_id, clean_key),
            ).fetchone()
            if duplicate is not None:
                raise DomainError(
                    "project_member_key_exists",
                    "A project member with this key already exists",
                    status_code=409,
                )
            connection.execute(
                """
                INSERT INTO project_members(
                    id, project_id, member_key, name, kind, role, status,
                    metadata_json, created_at, updated_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    member_id,
                    project_id,
                    clean_key,
                    clean_name,
                    clean_kind,
                    clean_role,
                    clean_status,
                    json_dump(metadata or {}),
                    now,
                    now,
                    revoked_at,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "member.created",
                actor_session_id=actor_session_id,
                payload={
                    "member_id": member_id,
                    "member_key": clean_key,
                    "name": clean_name,
                    "kind": clean_kind,
                    "role": clean_role,
                    "status": clean_status,
                },
            )
            row = connection.execute(
                "SELECT * FROM project_members WHERE id = ?", (member_id,)
            ).fetchone()
            return {
                "member": self._member_dict(row),
                "event_id": event_id,
                "cursor": event_id,
            }

    def list_project_members(
        self, project_id: str, *, include_revoked: bool = True
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            return self._list_project_members(
                connection, project_id, include_revoked=include_revoked
            )

    def _list_project_members(
        self,
        connection: Any,
        project_id: str,
        *,
        include_revoked: bool = True,
    ) -> list[dict[str, Any]]:
        where = "m.project_id = ?"
        parameters: list[Any] = [project_id, project_id, project_id]
        if not include_revoked:
            where += " AND m.status <> 'revoked'"
        rows = connection.execute(
            f"""
            SELECT m.*,
                   COALESCE(c.credential_count, 0) AS credential_count,
                   COALESCE(s.session_count, 0) AS session_count
            FROM project_members m
            LEFT JOIN (
                SELECT member_id, COUNT(*) AS credential_count
                FROM agent_credentials
                WHERE project_id = ?
                GROUP BY member_id
            ) c ON c.member_id = m.id
            LEFT JOIN (
                SELECT member_id, COUNT(*) AS session_count
                FROM agent_sessions
                WHERE project_id = ?
                GROUP BY member_id
            ) s ON s.member_id = m.id
            WHERE {where}
            ORDER BY m.created_at, m.member_key
            """,
            parameters,
        ).fetchall()
        members: list[dict[str, Any]] = []
        for row in rows:
            member = self._member_dict(row)
            member["credential_count"] = int(row["credential_count"] or 0)
            member["session_count"] = int(row["session_count"] or 0)
            members.append(member)
        return members

    @idempotent_write("project_member.update")
    def update_project_member(
        self,
        project_id: str,
        member_id: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        role: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor_session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            member = self._require_member(connection, project_id, member_id)
            if actor_session_id:
                actor = self._authenticate(
                    connection, project_id, actor_session_id, token or ""
                )
                # member:write is a coarse capability that the default Agent
                # credential bundle carries; it is not a licence to rewrite or
                # revoke somebody else's durable identity. An Agent may maintain
                # its own member record, and anything beyond that has to arrive
                # through an authenticated management request.
                actor_member_id = (
                    actor["member_id"] if "member_id" in actor.keys() else None
                )
                if actor_member_id != member_id and not management_authorized:
                    raise DomainError(
                        "management_auth_required",
                        "Updating or revoking another member's durable identity "
                        "requires an authenticated management request",
                        status_code=403,
                        details={
                            "required_action": "use_management_identity",
                            "target_member_id": member_id,
                        },
                    )
            elif not management_authorized:
                raise _management_authorization_required(
                    "update this project member"
                )
            if member["status"] == "revoked" and status not in {None, "revoked"}:
                raise DomainError(
                    "project_member_revoked",
                    "A revoked project member cannot be reactivated",
                    status_code=409,
                )
            clean_name = member["name"] if name is None else name.strip()
            clean_kind = member["kind"] if kind is None else kind.strip()
            clean_role = member["role"] if role is None else role.strip()
            clean_status = (
                member["status"]
                if status is None
                else self._validate_member_status(status)
            )
            if not clean_name or not clean_kind:
                raise DomainError(
                    "invalid_project_member",
                    "Member name and kind cannot be empty",
                )
            # The duplicate-name guard that joins already honour has to cover
            # renames too, or renaming any member onto an existing software
            # identity name makes the roster ambiguous for projection (#139).
            if (
                clean_kind == "software_agent"
                and clean_status == "active"
                and (clean_name != member["name"] or clean_kind != member["kind"])
            ):
                name_conflict = self._software_name_conflict(
                    connection,
                    project_id,
                    clean_name,
                    exclude_member_key=str(member["member_key"]),
                )
                if name_conflict is not None:
                    raise DomainError(
                        "software_identity_name_conflict",
                        "Another active software identity already uses this name "
                        "in the Project",
                        status_code=409,
                        details={
                            "conflicting_member_id": str(name_conflict["id"]),
                            "required_action": "pick_a_distinct_identity_name",
                        },
                    )
            clean_metadata = (
                json_load(member["metadata_json"], {})
                if metadata is None
                else metadata
            )
            changed_fields = [
                field
                for field, current, next_value in (
                    ("name", member["name"], clean_name),
                    ("kind", member["kind"], clean_kind),
                    ("role", member["role"], clean_role),
                    ("status", member["status"], clean_status),
                    (
                        "metadata",
                        json_load(member["metadata_json"], {}),
                        clean_metadata,
                    ),
                )
                if current != next_value
            ]
            if not changed_fields:
                return {
                    "member": self._member_dict(member),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            now = iso_now()
            revoked_at = now if clean_status == "revoked" else member["revoked_at"]
            connection.execute(
                """
                UPDATE project_members
                SET name = ?, kind = ?, role = ?, status = ?, metadata_json = ?,
                    updated_at = ?, revoked_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (
                    clean_name,
                    clean_kind,
                    clean_role,
                    clean_status,
                    json_dump(clean_metadata),
                    now,
                    revoked_at,
                    member_id,
                    project_id,
                ),
            )
            revocation: dict[str, Any] | None = None
            if clean_status == "revoked":
                revocation = self._revoke_member_access_locked(
                    connection, project_id, member_id, now, actor_session_id
                )
            event_id = self._emit(
                connection,
                project_id,
                "member.revoked" if clean_status == "revoked" else "member.updated",
                actor_session_id=actor_session_id,
                payload={
                    "member_id": member_id,
                    "changed_fields": changed_fields,
                    "status": clean_status,
                    **({"access_revocation": revocation} if revocation else {}),
                },
            )
            updated = connection.execute(
                "SELECT * FROM project_members WHERE id = ?", (member_id,)
            ).fetchone()
            return {
                "member": self._member_dict(updated),
                "event_id": event_id,
                "cursor": event_id,
            }

    def _revoke_member_access_locked(
        self,
        connection: Any,
        project_id: str,
        member_id: str,
        now: str,
        actor_session_id: str | None,
    ) -> dict[str, Any]:
        """End every write path a member still holds when it is revoked.

        The roster row is only a display: the credentials, the Sessions already
        joined and the file leases those Sessions hold are what grant write
        authority. Revoking the roster alone left a removed identity posting
        messages, claiming tasks and reporting work forever - and the heartbeat
        kept renewing its token and leases on top of that.
        """
        credentials = connection.execute(
            """
            SELECT id FROM agent_credentials
            WHERE project_id = ? AND member_id = ? AND revoked_at IS NULL
            """,
            (project_id, member_id),
        ).fetchall()
        if credentials:
            placeholders = _sql_placeholders(len(credentials))
            connection.execute(
                f"""
                UPDATE agent_credentials SET revoked_at = ?
                WHERE project_id = ? AND member_id = ? AND revoked_at IS NULL
                  AND id IN ({placeholders})
                """,
                (now, project_id, member_id, *[row["id"] for row in credentials]),
            )
        sessions = connection.execute(
            """
            SELECT id FROM agent_sessions
            WHERE project_id = ? AND member_id = ? AND left_at IS NULL
            """,
            (project_id, member_id),
        ).fetchall()
        closed_session_ids = [row["id"] for row in sessions]
        released_lease_ids: list[str] = []
        if closed_session_ids:
            session_placeholders = _sql_placeholders(len(closed_session_ids))
            leased = connection.execute(
                f"""
                SELECT id FROM file_leases
                WHERE project_id = ? AND released_at IS NULL
                  AND session_id IN ({session_placeholders})
                """,
                (project_id, *closed_session_ids),
            ).fetchall()
            released_lease_ids = [row["id"] for row in leased]
            if released_lease_ids:
                lease_placeholders = _sql_placeholders(len(released_lease_ids))
                connection.execute(
                    f"""
                    UPDATE file_leases SET released_at = ?
                    WHERE project_id = ? AND released_at IS NULL
                      AND id IN ({lease_placeholders})
                    """,
                    (now, project_id, *released_lease_ids),
                )
            connection.execute(
                f"""
                UPDATE agent_sessions
                SET status = 'offline', left_at = ?, token_expires_at = ?
                WHERE project_id = ? AND id IN ({session_placeholders})
                """,
                (now, now, project_id, *closed_session_ids),
            )
        for credential in credentials:
            self._emit(
                connection,
                project_id,
                "credential.revoked",
                actor_session_id=actor_session_id,
                payload={
                    "credential_id": credential["id"],
                    "reason": "member_revoked",
                },
            )
        for closed_session_id in closed_session_ids:
            self._emit(
                connection,
                project_id,
                "session.leave",
                actor_session_id=actor_session_id,
                payload={
                    "session_id": closed_session_id,
                    "reason": "member_revoked",
                },
            )
        return {
            "credentials_revoked": len(credentials),
            "sessions_closed": len(closed_session_ids),
            "leases_released": len(released_lease_ids),
        }

    @idempotent_write("project_member.revoke")
    def revoke_project_member(        self,
        project_id: str,
        member_id: str,
        *,
        actor_session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        return self.update_project_member(
            project_id,
            member_id,
            status="revoked",
            actor_session_id=actor_session_id,
            token=token,
            management_authorized=management_authorized,
        )

    def issue_agent_token(
        self,
        project_id: str,
        *,
        name: str,
        member_id: str | None = None,
        permissions: list[str] | None = None,
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise DomainError("invalid_agent_token", "Agent token name is required")
        if permissions is None:
            selected = sorted(DEFAULT_AGENT_PERMISSIONS)
            unknown: list[str] = []
        else:
            # An empty list is an explicit "no permissions", not an omitted
            # field: treating it as unset silently minted a token holding every
            # default permission, the opposite of the request, and the issue
            # event recorded the escalated set as if it had been asked for.
            selected = sorted(set(permissions))
            unknown = sorted(set(selected) - AGENT_PERMISSIONS)
        if not selected or unknown:
            raise DomainError(
                "invalid_agent_permissions",
                "Agent token permissions must contain only supported values",
                details={
                    "unknown": unknown,
                    "required_action": "pass_supported_permissions_or_omit_the_field",
                },
            )
        ttl = (
            self.settings.agent_token_ttl_seconds
            if expires_in_seconds is None
            else expires_in_seconds
        )
        if not self.min_agent_token_ttl_seconds() <= ttl <= (
            self.settings.max_agent_token_ttl_seconds
        ):
            raise DomainError(
                "invalid_agent_token_ttl",
                "Agent token TTL is outside the configured range",
                details={
                    "minimum": self.min_agent_token_ttl_seconds(),
                    "maximum": self.settings.max_agent_token_ttl_seconds,
                },
            )
        credential_id = new_id("credential")
        secret = secrets.token_urlsafe(32)
        token = f"acr.{credential_id}.{secret}"
        now = iso_now()
        expires_at = (utc_now() + timedelta(seconds=ttl)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            if member_id:
                member = self._require_member(connection, project_id, member_id)
                if member["status"] != "active":
                    raise DomainError(
                        "project_member_inactive",
                        "Agent Token can only be linked to an active project member",
                        status_code=409,
                    )
            connection.execute(
                """
                INSERT INTO agent_credentials(
                    id, project_id, member_id, name, token_hash, permissions_json,
                    expires_at, revoked_at, created_at, updated_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                """,
                (
                    credential_id,
                    project_id,
                    member_id,
                    name.strip(),
                    hashlib.sha256(token.encode()).hexdigest(),
                    json_dump(selected),
                    expires_at,
                    now,
                    now,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "credential.issued",
                payload={
                    "credential_id": credential_id,
                    "member_id": member_id,
                    "name": name.strip(),
                    "permissions": selected,
                    "expires_at": expires_at,
                },
            )
            row = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            return {
                "credential": self._credential_dict(row),
                "token": token,
                "event_id": event_id,
                "cursor": event_id,
            }

    def list_agent_tokens(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            return [
                self._credential_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM agent_credentials
                    WHERE project_id = ? ORDER BY created_at DESC
                    """,
                    (project_id,),
                ).fetchall()
            ]

    def authenticate_agent_token(
        self,
        token: str,
        *,
        project_id: str | None = None,
        required_permission: str | None = None,
        touch: bool = False,
    ) -> dict[str, Any]:
        parts = token.split(".", 2)
        if len(parts) != 3 or parts[0] != "acr" or not parts[1]:
            raise DomainError(
                "invalid_agent_token", "Agent token is invalid", status_code=401
            )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (parts[1],)
            ).fetchone()
            if row is None or not hmac.compare_digest(
                row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
            ):
                raise DomainError(
                    "invalid_agent_token", "Agent token is invalid", status_code=401
                )
            if row["revoked_at"] is not None:
                raise DomainError(
                    "agent_token_revoked",
                    "Agent token has been revoked",
                    status_code=401,
                    details={
                        "required_action": CREDENTIAL_FAILURE_ACTIONS[
                            PROJECT_CREDENTIAL_REVOKED
                        ]
                    },
                )
            if parse_time(row["expires_at"]) <= utc_now():
                raise DomainError(
                    "agent_token_expired",
                    "Agent token has expired",
                    status_code=401,
                    details={
                        "required_action": CREDENTIAL_FAILURE_ACTIONS[
                            PROJECT_CREDENTIAL_EXPIRED
                        ]
                    },
                )
            if project_id is not None and row["project_id"] != project_id:
                raise DomainError(
                    "agent_token_project_forbidden",
                    "Agent token is not authorized for this Project",
                    status_code=403,
                )
            permissions = set(json_load(row["permissions_json"], []))
            if required_permission and required_permission not in permissions:
                raise DomainError(
                    "agent_token_permission_forbidden",
                    "Agent token lacks the required permission",
                    status_code=403,
                    details={"required_permission": required_permission},
                )
            credential = self._credential_dict(row)
        if touch:
            self._token_touch.note(str(credential["id"]))
            credential["last_used_at"] = iso_now()
        return credential

    def revoke_agent_token(self, project_id: str, credential_id: str) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """
                SELECT * FROM agent_credentials
                WHERE id = ? AND project_id = ?
                """,
                (credential_id, project_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "agent_token_not_found",
                    "Agent token does not exist",
                    status_code=404,
                )
            if row["revoked_at"] is not None:
                return {
                    "credential": self._credential_dict(row),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            now = iso_now()
            connection.execute(
                "UPDATE agent_credentials SET revoked_at = ?, updated_at = ? WHERE id = ?",
                (now, now, credential_id),
            )
            event_id = self._emit(
                connection,
                project_id,
                "credential.revoked",
                payload={"credential_id": credential_id},
            )
            updated = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            return {
                "credential": self._credential_dict(updated),
                "event_id": event_id,
                "cursor": event_id,
            }

    def update_agent_token_permissions(
        self,
        project_id: str,
        credential_id: str,
        *,
        permissions: list[str],
    ) -> dict[str, Any]:
        selected = sorted(set(permissions))
        unknown = sorted(set(selected) - AGENT_PERMISSIONS)
        if not selected or unknown:
            raise DomainError(
                "invalid_agent_permissions",
                "Agent token permissions must contain only supported values",
                details={"unknown": unknown},
            )
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """
                SELECT * FROM agent_credentials
                WHERE id = ? AND project_id = ?
                """,
                (credential_id, project_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "agent_token_not_found",
                    "Agent token does not exist",
                    status_code=404,
                )
            if row["revoked_at"] is not None:
                raise DomainError(
                    "agent_token_revoked",
                    "A revoked Agent token cannot be updated",
                    status_code=409,
                )
            previous = sorted(json_load(row["permissions_json"], []))
            if selected == previous:
                return {
                    "credential": self._credential_dict(row),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            now = iso_now()
            connection.execute(
                "UPDATE agent_credentials SET permissions_json = ?, updated_at = ? WHERE id = ?",
                (json_dump(selected), now, credential_id),
            )
            event_id = self._emit(
                connection,
                project_id,
                "credential.permissions_updated",
                payload={
                    "credential_id": credential_id,
                    "previous_permissions": previous,
                    "permissions": selected,
                },
            )
            updated = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            return {
                "credential": self._credential_dict(updated),
                "event_id": event_id,
                "cursor": event_id,
            }

    def extend_agent_token(
        self,
        project_id: str,
        credential_id: str,
        *,
        extend_by_seconds: int,
    ) -> dict[str, Any]:
        if not self.min_agent_token_ttl_seconds() <= extend_by_seconds <= (
            self.settings.max_agent_token_ttl_seconds
        ):
            raise DomainError(
                "invalid_agent_token_extension",
                "Agent token extension is outside the configured range",
                details={
                    "minimum": self.min_agent_token_ttl_seconds(),
                    "maximum": self.settings.max_agent_token_ttl_seconds,
                },
            )
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """
                SELECT * FROM agent_credentials
                WHERE id = ? AND project_id = ?
                """,
                (credential_id, project_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "agent_token_not_found",
                    "Agent token does not exist",
                    status_code=404,
                )
            if row["revoked_at"] is not None:
                raise DomainError(
                    "agent_token_revoked",
                    "A revoked Agent token cannot be extended",
                    status_code=409,
                )
            if parse_time(str(row["expires_at"])) <= utc_now():
                raise DomainError(
                    "agent_token_expired",
                    "An expired Agent token cannot be extended",
                    status_code=409,
                )
            now_time = utc_now()
            previous_expires_at = str(row["expires_at"])
            base_time = max(parse_time(previous_expires_at), now_time)
            expires_time = base_time + timedelta(seconds=extend_by_seconds)
            remaining_seconds = int((expires_time - now_time).total_seconds())
            if remaining_seconds > self.settings.max_agent_token_ttl_seconds:
                raise DomainError(
                    "invalid_agent_token_extension",
                    "Extended Agent token expiry exceeds the configured maximum",
                    details={
                        "maximum": self.settings.max_agent_token_ttl_seconds,
                        "current_expires_at": previous_expires_at,
                    },
                )
            expires_at = expires_time.isoformat().replace("+00:00", "Z")
            now = iso_now()
            connection.execute(
                "UPDATE agent_credentials SET expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at, now, credential_id),
            )
            event_id = self._emit(
                connection,
                project_id,
                "credential.extended",
                payload={
                    "credential_id": credential_id,
                    "previous_expires_at": previous_expires_at,
                    "expires_at": expires_at,
                    "extend_by_seconds": extend_by_seconds,
                    "token_changed": False,
                },
            )
            updated = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            return {
                "credential": self._credential_dict(updated),
                "token_changed": False,
                "event_id": event_id,
                "cursor": event_id,
            }

    def rotate_agent_token(
        self,
        project_id: str,
        credential_id: str,
        *,
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        ttl = (
            self.settings.agent_token_ttl_seconds
            if expires_in_seconds is None
            else expires_in_seconds
        )
        if not self.min_agent_token_ttl_seconds() <= ttl <= (
            self.settings.max_agent_token_ttl_seconds
        ):
            raise DomainError(
                "invalid_agent_token_ttl",
                "Agent token TTL is outside the configured range",
                details={
                    "minimum": self.min_agent_token_ttl_seconds(),
                    "maximum": self.settings.max_agent_token_ttl_seconds,
                },
            )
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            previous = connection.execute(
                """
                SELECT * FROM agent_credentials
                WHERE id = ? AND project_id = ?
                """,
                (credential_id, project_id),
            ).fetchone()
            if previous is None:
                raise DomainError(
                    "agent_token_not_found",
                    "Agent token does not exist",
                    status_code=404,
                )
            if previous["revoked_at"] is not None:
                raise DomainError(
                    "agent_token_revoked",
                    "A revoked Agent token cannot be rotated",
                    status_code=409,
                )
            now = iso_now()
            replacement_id = new_id("credential")
            token = f"acr.{replacement_id}.{secrets.token_urlsafe(32)}"
            expires_at = (utc_now() + timedelta(seconds=ttl)).isoformat().replace(
                "+00:00", "Z"
            )
            connection.execute(
                "UPDATE agent_credentials SET revoked_at = ?, updated_at = ? WHERE id = ?",
                (now, now, credential_id),
            )
            connection.execute(
                """
                INSERT INTO agent_credentials(
                    id, project_id, member_id, name, token_hash, permissions_json,
                    expires_at, revoked_at, created_at, updated_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                """,
                (
                    replacement_id,
                    project_id,
                    previous["member_id"],
                    previous["name"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    previous["permissions_json"],
                    expires_at,
                    now,
                    now,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "credential.rotated",
                payload={
                    "previous_credential_id": credential_id,
                    "credential_id": replacement_id,
                    "expires_at": expires_at,
                },
            )
            revoked = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            replacement = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (replacement_id,)
            ).fetchone()
            return {
                "previous_credential": self._credential_dict(revoked),
                "credential": self._credential_dict(replacement),
                "token": token,
                "event_id": event_id,
                "cursor": event_id,
            }

    @staticmethod
    def _validate_workspace_path(field: str, value: str) -> str:
        """Reject malformed workspace paths before they poison git evidence.

        Control characters (C0 and DEL) in a registered path silently break
        the server-side git evidence capture, which later fails every work
        report from sessions bound to that workspace with an unrelated
        invalid_commit_hash error. Relative paths are equally meaningless for
        a host-anchored workspace registration.
        """
        text = value.strip()
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise DomainError(
                "invalid_workspace",
                f"{field} contains control characters",
            )
        # Windows UNC / remote filesystem paths would make central git evidence
        # collection contact an attacker-controlled host (NTLM hash leak).
        if text.startswith("\\\\") or text.startswith("//"):
            raise DomainError(
                "invalid_workspace",
                f"{field} must be a local path; UNC/remote paths are not allowed",
                details={"field": field},
            )
        if not (posixpath.isabs(text) or ntpath.isabs(text)):
            raise DomainError(
                "invalid_workspace",
                f"{field} must be an absolute path",
            )
        return text

    def register_workspace(
        self,
        project_id: str,
        *,
        host_key: str,
        host_name: str,
        local_path: str,
        branch: str = "",
        worktree: str = "",
        git_remote: str = "",
        host_metadata: dict[str, Any] | None = None,
        workspace_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not host_key.strip() or not host_name.strip() or not local_path.strip():
            raise DomainError(
                "invalid_workspace",
                "Host key, Host name, and local workspace path are required",
            )
        local_path = self._validate_workspace_path("local_path", local_path)
        if str(worktree or "").strip():
            worktree = self._validate_workspace_path("worktree", worktree)
        else:
            worktree = ""
        now = iso_now()
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_key = ?", (host_key.strip(),)
            ).fetchone()
            host_created = host is None
            if host is None:
                host_id = new_id("host")
                connection.execute(
                    """
                    INSERT INTO hosts(
                        id, host_key, name, metadata_json,
                        created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        host_id,
                        host_key.strip(),
                        host_name.strip(),
                        json_dump(host_metadata or {}),
                        now,
                        now,
                        now,
                    ),
                )
            else:
                host_id = host["id"]
                connection.execute(
                    """
                    UPDATE hosts SET name = ?, metadata_json = ?,
                                     updated_at = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        host_name.strip(),
                        json_dump(host_metadata or json_load(host["metadata_json"], {})),
                        now,
                        now,
                        host_id,
                    ),
                )
            workspace = connection.execute(
                """
                SELECT * FROM workspaces
                WHERE project_id = ? AND host_id = ? AND local_path = ?
                """,
                (project_id, host_id, local_path.strip()),
            ).fetchone()
            workspace_created = workspace is None
            if workspace is None:
                workspace_id = new_id("workspace")
                connection.execute(
                    """
                    INSERT INTO workspaces(
                        id, project_id, host_id, local_path, branch, worktree,
                        git_remote, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        project_id,
                        host_id,
                        local_path.strip(),
                        branch.strip(),
                        worktree.strip(),
                        git_remote.strip(),
                        json_dump(workspace_metadata or {}),
                        now,
                        now,
                    ),
                )
            else:
                workspace_id = workspace["id"]
                connection.execute(
                    """
                    UPDATE workspaces
                    SET branch = ?, worktree = ?, git_remote = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        branch.strip(),
                        worktree.strip(),
                        git_remote.strip(),
                        json_dump(
                            workspace_metadata
                            or json_load(workspace["metadata_json"], {})
                        ),
                        now,
                        workspace_id,
                    ),
                )
            event_id = self._emit(
                connection,
                project_id,
                "workspace.registered" if workspace_created else "workspace.updated",
                payload={
                    "host_id": host_id,
                    "host_created": host_created,
                    "workspace_id": workspace_id,
                    "local_path": local_path.strip(),
                },
            )
            host = connection.execute(
                "SELECT * FROM hosts WHERE id = ?", (host_id,)
            ).fetchone()
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            return {
                "host": self._host_dict(host),
                "workspace": self._workspace_dict(workspace),
                "event_id": event_id,
                "cursor": event_id,
            }

    def list_workspaces(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """
                SELECT w.*, h.host_key, h.name AS host_name,
                       h.last_seen_at AS host_last_seen_at
                FROM workspaces w JOIN hosts h ON h.id = w.host_id
                WHERE w.project_id = ? ORDER BY h.name, w.local_path
                """,
                (project_id,),
            ).fetchall()
            return [self._workspace_dict(row) for row in rows]

    def join_room(
        self,
        project_id: str,
        *,
        agent_key: str = "",
        software_key: str = "",
        name: str,
        client: str,
        model: str,
        role: str = "executor",
        branch: str = "",
        worktree: str = "",
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        host_id: str | None = None,
        workspace_id: str | None = None,
        credential_id: str | None = None,
        member_id: str | None = None,
        manage_member_reference: bool = False,
    ) -> dict[str, Any]:
        if not name.strip() or not client.strip():
            raise DomainError("invalid_agent", "Agent name and client are required")
        if not model.strip():
            raise DomainError(
                "invalid_agent_model",
                "Agent model code is required; use unknown only when unavailable",
            )
        # A Session worktree is later used as a filesystem root (git evidence
        # capture, permanent Project cleanup), so it follows the same guard the
        # sibling workspace registration applies.
        clean_worktree = (
            self._validate_workspace_path("worktree", worktree)
            if str(worktree or "").strip()
            else ""
        )
        clean_branch = str(branch or "").strip()
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in clean_branch):
            raise DomainError("invalid_agent", "branch contains control characters")
        token = secrets.token_urlsafe(32)
        session_id = new_id("agent")
        now = iso_now()
        token_expires_at = (
            utc_now() + timedelta(seconds=self.settings.session_token_ttl_seconds)
        ).isoformat().replace("+00:00", "Z")
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            if workspace_id:
                workspace = connection.execute(
                    """
                    SELECT * FROM workspaces
                    WHERE id = ? AND project_id = ?
                    """,
                    (workspace_id, project_id),
                ).fetchone()
                if workspace is None or (
                    host_id is not None and workspace["host_id"] != host_id
                ):
                    raise DomainError(
                        "workspace_not_found",
                        "Workspace does not belong to this Project and Host",
                        status_code=404,
                    )
                host_id = workspace["host_id"]
            elif host_id:
                # A Session worktree reference is a foreign key, so an unknown
                # host used to reach the INSERT and surface as a raw
                # sqlite3.IntegrityError (HTTP 500 with a traceback) instead of
                # the 404 the workspace branch gives for the same class of typo.
                host = connection.execute(
                    "SELECT id FROM hosts WHERE id = ?", (host_id,)
                ).fetchone()
                if host is None:
                    raise DomainError(
                        "host_not_found",
                        "Host does not exist; register the workspace before joining",
                        status_code=404,
                        details={
                            "host_id": host_id,
                            "required_action": "register_workspace_or_omit_host_id",
                        },
                    )
            if credential_id:
                credential = connection.execute(
                    """
                    SELECT id, member_id, name, revoked_at, expires_at
                    FROM agent_credentials
                    WHERE id = ? AND project_id = ?
                    """,
                    (credential_id, project_id),
                ).fetchone()
                if credential is None:
                    raise DomainError(
                        "agent_token_not_found",
                        "Agent credential does not belong to this Project",
                        status_code=404,
                    )
                # A revoked or expired credential must not be able to mint a new
                # 12h Session; previously only the existence of the row was
                # checked, so a revoked Token kept full write authority.
                _require_live_credential(credential)
                if member_id and credential["member_id"] not in {None, member_id}:
                    raise DomainError(
                        "project_member_mismatch",
                        "Agent credential is linked to a different project member",
                        status_code=409,
                    )
                member_id = member_id or credential["member_id"]
            if member_id:
                member = self._require_member(connection, project_id, member_id)
                if member["status"] != "active":
                    raise DomainError(
                        "project_member_inactive",
                        "Agent session can only join as an active project member",
                        status_code=409,
                    )
                # Naming a member is not proof of being that member. A caller
                # may join as a member only when it can actually prove that
                # identity: a management-issued credential already linked to
                # that member (resolved above), or a software key equal to the
                # member's pinned key. Management may additionally reference a
                # member that carries no software identity of its own. Without
                # one of these proofs any caller could mint a Session as
                # another software identity and inherit its independence,
                # lease takeovers and append-only history.
                if not credential_id:
                    self._require_joinable_member_identity(
                        member,
                        software_key=software_key,
                        management_reference=manage_member_reference,
                    )
                member_created = False
            else:
                member, member_created = self._ensure_software_member(
                    connection,
                    project_id,
                    software_key=software_key or client,
                    name=name,
                    client=client,
                )
                member_id = str(member["id"])

            canonical_agent_key = str(member_id)
            # Adopt pre-managed Sessions only when this client maps to a single
            # software identity. Multiple active members sharing one client
            # (e.g. two machines both using "zcode") must not absorb each
            # other's orphan history; leave member_id NULL for explicit merge.
            adopted_sessions: list[dict[str, Any]] = []
            shared_client_members = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT s.member_id) AS member_count
                    FROM agent_sessions s
                    WHERE s.project_id = ?
                      AND s.member_id IS NOT NULL
                      AND s.member_id != ?
                      AND lower(trim(s.client)) = lower(trim(?))
                    """,
                    (project_id, member_id, client),
                ).fetchone()["member_count"]
            )
            if shared_client_members == 0:
                # Adopting an ownerless Session assigns a durable identity to it,
                # so its tasks and leases become reachable by the joiner. Matching
                # on the client string alone let any same-client identity inherit
                # another identity's orphan history; the recorded software key has
                # to match as well, and the rewrite is audited in agent.joined.
                orphan_rows = connection.execute(
                    """
                    SELECT id, agent_key, member_id FROM agent_sessions
                    WHERE project_id = ? AND member_id IS NULL
                      AND lower(trim(client)) = lower(trim(?))
                      AND lower(trim(agent_key)) = lower(trim(?))
                    """,
                    (
                        project_id,
                        client,
                        self._normalize_software_key(software_key or client),
                    ),
                ).fetchall()
                if orphan_rows:
                    connection.execute(
                        """
                        UPDATE agent_sessions SET member_id = ?, agent_key = ?
                        WHERE project_id = ? AND member_id IS NULL
                          AND lower(trim(client)) = lower(trim(?))
                          AND lower(trim(agent_key)) = lower(trim(?))
                        """,
                        (
                            member_id,
                            canonical_agent_key,
                            project_id,
                            client,
                            self._normalize_software_key(software_key or client),
                        ),
                    )
                adopted_sessions = [
                    {
                        "session_id": str(row["id"]),
                        "previous_member_id": (
                            None if row["member_id"] is None else str(row["member_id"])
                        ),
                        "member_id": member_id,
                        "previous_agent_key": str(row["agent_key"] or ""),
                        "agent_key": canonical_agent_key,
                    }
                    for row in orphan_rows
                ]
            try:
                connection.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, project_id, member_id, agent_key, name, client, model, role, status, branch,
                        worktree, host_id, workspace_id, credential_id,
                        capabilities_json, metadata_json, token_hash,
                        token_expires_at, created_at, last_heartbeat
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        project_id,
                        member_id,
                        canonical_agent_key,
                        name.strip(),
                        client.strip(),
                        model.strip(),
                        role.strip() or "executor",
                        clean_branch,
                        clean_worktree,
                        host_id,
                        workspace_id,
                        credential_id,
                        json_dump(capabilities or {}),
                        json_dump(metadata or {}),
                        hashlib.sha256(token.encode()).hexdigest(),
                        token_expires_at,
                        now,
                        now,
                    ),
                )

            except sqlite3.IntegrityError as error:
                # Every reference has been resolved above, so a violation here
                # is an unforeseen constraint: answer with the domain vocabulary
                # rather than a bare traceback through the adapter.
                raise DomainError(
                    "invalid_join_reference",
                    "The Session could not be created against these references",
                    status_code=409,
                    details={
                        "required_action": "register_the_workspace_or_credential_first"
                    },
                ) from error            # One durable software identity may have several concurrent client
            # conversations. Session ownership is intentionally independent:
            # joining a new conversation must not close another connection or
            # silently move its tasks and leases.
            previous_member_sessions = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM agent_sessions"
                    " WHERE project_id = ? AND member_id = ? AND id <> ?",
                    (project_id, member_id, session_id),
                ).fetchone()["total"]
            )
            if member_created:
                self._emit(
                    connection,
                    project_id,
                    "agent.identity_registered",
                    actor_session_id=session_id,
                    payload={
                        "member_id": member_id,
                        "software_key": self._normalize_software_key(
                            software_key or client
                        ),
                        "name": name.strip(),
                        "client": client.strip(),
                    },
                )
            if credential_id and credential is not None:
                needs_link = credential["member_id"] != member_id
                needs_rename = self._is_default_credential_name(credential["name"])
                if needs_link or needs_rename:
                    cred_name = credential["name"]
                    if needs_rename:
                        cred_name = self._derive_unique_credential_name(
                            connection,
                            project_id,
                            str(member["name"]),
                            exclude_credential_id=credential_id,
                        )
                    connection.execute(
                        "UPDATE agent_credentials SET member_id = ?, name = ?, updated_at = ? WHERE id = ?",
                        (member_id, cred_name, now, credential_id),
                    )
                    self._emit(
                        connection,
                        project_id,
                        "agent.credential_linked",
                        actor_session_id=session_id,
                        payload={
                            "credential_id": credential_id,
                            "member_id": member_id,
                            "name": cred_name,
                        },
                    )
            event_id = self._emit(
                connection,
                project_id,
                "agent.joined",
                actor_session_id=session_id,
                payload={
                    "agent_key": canonical_agent_key,
                    "member_id": member_id,
                    "software_key": self._normalize_software_key(
                        software_key or client
                    ),
                    "name": name.strip(),
                    "client": client.strip(),
                    "model": model.strip(),
                    "role": role,
                    "reconnected": previous_member_sessions > 0,
                    "adopted_sessions": adopted_sessions,
                },
            )
            connection.execute(
                "UPDATE agent_sessions SET last_read_cursor = ? WHERE id = ?",
                (event_id, session_id),
            )
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return {
                "agent": self._agent_dict(row),
                "identity": self._member_dict(member),
                "member_created": member_created,
                "token": token,
                "event_id": event_id,
                "cursor": event_id,
            }

    def link_credential_member(
        self,
        project_id: str,
        credential_id: str,
        member_id: str,
        *,
        actor_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Pin an auto-registered software member to the credential that
        bootstrapped it, so later requests presenting this credential with a
        different software identity are rejected instead of drifting."""
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            credential = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ? AND project_id = ?",
                (credential_id, project_id),
            ).fetchone()
            if credential is None:
                raise DomainError(
                    "agent_token_not_found",
                    "Agent credential does not belong to this Project",
                    status_code=404,
                )
            member = connection.execute(
                "SELECT * FROM project_members WHERE id = ? AND project_id = ?",
                (member_id, project_id),
            ).fetchone()
            if member is None or member["status"] != "active":
                raise DomainError(
                    "project_member_inactive",
                    "Credential can only be linked to an active project member",
                    status_code=409,
                )
            new_name = None
            if self._is_default_credential_name(credential["name"]):
                new_name = self._derive_unique_credential_name(
                    connection,
                    project_id,
                    str(member["name"]),
                    exclude_credential_id=credential_id,
                )
            if credential["member_id"] == member_id:
                if new_name and new_name != credential["name"]:
                    now = iso_now()
                    connection.execute(
                        "UPDATE agent_credentials SET name = ?, updated_at = ? WHERE id = ?",
                        (new_name, now, credential_id),
                    )
                    event_id = self._emit(
                        connection,
                        project_id,
                        "agent.credential_linked",
                        actor_session_id=actor_session_id,
                        payload={
                            "credential_id": credential_id,
                            "member_id": member_id,
                            "name": new_name,
                        },
                    )
                    updated = connection.execute(
                        "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
                    ).fetchone()
                    return {
                        "credential": self._credential_dict(updated),
                        "linked": True,
                        "event_id": event_id,
                        "cursor": event_id,
                    }
                return {"credential": self._credential_dict(credential), "linked": False}
            if credential["member_id"] is not None:
                raise DomainError(
                    "credential_already_linked",
                    "Agent credential is already linked to a project member",
                    status_code=409,
                )
            now = iso_now()
            updated_name = new_name or credential["name"]
            connection.execute(
                "UPDATE agent_credentials SET member_id = ?, name = ?, updated_at = ? WHERE id = ?",
                (member_id, updated_name, now, credential_id),
            )
            event_id = self._emit(
                connection,
                project_id,
                "agent.credential_linked",
                actor_session_id=actor_session_id,
                payload={
                    "credential_id": credential_id,
                    "member_id": member_id,
                    "name": updated_name,
                },
            )
            updated = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            return {
                "credential": self._credential_dict(updated),
                "linked": True,
                "event_id": event_id,
                "cursor": event_id,
            }

    def join_remote_room(
        self,
        project_id: str,
        *,
        agent_token: str,
        host_key: str,
        host_name: str,
        workspace_path: str,
        model: str,
        software_key: str = "",
        agent_key: str = "",
        name: str = "",
        client: str = "",
        role: str = "executor",
        branch: str = "",
        worktree: str = "",
        git_remote: str = "",
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        credential = self.authenticate_agent_token(
            agent_token,
            project_id=project_id,
            required_permission="room:join",
            touch=True,
        )
        registered = self.register_workspace(
            project_id,
            host_key=host_key,
            host_name=host_name,
            local_path=workspace_path,
            branch=branch,
            worktree=worktree,
            git_remote=git_remote,
        )
        # The credential is the trust boundary for a remote join: a member it
        # is linked to pins the durable identity, and its display fields are
        # derived from that member. An unlinked credential falls back to the
        # software identity the adapter already validated against the request
        # credentials and headers. Caller-supplied display fields never select
        # which software identity the Session runs as.
        credential_member_id = str(credential.get("member_id") or "")
        if credential_member_id:
            with self.database.connect() as connection:
                linked_member = self._require_member(
                    connection, project_id, credential_member_id
                )
            linked_metadata = json_load(linked_member["metadata_json"], {})
            name = str(name or "").strip() or str(linked_member["name"] or "").strip()
            client = (
                str(client or "").strip()
                or str(linked_metadata.get("client") or "").strip()
            )
        joined = self.join_room(
            project_id,
            agent_key=agent_key,
            software_key=software_key,
            name=name,
            client=client,
            model=model,
            role=role,
            branch=branch,
            worktree=worktree or workspace_path,
            capabilities=capabilities,
            metadata=metadata,
            host_id=registered["host"]["id"],
            workspace_id=registered["workspace"]["id"],
            credential_id=credential["id"],
            member_id=credential_member_id or None,
        )
        return {"credential": credential, **registered, **joined}

    @idempotent_write("session.heartbeat")
    def heartbeat(
        self,
        project_id: str,
        session_id: str,
        token: str,
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            now = iso_now()
            token_expires_at = (
                utc_now() + timedelta(seconds=self.settings.session_token_ttl_seconds)
            ).isoformat().replace("+00:00", "Z")
            connection.execute(
                """
                UPDATE agent_sessions
                SET status = ?, last_heartbeat = ?, token_expires_at = ?
                WHERE id = ?
                """,
                ("online", now, token_expires_at, session_id),
            )
            leases = connection.execute(
                """
                SELECT id, ttl_seconds FROM file_leases
                WHERE project_id = ? AND session_id = ? AND released_at IS NULL
                  AND expires_at > ?
                """,
                (project_id, session_id, now),
            ).fetchall()
            for lease in leases:
                lease_expires_at = (
                    utc_now() + timedelta(seconds=int(lease["ttl_seconds"]))
                ).isoformat().replace("+00:00", "Z")
                connection.execute(
                    "UPDATE file_leases SET expires_at = ?, renewed_at = ? WHERE id = ?",
                    (lease_expires_at, now, lease["id"]),
                )
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._agent_dict(row)

    @idempotent_write("session.leave")
    def leave_session(
        self,
        project_id: str,
        session_id: str,
        token: str,
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            session = self._authenticate(
                connection,
                project_id,
                session_id,
                token,
                allow_closed=True,
            )
            if session["left_at"] is not None:
                return {
                    "agent": self._agent_dict(session),
                    "released_lease_ids": [],
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }

            active_leases = connection.execute(
                """
                SELECT id FROM file_leases
                WHERE project_id = ? AND session_id = ? AND released_at IS NULL
                """,
                (project_id, session_id),
            ).fetchall()
            released_lease_ids = [row["id"] for row in active_leases]
            now = iso_now()
            if released_lease_ids:
                connection.execute(
                    """
                    UPDATE file_leases SET released_at = ?
                    WHERE project_id = ? AND session_id = ? AND released_at IS NULL
                    """,
                    (now, project_id, session_id),
                )
            connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'offline', left_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            event_id = self._emit(
                connection,
                project_id,
                "agent.left",
                actor_session_id=session_id,
                payload={"released_lease_ids": released_lease_ids},
            )
            updated = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return {
                "agent": self._agent_dict(updated),
                "released_lease_ids": released_lease_ids,
                "event_id": event_id,
                "cursor": event_id,
            }

    def list_events(
        self, project_id: str, *, after: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            return self._list_events(connection, project_id, after=after, limit=limit)

    def _list_events(
        self,
        connection: Any,
        project_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        after = max(0, int(after or 0))
        rows = connection.execute(
            """
            SELECT * FROM events
            WHERE project_id = ? AND project_seq > ?
            ORDER BY project_seq ASC LIMIT ?
            """,
            (project_id, after, limit),
        ).fetchall()
        events = [self._event_dict(row, connection) for row in rows]
        cursor = events[-1]["project_seq"] if events else after
        latest = connection.execute(
            "SELECT COALESCE(MAX(project_seq), 0) AS cursor FROM events WHERE project_id = ?",
            (project_id,),
        ).fetchone()["cursor"]
        # A retention purge can leave a hole between the caller's cursor and
        # the oldest retained event. Reporting the highest purged project_seq
        # lets a client holding an old cursor tell "there is a gap here" apart
        # from "you are already at the start".
        purged_before = 0
        for receipt in connection.execute(
            """
            SELECT payload_json FROM events
            WHERE project_id = ? AND event_type = 'audit.purged'
            ORDER BY project_seq DESC
            """,
            (project_id,),
        ).fetchall():
            value = json_load(receipt["payload_json"], {}).get("purged_before")
            if isinstance(value, int) and not isinstance(value, bool):
                purged_before = max(purged_before, value)
        return {
            "events": events,
            "cursor": int(cursor),
            "latest_cursor": int(latest),
            "purged_before": purged_before or None,
            "cursor_is_purged": bool(purged_before) and after < purged_before,
        }

    def get_project_mcp_message_limit(
        self, project_id: str, *, connection: Any | None = None
    ) -> int:
        def _resolve(conn: Any) -> int:
            project = self._require_project(conn, project_id)
            raw = json_load(project["settings_json"], {})
            if "mcp_message_limit" in raw and raw["mcp_message_limit"] is not None:
                val = raw["mcp_message_limit"]
                if isinstance(val, int) and not isinstance(val, bool) and 1 <= val <= 10:
                    return val
            if "mcp_message_context_limit" in raw and raw["mcp_message_context_limit"] is not None:
                val = raw["mcp_message_context_limit"]
                if isinstance(val, int) and not isinstance(val, bool) and 1 <= val <= 10:
                    return val
            env_val = os.environ.get("AGENTCHATROOM_MCP_MESSAGE_LIMIT", "").strip()
            if env_val.isdigit():
                parsed = int(env_val)
                if 1 <= parsed <= 10:
                    return parsed
            return getattr(self.settings, "mcp_message_limit", 5)

        if connection is not None:
            return _resolve(connection)
        with self.database.connect() as conn:
            return _resolve(conn)

    def _list_mcp_messages(
        self,
        connection: Any,
        project_id: str,
        *,
        after: int = 0,
        limit: int = 5,
        session_id: str | None = None,
        initial_inject: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 10))
        effective_after = after
        if not initial_inject and effective_after <= 0 and session_id:
            session_row = connection.execute(
                "SELECT last_read_cursor FROM agent_sessions WHERE id = ? AND project_id = ?",
                (session_id, project_id),
            ).fetchone()
            if session_row and session_row["last_read_cursor"]:
                effective_after = int(session_row["last_read_cursor"])

        placeholders = ", ".join("?" for _ in MCP_CONTEXT_EVENT_TYPES)
        # An incremental sync must deliver from the read cursor forwards, one
        # contiguous window at a time. Taking the newest `limit` rows and then
        # advancing the cursor to the project head marked every older
        # not-yet-shown message as read, so a burst of traffic bigger than the
        # window was silently swallowed - including requires_ack messages, which
        # the whole acknowledgement loop depends on being seen.
        incremental = not initial_inject and effective_after > 0
        if incremental:
            query = f"""
                SELECT * FROM events
                WHERE project_id = ?
                  AND event_type IN ({placeholders})
                  AND project_seq > ?
                ORDER BY project_seq ASC
                LIMIT ?
            """
            params = (project_id, *MCP_CONTEXT_EVENT_TYPES, effective_after, limit)
        elif effective_after > 0:
            query = f"""
                SELECT * FROM events
                WHERE project_id = ?
                  AND event_type IN ({placeholders})
                  AND project_seq > ?
                ORDER BY project_seq DESC
                LIMIT ?
            """
            params = (project_id, *MCP_CONTEXT_EVENT_TYPES, effective_after, limit)
        else:
            query = f"""
                SELECT * FROM events
                WHERE project_id = ?
                  AND event_type IN ({placeholders})
                ORDER BY project_seq DESC
                LIMIT ?
            """
            params = (project_id, *MCP_CONTEXT_EVENT_TYPES, limit)

        rows = connection.execute(query, params).fetchall()
        events = [
            self._event_dict(row, connection)
            for row in (rows if incremental else reversed(rows))
        ]
        latest = connection.execute(
            "SELECT COALESCE(MAX(project_seq), 0) AS cursor FROM events WHERE project_id = ?",
            (project_id,),
        ).fetchone()["cursor"]
        if incremental:
            delivered_through = max(
                (int(event["project_seq"]) for event in events),
                default=effective_after,
            )
            cursor = max(int(after or 0), effective_after, delivered_through)
            # A real existence probe, not a window heuristic: the caller needs
            # to know whether more context is still waiting behind the cursor.
            remaining = connection.execute(
                f"""
                SELECT COUNT(*) AS n FROM events
                WHERE project_id = ?
                  AND event_type IN ({placeholders})
                  AND project_seq > ?
                """,
                (project_id, *MCP_CONTEXT_EVENT_TYPES, cursor),
            ).fetchone()["n"]
        else:
            cursor = latest
            remaining = 0
        return {
            "events": events,
            "messages": events,
            "cursor": int(cursor),
            "latest_cursor": int(latest),
            "has_more": int(remaining) > 0,
            "next_after": int(cursor),
        }

    def verify_session(
        self, project_id: str, session_id: str, token: str
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._agent_dict(
                self._authenticate(connection, project_id, session_id, token)
            )

    def query_audit(
        self,
        project_id: str,
        *,
        after: int = 0,
        before: int = 0,
        limit: int = 200,
        event_type: str | None = None,
        actor_session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Page through the append-only audit log in either direction.

        ``after``/``before`` bound an exclusive window in ``project_seq``
        space (the Agent-visible event number); ``before=0`` keeps the
        historical forward-only behavior (ascending from ``after``). With
        ``before`` set, the window returns the ``limit`` events closest
        below ``before`` in ascending order, so clients can load earlier
        pages without losing their position. ``has_older`` and
        ``has_newer`` report continuation under the same filters.
        """
        self.enforce_audit_retention_throttled(project_id)
        limit = max(1, min(limit, 1000))
        after = max(0, int(after or 0))
        before = max(0, int(before or 0))
        if after and before and after >= before:
            raise DomainError(
                "conflicting_audit_window",
                "after must be below before when both bounds are provided",
                status_code=422,
            )
        clauses = ["project_id = ?"]
        parameters: list[Any] = [project_id]
        if event_type:
            clauses.append("event_type = ?")
            parameters.append(event_type)
        if actor_session_id:
            clauses.append("actor_session_id = ?")
            parameters.append(actor_session_id)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        base_where = " AND ".join(clauses)
        forward_where = f"{base_where} AND project_seq > ?"
        backward_where = f"{base_where} AND project_seq > ? AND project_seq < ?"
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            if before:
                rows = connection.execute(
                    f"""
                    SELECT * FROM events
                    WHERE {backward_where}
                    ORDER BY project_seq DESC LIMIT ?
                    """,
                    [*parameters, after, before, limit],
                ).fetchall()
                rows = list(reversed(rows))
                older_where, older_params = backward_where, [*parameters, after, before]
            else:
                rows = connection.execute(
                    f"""
                    SELECT * FROM events
                    WHERE {forward_where}
                    ORDER BY project_seq ASC LIMIT ?
                    """,
                    [*parameters, after, limit],
                ).fetchall()
                older_where, older_params = forward_where, [*parameters, after]
            events = [self._event_dict(row, connection) for row in rows]
            cursor = events[-1]["project_seq"] if events else after
            first_id = events[0]["project_seq"] if events else None
            has_newer = False
            has_older = False
            if events:
                has_newer = (
                    connection.execute(
                        f"SELECT 1 FROM events WHERE {base_where} AND project_seq > ? LIMIT 1",
                        [*parameters, cursor],
                    ).fetchone()
                    is not None
                )
                has_older = (
                    connection.execute(
                        f"SELECT 1 FROM events WHERE {older_where} AND project_seq < ? LIMIT 1",
                        [*older_params, first_id],
                    ).fetchone()
                    is not None
                )
            latest = self.latest_cursor(connection, project_id)
            return {
                "events": events,
                "cursor": cursor,
                "latest_cursor": latest,
                "has_older": has_older,
                "has_newer": has_newer,
                "filters": {
                    "event_type": event_type,
                    "actor_session_id": actor_session_id,
                    "task_id": task_id,
                },
            }

    def list_task_history(
        self,
        project_id: str,
        task_id: str,
        *,
        after: int = 0,
        before: int = 0,
        cursor: int = 0,
        limit: int = 50,
        event_type: str = "",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), TASK_HISTORY_LIMIT_MAX))
        after = max(0, int(after or 0))
        before = max(0, int(before or 0))
        cursor = max(0, int(cursor or 0))
        if cursor and after and cursor != after:
            raise DomainError(
                "conflicting_history_cursor",
                "Provide either cursor or after for forward pagination, not both",
                status_code=422,
            )
        if cursor and not after:
            # ``cursor`` is the forward-pagination alias of ``after``; both
            # mean "return events with event_id strictly greater than this".
            after = cursor
        wanted_type = str(event_type or "").strip()
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            task = self._require_task(connection, project_id, task_id)
            intake_ids = [
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM task_intakes
                    WHERE project_id = ? AND formal_task_id = ?
                    """,
                    (project_id, task_id),
                ).fetchall()
            ]
            clauses = ["project_id = ?"]
            parameters: list[Any] = [project_id]
            task_clause = "task_id = ?"
            parameters.append(task_id)
            if intake_ids:
                placeholders = _sql_placeholders(len(intake_ids))
                extractor = (
                    "(payload_json::json->>'intake_id')"
                    if getattr(self.database, "backend", "sqlite") == "postgresql"
                    else "json_extract(payload_json, '$.intake_id')"
                )
                task_clause = f"(task_id = ? OR {extractor} IN ({placeholders}))"
                parameters.extend(intake_ids)
            clauses.append(task_clause)
            if wanted_type:
                clauses.append("event_type = ?")
                parameters.append(wanted_type)
            count_sql = (
                f"SELECT COUNT(*) AS total FROM events WHERE {' AND '.join(clauses)}"
            )
            total = int(connection.execute(count_sql, parameters).fetchone()["total"])
            page_clauses = list(clauses)
            page_parameters = list(parameters)
            descending = after == 0 and before > 0
            if after:
                page_clauses.append("project_seq > ?")
                page_parameters.append(after)
            if before:
                page_clauses.append("project_seq < ?")
                page_parameters.append(before)
            order = "DESC" if descending or (before and not after) else "ASC"
            if after == 0 and before == 0:
                order = "DESC"
            page_parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE {' AND '.join(page_clauses)}
                ORDER BY project_seq {order} LIMIT ?
                """,
                page_parameters,
            ).fetchall()
            events = [self._event_dict(row, connection) for row in rows]
            if order == "DESC":
                events.reverse()
            items = self._project_task_history_items(connection, events)
            # An empty page still has to hand back the position it was asked
            # about, not the zero default, so the opposite direction stays
            # navigable instead of silently claiming there is nothing there.
            next_after = items[-1]["event_id"] if items else (after or before)
            next_before = items[0]["event_id"] if items else (before or after)
            # The Room-wide cursor moves with unrelated tasks, so forward
            # pagination bounds must be computed from THIS task's events:
            # only an event of this task after ``next_after`` means more.
            # Boundary probes rather than ``bool(items)`` plus a count proxy:
            # an empty page still has to answer honestly which side holds data,
            # or the disabled direction is the one the user just walked past.
            def _has_further(direction: str, boundary: int) -> bool:
                # ``before`` of the first event can never match, but boundary 0
                # on the forward side is a real position: it means the caller
                # walked past the oldest event and newer data does exist.
                if direction == "before" and boundary <= 1:
                    return False
                op = ">" if direction == "after" else "<"
                row = connection.execute(
                    f"SELECT 1 AS hit FROM events WHERE {' AND '.join(clauses)}"
                    f" AND project_seq {op} ? LIMIT 1",
                    [*parameters, boundary],
                ).fetchone()
                return row is not None

            latest = self.latest_cursor(connection, project_id)
            return {
                "schema_version": TASK_HISTORY_SCHEMA_VERSION,
                "task_id": task_id,
                "task_number": int(task["task_number"]),
                "items": items,
                "total": total,
                "limit": limit,
                "after": after,
                "before": before,
                "cursor": next_after,
                "next_after": next_after,
                "next_before": next_before,
                "has_more_after": _has_further("after", next_after),
                "has_more_before": _has_further("before", next_before),
                "latest_cursor": latest,
                "event_type": wanted_type,
            }

    def _project_task_history_items(
        self, connection: Any, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not events:
            return []
        session_ids = {
            str(event.get("actor_session_id") or "")
            for event in events
            if event.get("actor_session_id")
        }
        report_ids = {
            str((event.get("payload") or {}).get("report_id") or "")
            for event in events
        }
        review_ids = {
            str((event.get("payload") or {}).get("review_id") or "")
            for event in events
        }
        integration_ids = {
            str((event.get("payload") or {}).get("integration_id") or "")
            for event in events
        }
        sessions = self._rows_by_id(
            connection, "agent_sessions", session_ids, redact_token=True
        )
        member_ids = {
            str(session.get("member_id") or "")
            for session in sessions.values()
            if session.get("member_id")
        }
        members = self._rows_by_id(connection, "project_members", member_ids)
        for member in members.values():
            member["metadata"] = json_load(member.get("metadata_json"), {})
        reports = self._rows_by_id(connection, "work_reports", report_ids)
        for report in reports.values():
            report["files"] = json_load(report.get("files_json"), [])
            report["tests"] = json_load(report.get("tests_json"), [])
            report["system_evidence"] = json_load(
                report.get("system_evidence_json"), {}
            )
        reviews = self._rows_by_id(connection, "reviews", review_ids)
        for review in reviews.values():
            review["criteria"] = json_load(review.get("criteria_json"), [])
        integrations = self._rows_by_id(
            connection, "task_integrations", integration_ids
        )
        for integration in integrations.values():
            integration["files"] = json_load(integration.get("files_json"), [])
            integration["tests"] = json_load(integration.get("tests_json"), [])
        # Acknowledgements are stored against the physical events.id; public
        # event dicts carry project_seq only (#169), so join through the
        # (project_id, project_seq) index instead of comparing number spaces.
        project_id = str(events[0]["project_id"])
        event_seqs = [int(event["project_seq"]) for event in events]
        acknowledgements_by_event: dict[int, list[dict[str, Any]]] = {
            seq: [] for seq in event_seqs
        }
        if event_seqs:
            placeholders = _sql_placeholders(len(event_seqs))
            for row in connection.execute(
                f"""
                SELECT e.project_seq AS project_seq, a.session_id, a.created_at
                FROM event_acknowledgements a
                JOIN events e ON e.id = a.event_id
                WHERE e.project_id = ? AND e.project_seq IN ({placeholders})
                ORDER BY a.created_at
                """,
                [project_id, *event_seqs],
            ).fetchall():
                ack_session = sessions.get(str(row["session_id"]))
                if ack_session is None:
                    loaded = self._rows_by_id(
                        connection,
                        "agent_sessions",
                        {str(row["session_id"])},
                        redact_token=True,
                    )
                    sessions.update(loaded)
                    ack_session = loaded.get(str(row["session_id"]))
                    member_id = str((ack_session or {}).get("member_id") or "")
                    if member_id and member_id not in members:
                        members.update(
                            self._rows_by_id(
                                connection, "project_members", {member_id}
                            )
                        )
                        if member_id in members:
                            members[member_id]["metadata"] = json_load(
                                members[member_id].get("metadata_json"), {}
                            )
                acknowledgements_by_event[int(row["project_seq"])].append(
                    {
                        "session_id": str(row["session_id"]),
                        "created_at": row["created_at"],
                        "actor": actor_snapshot(
                            ack_session,
                            members.get(str((ack_session or {}).get("member_id") or "")),
                        ),
                    }
                )
        items = []
        for event in events:
            payload = event.get("payload") or {}
            payload_actor = payload.get("actor")
            session = sessions.get(str(event.get("actor_session_id") or ""))
            member = members.get(str((session or {}).get("member_id") or ""))
            actor = actor_snapshot(session, member)
            if (
                isinstance(payload_actor, Mapping)
                and str(payload_actor.get("name") or "").strip()
                and str(payload_actor.get("name")) != "unknown"
                and str(actor.get("name") or "unknown") == "unknown"
            ):
                # 会话行缺失（不可再反查）时退回写入时固化的操作者快照，
                # 时间线因此不依赖 agent_sessions 行存活。
                actor = redact_runtime_value(dict(payload_actor))
            related = {
                "report": reports.get(str(payload.get("report_id") or "")),
                "review": reviews.get(str(payload.get("review_id") or "")),
                "integration": integrations.get(
                    str(payload.get("integration_id") or "")
                ),
            }
            items.append(
                project_history_item(
                    event,
                    actor=actor,
                    related=related,
                    acknowledgements=acknowledgements_by_event.get(
                        int(event["project_seq"]), []
                    ),
                )
            )
        return items

    def _rows_by_id(
        self,
        connection: Any,
        table: str,
        ids: set[str],
        *,
        redact_token: bool = False,
    ) -> dict[str, dict[str, Any]]:
        allowed = {
            "agent_sessions",
            "project_members",
            "work_reports",
            "reviews",
            "task_integrations",
        }
        if table not in allowed:
            raise ValueError(f"Unsupported history lookup table: {table}")
        clean = {item for item in ids if item}
        if not clean:
            return {}
        placeholders = _sql_placeholders(len(clean))
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE id IN ({placeholders})",
            list(clean),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            data = dict(row)
            if redact_token:
                data.pop("token_hash", None)
            result[str(data["id"])] = data
        return result

    def room_sync(
        self,
        project_id: str,
        *,
        after: int = 0,
        session_id: str | None = None,
        token: str | None = None,
        mcp_context: bool = False,
        initial_inject: bool = False,
    ) -> dict[str, Any]:
        if bool(session_id) != bool(token):
            raise DomainError(
                "invalid_session_token",
                "session_id and token must be supplied together",
                status_code=401,
            )
        with self.database.connect() as connection:
            project = self._require_project(connection, project_id)
            if session_id:
                self._authenticate(connection, project_id, session_id, token or "")
            if mcp_context:
                limit = self.get_project_mcp_message_limit(
                    project_id, connection=connection
                )
                event_result = self._list_mcp_messages(
                    connection,
                    project_id,
                    after=after,
                    limit=limit,
                    session_id=session_id,
                    initial_inject=initial_inject,
                )
            else:
                event_result = self._list_events(
                    connection, project_id, after=after
                )
            snapshot = self._snapshot(connection, project_id)
            unread_count = None
            read_cursor = None
            if session_id:
                read_cursor = min(event_result["cursor"], event_result["latest_cursor"])
                unread_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS unread_count
                        FROM events WHERE project_id = ? AND project_seq > ?
                        """,
                        (project_id, read_cursor),
                    ).fetchone()["unread_count"]
                )
        if session_id and read_cursor is not None:
            # Single write connection: heartbeat lease/token renewal and
            # last_read_cursor CASE advance share one BEGIN IMMEDIATE window.
            # Empty syncs (cursor already current) skip the cursor UPDATE so
            # the sync still heartbeats without a second write transaction.
            with self.database.connect(write=True) as connection:
                self._authenticate(connection, project_id, session_id, token or "")
                now = iso_now()
                token_expires_at = (
                    utc_now()
                    + timedelta(seconds=self.settings.session_token_ttl_seconds)
                ).isoformat().replace("+00:00", "Z")
                connection.execute(
                    """
                    UPDATE agent_sessions
                    SET status = ?, last_heartbeat = ?, token_expires_at = ?
                    WHERE id = ?
                    """,
                    ("online", now, token_expires_at, session_id),
                )
                leases = connection.execute(
                    """
                    SELECT id, ttl_seconds FROM file_leases
                    WHERE project_id = ? AND session_id = ? AND released_at IS NULL
                      AND expires_at > ?
                    """,
                    (project_id, session_id, now),
                ).fetchall()
                for lease in leases:
                    lease_expires_at = (
                        utc_now() + timedelta(seconds=int(lease["ttl_seconds"]))
                    ).isoformat().replace("+00:00", "Z")
                    connection.execute(
                        "UPDATE file_leases SET expires_at = ?, renewed_at = ? WHERE id = ?",
                        (lease_expires_at, now, lease["id"]),
                    )
                stored_cursor = int(
                    connection.execute(
                        "SELECT last_read_cursor FROM agent_sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()["last_read_cursor"]
                    or 0
                )
                if read_cursor > stored_cursor:
                    connection.execute(
                        """
                        UPDATE agent_sessions
                        SET last_read_cursor = CASE
                            WHEN last_read_cursor > ? THEN last_read_cursor
                            ELSE ?
                        END
                        WHERE id = ?
                        """,
                        (read_cursor, read_cursor, session_id),
                    )
            for agent in snapshot["agents"]:
                if agent["id"] == session_id:
                    agent["last_read_cursor"] = max(
                        int(agent.get("last_read_cursor") or 0), read_cursor
                    )
                    agent["unread_count"] = unread_count or 0
                    agent["last_heartbeat"] = now
                    agent["status"] = "online"
                    agent["token_expires_at"] = token_expires_at
        return {
            "snapshot": snapshot,
            **event_result,
            "unread_count": unread_count,
        }

    @idempotent_write("message.post")
    def post_message(
        self,
        project_id: str,
        *,
        body: str,
        kind: str = "message",
        session_id: str | None = None,
        token: str | None = None,
        model_display_name: str | None = None,
        task_id: str | None = None,
        mentions: list[str] | None = None,
        files: list[str] | None = None,
        requires_ack: bool = False,
        priority: int = 2,
        channel: str = "public",
    ) -> dict[str, Any]:
        if kind not in MESSAGE_KINDS:
            raise DomainError("invalid_message_kind", "Unsupported message kind")
        if not body.strip():
            raise DomainError("empty_message", "Message body is required")
        if len(body) > self.settings.message_max_body_length:
            raise DomainError(
                "message_body_too_long",
                "Message body exceeds message_max_body_length "
                f"({self.settings.message_max_body_length})",
                details={"limit": self.settings.message_max_body_length},
            )
        if not 0 <= priority <= 4:
            raise DomainError("invalid_priority", "Priority must be between 0 and 4")
        if channel not in MESSAGE_CHANNELS:
            raise DomainError("invalid_message_channel", "Unsupported message channel")
        clean_model_display_name = str(model_display_name or "").strip()
        if len(clean_model_display_name) > MODEL_DISPLAY_NAME_MAX_LENGTH:
            raise DomainError(
                "invalid_model_display_name",
                f"Model display name must be at most {MODEL_DISPLAY_NAME_MAX_LENGTH} characters",
            )
        resolved_channel = "system" if kind == "system" else channel
        if task_id and channel == "public":
            resolved_channel = "task"
        list_limit = int(getattr(self.settings, "list_input_max_count", 100))
        clean_files = self._require_list_input_limit(
            "files",
            sorted({normalize_evidence_path(path) for path in (files or [])}),
            limit=list_limit,
        )
        clean_mentions = self._require_list_input_limit(
            "mentions",
            list(dict.fromkeys(mentions or [])),
            limit=list_limit,
        )
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            if session_id:
                self._authenticate(connection, project_id, session_id, token or "")
                if not clean_model_display_name:
                    raise DomainError(
                        "message_model_required",
                        "Agent messages must include the model name shown in the client UI",
                    )
            elif clean_model_display_name:
                raise DomainError(
                    "message_model_requires_session",
                    "A message model display name requires an Agent session",
                )
            if task_id:
                self._require_task(connection, project_id, task_id)
            event_id = self._emit(
                connection,
                project_id,
                f"message.{kind}",
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "schema_version": DOMAIN_SCHEMA_VERSION,
                    "body": body.strip(),
                    "model_display_name": clean_model_display_name or None,
                    "mentions": clean_mentions,
                    "files": clean_files,
                    "requires_ack": requires_ack,
                    "priority": priority,
                    "channel": resolved_channel,
                },
            )
            return {
                "event_id": event_id,
                "cursor": event_id,
                "model_display_name": clean_model_display_name or None,
            }

    @idempotent_write("event.acknowledge")
    def acknowledge_event(
        self,
        project_id: str,
        event_id: int,
        session_id: str,
        token: str,
    ) -> dict[str, Any]:
        """Acknowledge by Agent-visible ``event_id`` (= ``project_seq``).

        Cross-project or unknown numbers raise ``event_not_found``; the
        physical global id is never accepted as a reference (no ambiguous
        dual mapping during transition).
        """
        project_seq = max(0, int(event_id or 0))
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            event = connection.execute(
                "SELECT * FROM events WHERE project_seq = ? AND project_id = ?",
                (project_seq, project_id),
            ).fetchone()
            if event is None:
                raise DomainError("event_not_found", "Event does not exist", status_code=404)
            physical_event_id = int(event["id"])
            payload = json_load(event["payload_json"], {})
            if not payload.get("requires_ack"):
                raise DomainError(
                    "acknowledgement_not_required",
                    "This event does not require acknowledgement",
                    status_code=409,
                )
            inserted = connection.execute(
                """
                INSERT INTO event_acknowledgements(event_id, session_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id, session_id) DO NOTHING
                """,
                (physical_event_id, session_id, iso_now()),
            ).rowcount
            if inserted != 1:
                # Duplicate acknowledge: keep append-only history stable and
                # do not emit another message.acknowledged (style matches
                # release_lease's already_released).
                return {
                    "acknowledged": True,
                    "already_acknowledged": True,
                    "acknowledged_event_id": project_seq,
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            cursor = self._emit(
                connection,
                project_id,
                "message.acknowledged",
                actor_session_id=session_id,
                task_id=event["task_id"],
                payload={"event_id": project_seq},
            )
            return {
                "acknowledged": True,
                "acknowledged_event_id": project_seq,
                "event_id": cursor,
                "cursor": cursor,
            }

    def list_task_intake_targets(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """
                SELECT a.*, (
                    SELECT MAX(e.created_at)
                    FROM events e
                    WHERE e.project_id = a.project_id
                      AND e.actor_session_id = a.id
                ) AS last_activity_at
                FROM agent_sessions a
                WHERE a.project_id = ?
                ORDER BY a.created_at
                """,
                (project_id,),
            ).fetchall()
            agents = [self._agent_dict(row) for row in rows]
            members = [
                self._member_dict(row)
                for row in connection.execute(
                    "SELECT * FROM project_members WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            ]
            eligible_member_ids = {
                str(member["id"])
                for member in members
                if member["status"] != "revoked"
            }
            targets = self._agent_identities(agents, members=members)
            # A task intake targets an Agent identity, not a live connection.
            # Keep every non-revoked member with historical sessions selectable
            # so users can queue work for an Agent that is currently offline.
            return [
                target
                for target in targets
                if target["member_id"] in eligible_member_ids
            ]

    @idempotent_write("task.intake.submit")
    def submit_task_intake(
        self,
        project_id: str,
        *,
        raw_description: str,
        target_member_id: str,
        target_session_id: str | None = None,
        created_by_session_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        clean_description = raw_description.strip()
        clean_member_id = target_member_id.strip()
        clean_session_id = target_session_id.strip() if target_session_id else None
        if not clean_description:
            raise DomainError(
                "invalid_task_intake", "A raw task description is required"
            )
        if len(clean_description) > self.settings.task_text_max_length:
            raise DomainError(
                "task_text_too_long",
                "Task intake raw_description exceeds task_text_max_length "
                f"({self.settings.task_text_max_length})",
                status_code=422,
                details={
                    "field": "raw_description",
                    "limit": self.settings.task_text_max_length,
                },
            )
        if not clean_member_id:
            raise DomainError(
                "task_intake_target_required", "An Agent target is required"
            )
        intake_id = new_id("intake")
        now = iso_now()
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            if created_by_session_id:
                self._authenticate(
                    connection, project_id, created_by_session_id, token or ""
                )
            member = self._require_intake_target(
                connection, project_id, clean_member_id, clean_session_id
            )
            if clean_session_id is None:
                candidates = connection.execute(
                    """
                    SELECT * FROM agent_sessions
                    WHERE project_id = ? AND member_id = ? AND left_at IS NULL
                    ORDER BY last_heartbeat DESC, created_at DESC
                    """,
                    (project_id, clean_member_id),
                ).fetchall()
                for candidate in candidates:
                    if self._agent_dict(candidate)["status"] != "offline":
                        clean_session_id = str(candidate["id"])
                        break
            # An offline member remains a valid target; in that case the
            # identity is stored without a session until it reconnects.
            connection.execute(
                """
                INSERT INTO task_intakes(
                    id, project_id, raw_description, target_member_id,
                    target_session_id, created_by_session_id, status, formal_task_id,
                    note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, '', ?, ?)
                """,
                (
                    intake_id,
                    project_id,
                    clean_description,
                    member["id"],
                    clean_session_id,
                    created_by_session_id,
                    now,
                    now,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "task.intake_submitted",
                actor_session_id=created_by_session_id,
                payload={
                    "intake_id": intake_id,
                    "raw_description": clean_description,
                    "target_member_id": member["id"],
                    "target_session_id": clean_session_id,
                    "requires_agent_acceptance": True,
                },
            )
            row = self._require_task_intake(connection, project_id, intake_id)
            return {
                "intake": self._task_intake_dict(row),
                "event_id": event_id,
                "cursor": event_id,
            }

    def list_task_intakes(
        self, project_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            if status is not None and status not in TASK_INTAKE_STATUSES:
                raise DomainError(
                    "invalid_task_intake_status", "Unsupported intake status"
                )
            clause = "project_id = ?"
            parameters: list[Any] = [project_id]
            if status:
                clause += " AND status = ?"
                parameters.append(status)
            rows = connection.execute(
                f"SELECT * FROM task_intakes WHERE {clause} ORDER BY created_at ASC, id ASC",
                parameters,
            ).fetchall()
            return [self._task_intake_dict(row) for row in rows]

    def get_task_intake(self, project_id: str, intake_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._task_intake_dict(
                self._require_task_intake(connection, project_id, intake_id)
            )

    @idempotent_write("task.intake.reassign")
    def reassign_task_intake(
        self,
        project_id: str,
        intake_id: str,
        *,
        target_member_id: str,
        target_session_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        clean_member_id = target_member_id.strip()
        clean_session_id = target_session_id.strip() if target_session_id else None
        if not clean_member_id:
            raise DomainError(
                "task_intake_target_required", "An Agent target is required"
            )
        with self.database.connect(write=True) as connection:
            intake = self._require_task_intake(connection, project_id, intake_id)
            previous_status = str(intake["status"])
            if intake["status"] == "defined":
                raise DomainError(
                    "task_intake_already_defined",
                    "A defined intake must be reassigned through its formal task",
                    status_code=409,
                )
            if intake["status"] == "cancelled":
                raise DomainError(
                    "task_intake_cancelled", "A cancelled intake cannot be reassigned", status_code=409
                )
            self._transition_task_intake(connection, intake["status"], "pending")
            member = self._require_intake_target(
                connection, project_id, clean_member_id, clean_session_id
            )
            if clean_session_id is None:
                candidates = connection.execute(
                    """
                    SELECT * FROM agent_sessions
                    WHERE project_id = ? AND member_id = ? AND left_at IS NULL
                    ORDER BY last_heartbeat DESC, created_at DESC
                    """,
                    (project_id, clean_member_id),
                ).fetchall()
                for candidate in candidates:
                    if self._agent_dict(candidate)["status"] != "offline":
                        clean_session_id = str(candidate["id"])
                        break
            # An offline member remains a valid target; in that case the
            # identity is stored without a session until it reconnects.
            now = iso_now()
            updated_rows = connection.execute(
                """
                UPDATE task_intakes
                SET target_member_id = ?, target_session_id = ?, status = 'pending',
                    note = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = ?
                """,
                (
                    member["id"],
                    clean_session_id,
                    note.strip(),
                    now,
                    intake_id,
                    project_id,
                    previous_status,
                ),
            )
            if updated_rows.rowcount != 1:
                raise DomainError(
                    "task_modified_concurrently",
                    "Task intake was modified concurrently; reload the intake and retry",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "task.intake_reassigned",
                payload={
                    "intake_id": intake_id,
                    "previous_status": previous_status,
                    "status": "pending",
                    "target_member_id": member["id"],
                    "target_session_id": clean_session_id,
                    "note": note.strip(),
                },
            )
            updated = self._require_task_intake(connection, project_id, intake_id)
            return {
                "intake": self._task_intake_dict(updated),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.intake.acknowledge")
    def acknowledge_task_intake(
        self,
        project_id: str,
        intake_id: str,
        *,
        session_id: str,
        token: str,
        response: str = "accepted",
        note: str = "",
    ) -> dict[str, Any]:
        if response not in {"accepted", "declined", "blocked"}:
            raise DomainError(
                "invalid_task_intake_response",
                "Intake response must be accepted, declined, or blocked",
            )
        if response != "accepted" and not note.strip():
            raise DomainError(
                "intake_response_note_required",
                "Declined or blocked intake responses require a note",
            )
        with self.database.connect(write=True) as connection:
            agent = self._authenticate(connection, project_id, session_id, token)
            intake = self._require_task_intake(connection, project_id, intake_id)
            if intake["target_member_id"] != agent["member_id"]:
                raise DomainError(
                    "task_intake_not_for_agent",
                    "This Agent is not the selected intake target",
                    status_code=403,
                )
            if intake["status"] != "pending":
                if (
                    intake["status"] == response
                    and intake["target_session_id"] == session_id
                ):
                    return {
                        "intake": self._task_intake_dict(intake),
                        "event_id": None,
                        "cursor": self.latest_cursor(connection, project_id),
                    }
                raise DomainError(
                    "task_intake_already_acknowledged",
                    "Task intake already has a response",
                    status_code=409,
                )
            next_status = response
            self._transition_task_intake(connection, intake["status"], next_status)
            now = iso_now()
            cursor = connection.execute(
                """
                UPDATE task_intakes
                SET target_session_id = ?, status = ?, note = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'pending'
                """,
                (session_id, next_status, note.strip(), now, intake_id, project_id),
            )
            if cursor.rowcount != 1:
                raise DomainError(
                    "task_intake_already_acknowledged",
                    "Task intake already has a response",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "task.intake_acknowledged",
                actor_session_id=session_id,
                payload={
                    "intake_id": intake_id,
                    "response": response,
                    "note": note.strip(),
                    "target_session_id": session_id,
                },
            )
            updated = self._require_task_intake(connection, project_id, intake_id)
            return {
                "intake": self._task_intake_dict(updated),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.intake.define")
    def define_task_from_intake(
        self,
        project_id: str,
        intake_id: str,
        *,
        session_id: str,
        token: str,
        title: str,
        description: str = "",
        acceptance_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
        priority: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            agent = self._authenticate(connection, project_id, session_id, token)
            intake = self._require_task_intake(connection, project_id, intake_id)
            if intake["target_member_id"] != agent["member_id"]:
                raise DomainError(
                    "task_intake_not_for_agent",
                    "Only the selected Agent can define this intake",
                    status_code=403,
                )
            if intake["status"] != "accepted":
                raise DomainError(
                    "task_intake_not_accepted",
                    "The intake must be accepted before its formal task can be defined",
                    status_code=409,
                )
            # Optimistic lock BEFORE creating the task: two concurrent defines
            # must not both mint formal tasks (the loser must not leave an
            # orphan duplicate behind).
            self._transition_task_intake(connection, intake["status"], "defining")
            claim = connection.execute(
                """
                UPDATE task_intakes
                SET status = 'defining', updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'accepted'
                """,
                (iso_now(), intake_id, project_id),
            )
            if claim.rowcount != 1:
                raise DomainError(
                    "task_intake_already_defined",
                    "This intake has already been defined into a formal task",
                    status_code=409,
                )
            created = self.create_task(
                project_id,
                title=title,
                description=description,
                acceptance_criteria=acceptance_criteria,
                depends_on=depends_on,
                priority=priority,
                actor_session_id=session_id,
                token=token,
            )
            task = created["task"]
            now = iso_now()
            self._transition_task_intake(connection, "defining", "defined")
            defined_rows = connection.execute(
                """
                UPDATE task_intakes
                SET status = 'defined', formal_task_id = ?, note = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'defining'
                """,
                (task["id"], note.strip(), now, intake_id, project_id),
            )
            if defined_rows.rowcount != 1:
                raise DomainError(
                    "task_modified_concurrently",
                    "Task intake was modified concurrently; the formal task was not linked",
                    status_code=409,
                )
            defined_event_id = self._emit(
                connection,
                project_id,
                "task.intake_defined",
                actor_session_id=session_id,
                task_id=task["id"],
                payload={
                    "intake_id": intake_id,
                    "task_number": task["task_number"],
                    "note": note.strip(),
                },
            )
            assignment = self.assign_task(
                project_id,
                task["id"],
                assigned_by_session_id=session_id,
                token=token,
                assigned_to_session_id=session_id,
                note="Agent-defined task is ready for execution",
            )
            updated = self._require_task_intake(connection, project_id, intake_id)
            return {
                "intake": self._task_intake_dict(updated),
                "task": self.get_task(project_id, task["id"]),
                "assignment": assignment["assignment"],
                "event_id": defined_event_id,
                "cursor": defined_event_id,
            }

    @idempotent_write("task.create")
    def create_task(
        self,
        project_id: str,
        *,
        title: str,
        description: str = "",
        acceptance_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
        priority: int | None = None,
        actor_session_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise DomainError("invalid_task", "Task title is required")
        text_limit = self.settings.task_text_max_length
        for field_name, value in (
            ("title", title),
            ("description", description),
        ):
            if len(str(value)) > text_limit:
                raise DomainError(
                    "task_text_too_long",
                    f"Task {field_name} exceeds task_text_max_length ({text_limit})",
                    details={"field": field_name, "limit": text_limit},
                )
        priority = require_int_field(priority, "priority")
        if priority is not None and not 0 <= priority <= 4:
            raise DomainError("invalid_priority", "Priority must be between 0 and 4")
        list_limit = int(getattr(self.settings, "list_input_max_count", 100))
        criteria = self._require_list_input_limit(
            "acceptance_criteria",
            [item.strip() for item in (acceptance_criteria or []) if item.strip()],
            limit=list_limit,
        )
        for item in criteria:
            if len(item) > self.settings.task_text_max_length:
                raise DomainError(
                    "task_text_too_long",
                    "Acceptance criterion exceeds task_text_max_length "
                    f"({self.settings.task_text_max_length})",
                    details={"field": "acceptance_criteria", "limit": self.settings.task_text_max_length},
                )
        if not criteria:
            raise DomainError(
                "missing_acceptance_criteria",
                "At least one acceptance criterion is required",
            )
        dependencies = self._require_list_input_limit(
            "depends_on",
            list(dict.fromkeys(depends_on or [])),
            limit=list_limit,
        )
        now = iso_now()
        task_id = new_id("task")
        with self.database.connect(write=True) as connection:
            project_row = self._require_project(connection, project_id)
            if priority is None:
                # 未显式指定时取项目设置的默认任务优先级（default_task_priority）。
                settings = json_load(project_row["settings_json"], {})
                priority = int(
                    (settings or {}).get(
                        "default_task_priority",
                        PROJECT_SETTINGS_DEFAULTS["default_task_priority"],
                    )
                )
                if not 0 <= priority <= 4:
                    priority = PROJECT_SETTINGS_DEFAULTS["default_task_priority"]
            if not actor_session_id:
                # 正式任务必须能追溯到创建者；无操作者的创建一律显式拒绝，
                # 不再静默写入 actor 为 NULL 的任务与事件。
                raise DomainError(
                    "missing_actor_session",
                    "Task creation requires the authenticated Agent session of the operator",
                )
            self._authenticate(connection, project_id, actor_session_id, token or "")
            for dependency_id in dependencies:
                self._require_satisfiable_dependency(connection, project_id, dependency_id)
            task_number = self._next_task_number(connection, project_id)
            try:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id, project_id, task_number, title, description,
                        acceptance_criteria_json, priority, status, execution_status,
                        verification_status, integration_status, created_by_session_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'todo', 'todo', 'not_required',
                              'pending', ?, ?, ?)
                    """,
                    (
                        task_id,
                        project_id,
                        task_number,
                        title.strip(),
                        description.strip(),
                        json_dump(criteria),
                        priority,
                        actor_session_id,
                        now,
                        now,
                    ),
                )
            except Exception as error:
                if _is_unique_constraint_error(error):
                    raise DomainError(
                        "task_number_conflict",
                        "Could not allocate a unique task number",
                        status_code=409,
                    ) from error
                raise
            for dependency_id in dependencies:
                connection.execute(
                    """
                    INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (task_id, dependency_id, now),
                )
            event_id = self._emit(
                connection,
                project_id,
                "task.created",
                actor_session_id=actor_session_id,
                task_id=task_id,
                payload={
                    "task_number": task_number,
                    "title": title.strip(),
                    "priority": priority,
                    "depends_on": dependencies,
                },
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return {
                "task": self._task_with_dependencies(connection, row),
                "event_id": event_id,
                "cursor": event_id,
            }

    def list_tasks(
        self,
        project_id: str,
        status: str | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            tasks = self._list_tasks(connection, project_id, status=status)
        if phase:
            wanted = str(phase).strip()
            if wanted == "attention":
                tasks = [
                    task
                    for task in tasks
                    if task["state_view"]["needs_attention"]
                ]
            else:
                tasks = [
                    task
                    for task in tasks
                    if task["state_view"]["phase"] == wanted
                ]
        return tasks

    def _list_tasks(
        self,
        connection: Any,
        project_id: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE project_id = ? AND status = ?
                ORDER BY priority ASC, created_at ASC
                """,
                (project_id, status),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE project_id = ?
                ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END,
                         priority ASC, created_at ASC
                """,
                (project_id,),
            ).fetchall()
        return self._tasks_with_relations(connection, rows)

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        """Return one complete task without requiring a large project list."""
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            task = self._require_task(connection, project_id, task_id)
            return self._task_with_dependencies(connection, task)

    def get_task_with_documents(self, project_id: str, task_id: str) -> dict[str, Any]:
        """Task contract plus the current binding-document injection payload."""
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            task = self._require_task(connection, project_id, task_id)
            return {
                "task": self._task_with_dependencies(connection, task),
                "project_documents": self.injectable_project_documents(
                    project_id, connection=connection
                ),
            }

    def get_task_by_number(self, project_id: str, task_number: int) -> dict[str, Any]:
        """Resolve a human-readable Project task number to the full task."""
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            task = self._require_task_by_number(connection, project_id, task_number)
            return self._task_with_dependencies(connection, task)

    def _unfinished_dependencies(self, connection: Any, task_id: str) -> list[Any]:
        return connection.execute(
            """
            SELECT t.id, t.title, t.status, t.execution_status,
                   t.verification_status, t.integration_status
            FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ?
              AND NOT (
                  t.verification_status = 'approved'
                  AND t.integration_status = 'done'
              )
            """,
            (task_id,),
        ).fetchall()

    def _require_finished_dependencies(self, connection: Any, task_id: str) -> None:
        """Gate entry into work, whatever door is used.

        Claim, assignment acceptance and handoff acceptance all move a task into
        ``claimed``. The dependency rule lived only in ``claim_task``, so
        assigning a blocked task and having the target accept it stepped straight
        past the gate, and the task then read as claimed and could be reported
        and reviewed with its dependencies unfinished.
        """
        blockers = self._unfinished_dependencies(connection, task_id)
        if blockers:
            raise DomainError(
                "task_dependencies_incomplete",
                "Task has unfinished dependencies",
                status_code=409,
                details={"blockers": [dict(row) for row in blockers]},
            )

    def _require_satisfiable_dependency(
        self, connection: Any, project_id: str, dependency_id: str
    ) -> None:
        """Reject a dependency that can never be satisfied.

        The gate a dependency has to clear is ``approved`` plus ``integrated``.
        A cancelled task can never reach it again - cancelled is terminal - so
        depending on one turned the new task into a permanent
        ``task_dependencies_incomplete``, discovered only when someone tried to
        claim it. Say so at the moment the dependency is wired up instead.
        """
        dependency = self._require_task(connection, project_id, dependency_id)
        if dependency["execution_status"] != "cancelled":
            return
        raise DomainError(
            "invalid_dependency_status",
            f"Task #{dependency['task_number']} is cancelled and can never "
            "satisfy a dependency",
            status_code=422,
            details={
                "depends_on_task_id": dependency_id,
                "task_number": dependency["task_number"],
                "execution_status": dependency["execution_status"],
                "required_action": "drop_the_cancelled_dependency_or_pick_another",
            },
        )

    @idempotent_write("task.claim")
    def claim_task(
        self,
        project_id: str,
        task_id: str,
        session_id: str,
        token: str,
        *,
        reclaim: bool = False,
        takeover: bool = False,
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            claimant = self._authenticate(connection, project_id, session_id, token)
            task = self._require_task(connection, project_id, task_id)
            if task["owner_session_id"] == session_id:
                return {
                    "task": self._task_with_dependencies(connection, task),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            if task["owner_session_id"] is not None:
                liveness = self._owner_liveness(
                    connection, project_id, str(task["owner_session_id"])
                )
                owner = liveness["owner"]
                can_reclaim = bool(
                    owner is not None
                    and self._same_agent_identity(claimant, owner)
                    # Unfinished execution states are reclaimable once the owner
                    # is disconnected: claimed, in_progress, and blocked.
                    and task["execution_status"] in {"claimed", "in_progress", "blocked"}
                )
                if not (reclaim or takeover):
                    raise DomainError(
                        "task_already_claimed",
                        "Task is already owned by another Session",
                        status_code=409,
                        details={
                            "owner_session_id": task["owner_session_id"],
                            "status": task["status"],
                            "can_reclaim_if_disconnected": can_reclaim,
                            "can_explicitly_take_over": can_reclaim,
                        },
                    )
                if not can_reclaim or owner is None:
                    raise DomainError(
                        "task_reclaim_forbidden",
                        "Only the same software identity can reclaim unfinished execution work",
                        status_code=403,
                    )
                last_seen = liveness["last_seen"]
                owner_connected = bool(liveness["connected"])
                transport_state = str(liveness["transport_state"])
                if owner_connected and not takeover:
                    # An ordinary reclaim never preempts a live owner.  A same-
                    # identity caller can choose an explicit, audited takeover
                    # when the transport/heartbeat is a ghost or continuation
                    # in the old conversation is impossible.
                    stale_after = last_seen + timedelta(
                        seconds=self.settings.heartbeat_timeout_seconds
                    )
                    retry_after = max(
                        1, int((stale_after - utc_now()).total_seconds() + 0.999)
                    )
                    raise DomainError(
                        "task_owner_session_connected",
                        "The owning Session is still connected; restore that conversation, or explicitly take over its task and leases with takeover=true",
                        status_code=409,
                        details={
                            "owner_session_id": task["owner_session_id"],
                            "owner_last_seen_at": last_seen.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "owner_considered_disconnected_at": stale_after.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "retry_after_seconds": retry_after,
                            "owner_transport_state": transport_state,
                            "heartbeat_timeout_seconds": self.settings.heartbeat_timeout_seconds,
                            "required_action": "restore_owner_or_explicit_takeover",
                            "optional_wait_seconds": retry_after,
                            "explicit_takeover": "task_claim(takeover=true)",
                            "forbidden_bypass": "task_update(status=todo)",
                        },
                    )
                now = iso_now()
                previous_owner = str(task["owner_session_id"])
                updated_reclaim = connection.execute(
                    "UPDATE tasks SET owner_session_id = ?, updated_at = ? WHERE id = ? AND project_id = ? AND owner_session_id = ?",
                    (session_id, now, task_id, project_id, previous_owner),
                )
                if updated_reclaim.rowcount != 1:
                    raise DomainError(
                        "task_already_claimed",
                        "Task was modified concurrently",
                        status_code=409,
                    )
                leases = connection.execute(
                    "SELECT id FROM file_leases WHERE project_id = ? AND task_id = ? AND session_id = ? AND released_at IS NULL",
                    (project_id, task_id, previous_owner),
                ).fetchall()
                lease_ids = [str(row["id"]) for row in leases]
                connection.execute(
                    "UPDATE file_leases SET session_id = ?, renewed_at = ? WHERE project_id = ? AND task_id = ? AND session_id = ? AND released_at IS NULL",
                    (session_id, now, project_id, task_id, previous_owner),
                )
                event_id = self._emit(
                    connection,
                    project_id,
                    "task.reclaimed",
                    actor_session_id=session_id,
                    task_id=task_id,
                    payload={
                        "task_number": task["task_number"],
                        "previous_owner_session_id": previous_owner,
                        "owner_session_id": session_id,
                        "transferred_lease_ids": lease_ids,
                        "execution_status": task["execution_status"],
                        "explicit_live_takeover": bool(takeover and owner_connected),
                    },
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                return {
                    "task": self._task_with_dependencies(connection, row),
                    "project_documents": self.injectable_project_documents(
                        project_id, connection=connection
                    ),
                    "event_id": event_id,
                    "cursor": event_id,
                    "explicit_live_takeover": bool(takeover and owner_connected),
                }
            if task["execution_status"] != "todo":
                raise DomainError(
                    "task_already_claimed",
                    "Task is not available for claiming",
                    status_code=409,
                    details={
                        "owner_session_id": task["owner_session_id"],
                        "status": task["status"],
                    },
                )
            self._require_finished_dependencies(connection, task_id)
            now = iso_now()
            updated = connection.execute(
                """
                UPDATE tasks
                SET owner_session_id = ?, status = 'claimed',
                    execution_status = 'claimed', updated_at = ?
                WHERE id = ? AND project_id = ? AND owner_session_id IS NULL
                  AND execution_status = 'todo'
                """,
                (session_id, now, task_id, project_id),
            )
            if updated.rowcount != 1:
                raise DomainError(
                    "task_already_claimed", "Task was claimed concurrently", status_code=409
                )
            event_id = self._emit(
                connection,
                project_id,
                "task.claimed",
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "task_number": task["task_number"],
                    "previous_owner_session_id": None,
                    "owner_session_id": session_id,
                    "from_status": task["status"],
                    "status": "claimed",
                    "from_execution_status": task["execution_status"],
                    "execution_status": "claimed",
                    "from_verification_status": task["verification_status"],
                    "project_documents": self._binding_document_snapshot(
                        connection, project_id
                    ),
                    "verification_status": task["verification_status"],
                    "from_integration_status": task["integration_status"],
                    "integration_status": task["integration_status"],
                },
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return {
                "task": self._task_with_dependencies(connection, row),
                "project_documents": self.injectable_project_documents(
                    project_id, connection=connection
                ),
                "event_id": event_id,
                "cursor": event_id,
            }

    def _cancel_task_resources_locked(
        self,
        connection: Any,
        project_id: str,
        task_id: str,
        actor_session_id: str | None,
    ) -> dict[str, Any]:
        """Release active leases and cancel pending assignments/handoffs when
        a task is cancelled. Runs inside an open write transaction so the
        terminal state never leaves orphan resources behind."""
        now = iso_now()
        # A Session-driven cancellation only cleans up what that Session owns.
        # Another Agent's exclusive lease on the task must survive: before this
        # was scoped, cancelling an unowned task released leases held by an
        # unrelated Agent that had bound work to it. A management cancellation
        # carries no actor Session and keeps the documented whole-task sweep.
        actor_clause = ""
        lease_params: tuple[Any, ...] = (project_id, task_id)
        if actor_session_id:
            actor_clause = " AND session_id = ?"
            lease_params = (project_id, task_id, actor_session_id)
        active_leases = connection.execute(
            f"""
            SELECT id FROM file_leases
            WHERE project_id = ? AND task_id = ? AND released_at IS NULL{actor_clause}
            """,
            lease_params,
        ).fetchall()
        released_lease_ids = [row["id"] for row in active_leases]
        if released_lease_ids:
            placeholders = _sql_placeholders(len(released_lease_ids))
            connection.execute(
                f"""
                UPDATE file_leases SET released_at = ?
                WHERE project_id = ? AND task_id = ? AND released_at IS NULL
                  AND id IN ({placeholders})
                """,
                (now, project_id, task_id, *released_lease_ids),
            )
        task_number_row = connection.execute(
            "SELECT task_number FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        ).fetchone()
        task_number = int(task_number_row["task_number"]) if task_number_row else 0
        pending_assignments = connection.execute(
            """
            SELECT id, assigned_to_session_id FROM task_assignments
            WHERE project_id = ? AND task_id = ? AND status = 'pending'
            """,
            (project_id, task_id),
        ).fetchall()
        cancelled_assignment_ids = [row["id"] for row in pending_assignments]
        if cancelled_assignment_ids:
            placeholders = _sql_placeholders(len(cancelled_assignment_ids))
            connection.execute(
                f"""
                UPDATE task_assignments
                SET status = 'cancelled', responded_by_session_id = ?,
                    response_note = 'cancelled by task cancellation', responded_at = ?
                WHERE project_id = ? AND task_id = ?
                  AND status = 'pending' AND id IN ({placeholders})
                """,
                (
                    actor_session_id,
                    now,
                    project_id,
                    task_id,
                    *cancelled_assignment_ids,
                ),
            )
            # Release and reassignment already emit one event per invalidated
            # row; cancellation has to as well, or the event-sourced timelines
            # show an assignment vanishing on release but not on cancellation.
            for row in pending_assignments:
                self._emit(
                    connection,
                    project_id,
                    "task.assignment_cancelled",
                    actor_session_id=actor_session_id,
                    task_id=task_id,
                    payload={
                        "assignment_id": str(row["id"]),
                        "by": "task_cancellation",
                        "previous_target_session_id": str(
                            row["assigned_to_session_id"] or ""
                        ),
                        "task_number": task_number,
                    },
                )
        pending_handoffs = connection.execute(
            """
            SELECT id, to_session_id FROM task_handoffs
            WHERE project_id = ? AND task_id = ? AND status = 'pending'
            """,
            (project_id, task_id),
        ).fetchall()
        cancelled_handoff_ids = [row["id"] for row in pending_handoffs]
        if cancelled_handoff_ids:
            placeholders = _sql_placeholders(len(cancelled_handoff_ids))
            connection.execute(
                f"""
                UPDATE task_handoffs
                SET status = 'cancelled', responded_by_session_id = ?,
                    response_note = 'cancelled by task cancellation', responded_at = ?
                WHERE project_id = ? AND task_id = ?
                  AND status = 'pending' AND id IN ({placeholders})
                """,
                (
                    actor_session_id,
                    now,
                    project_id,
                    task_id,
                    *cancelled_handoff_ids,
                ),
            )
            for row in pending_handoffs:
                self._emit(
                    connection,
                    project_id,
                    "task.handoff_cancelled",
                    actor_session_id=actor_session_id,
                    task_id=task_id,
                    payload={
                        "handoff_id": str(row["id"]),
                        "by": "task_cancellation",
                        "previous_target_session_id": str(row["to_session_id"] or ""),
                        "task_number": task_number,
                    },
                )
        return {
            "released_lease_ids": released_lease_ids,
            "cancelled_assignment_ids": cancelled_assignment_ids,
            "cancelled_handoff_ids": cancelled_handoff_ids,
        }

    def _release_task_locked(
        self,
        connection: Any,
        project_id: str,
        task: Mapping[str, Any],
        *,
        reason_code: str,
        reason: str,
        initiator: str,
        actor_session_id: str | None,
    ) -> dict[str, Any]:
        """Shared release core; runs inside an open write transaction.

        Release moves an owned claimed / in_progress / blocked task back to the
        claimable pool without touching task contracts, progress, evidence, or
        append-only history. Cancellation is a different, terminal action.
        """
        clean_reason_code = str(reason_code or "").strip()
        clean_reason = str(reason or "").strip()
        if clean_reason_code not in TASK_RELEASE_REASON_CODES:
            raise DomainError(
                "invalid_release_reason_code",
                "Release reason code must be one of: "
                + ", ".join(sorted(TASK_RELEASE_REASON_CODES)),
                status_code=422,
                details={"allowed": sorted(TASK_RELEASE_REASON_CODES)},
            )
        previous_owner = task["owner_session_id"]
        if previous_owner is None and task["execution_status"] == "todo":
            return {
                "task": self._task_with_dependencies(connection, task),
                "released": False,
                "already_released": True,
                "reason_code": clean_reason_code,
                "reason": clean_reason,
                "released_lease_ids": [],
                "invalidated_assignment_ids": [],
                "invalidated_handoff_ids": [],
                "event_id": None,
                "cursor": self.latest_cursor(connection, project_id),
            }
        if task["execution_status"] not in TASK_RELEASE_EXECUTION_STATUSES:
            raise DomainError(
                "task_not_releasable",
                "Only claimed, in_progress, or blocked tasks can be released",
                status_code=409,
                details={
                    "execution_status": task["execution_status"],
                    "verification_status": task["verification_status"],
                    "integration_status": task["integration_status"],
                    "legacy_status": task["status"],
                },
            )
        if previous_owner is None:
            raise DomainError(
                "task_not_releasable",
                "Task has no owner to release",
                status_code=409,
                details={"execution_status": task["execution_status"]},
            )
        (
            next_execution_status,
            next_verification_status,
            next_integration_status,
        ) = task_state_for_legacy_status(
            "todo", previous_verification=task["verification_status"]
        )
        now = iso_now()
        active_leases = connection.execute(
            """
            SELECT id FROM file_leases
            WHERE project_id = ? AND task_id = ? AND session_id = ?
              AND released_at IS NULL
            """,
            (project_id, task["id"], previous_owner),
        ).fetchall()
        released_lease_ids = [row["id"] for row in active_leases]
        if released_lease_ids:
            connection.execute(
                """
                UPDATE file_leases SET released_at = ?
                WHERE project_id = ? AND task_id = ? AND session_id = ?
                  AND released_at IS NULL
                """,
                (now, project_id, task["id"], previous_owner),
            )
        pending_assignments = connection.execute(
            """
            SELECT id FROM task_assignments
            WHERE project_id = ? AND task_id = ? AND status = 'pending'
            ORDER BY created_at
            """,
            (project_id, task["id"]),
        ).fetchall()
        invalidated_assignment_ids = [row["id"] for row in pending_assignments]
        if invalidated_assignment_ids:
            placeholders = _sql_placeholders(len(invalidated_assignment_ids))
            connection.execute(
                f"""
                UPDATE task_assignments
                SET status = 'cancelled', responded_by_session_id = ?,
                    response_note = ?, responded_at = ?
                WHERE project_id = ? AND task_id = ?
                  AND status = 'pending' AND id IN ({placeholders})
                """,
                (
                    actor_session_id,
                    f"cancelled by task release ({clean_reason_code})",
                    now,
                    project_id,
                    task["id"],
                    *invalidated_assignment_ids,
                ),
            )
        pending_handoffs = connection.execute(
            """
            SELECT id FROM task_handoffs
            WHERE project_id = ? AND task_id = ? AND status = 'pending'
            ORDER BY created_at
            """,
            (project_id, task["id"]),
        ).fetchall()
        invalidated_handoff_ids = [row["id"] for row in pending_handoffs]
        if invalidated_handoff_ids:
            placeholders = _sql_placeholders(len(invalidated_handoff_ids))
            connection.execute(
                f"""
                UPDATE task_handoffs
                SET status = 'cancelled', responded_by_session_id = ?,
                    response_note = ?, responded_at = ?
                WHERE project_id = ? AND task_id = ?
                  AND status = 'pending' AND id IN ({placeholders})
                """,
                (
                    actor_session_id,
                    f"cancelled by task release ({clean_reason_code})",
                    now,
                    project_id,
                    task["id"],
                    *invalidated_handoff_ids,
                ),
            )
        next_blocker_reason = (
            "" if task["status"] == "blocked" else task["blocker_reason"]
        )
        updated = connection.execute(
            """
            UPDATE tasks
            SET owner_session_id = NULL, status = 'todo',
                execution_status = 'todo', verification_status = ?,
                integration_status = ?, blocker_reason = ?, updated_at = ?
            WHERE id = ? AND project_id = ? AND owner_session_id = ?
              AND execution_status IN ('claimed', 'in_progress', 'blocked')
            """,
            (
                next_verification_status,
                next_integration_status,
                next_blocker_reason,
                now,
                task["id"],
                project_id,
                previous_owner,
            ),
        )
        if updated.rowcount != 1:
            raise DomainError(
                "task_release_conflict",
                "Task ownership changed concurrently; reload the task and retry",
                status_code=409,
            )
        event_id = self._emit(
            connection,
            project_id,
            "task.released",
            actor_session_id=actor_session_id,
            task_id=task["id"],
            payload={
                "initiator": initiator,
                "initiator_session_id": actor_session_id or "",
                "previous_owner_session_id": previous_owner,
                "reason_code": clean_reason_code,
                "reason": clean_reason,
                "from_status": task["status"],
                "status": "todo",
                "from_execution_status": task["execution_status"],
                "execution_status": next_execution_status,
                "from_verification_status": task["verification_status"],
                "verification_status": next_verification_status,
                "from_integration_status": task["integration_status"],
                "integration_status": next_integration_status,
                "progress_percent": task["progress_percent"],
                "current_step": task["current_step"],
                "next_step": task["next_step"],
                "cleared_blocker_reason": (
                    task["blocker_reason"]
                    if next_blocker_reason != task["blocker_reason"]
                    else ""
                ),
                "released_lease_ids": released_lease_ids,
                "invalidated_assignment_ids": invalidated_assignment_ids,
                "invalidated_handoff_ids": invalidated_handoff_ids,
                "task_number": task["task_number"],
            },
        )
        for assignment_id in invalidated_assignment_ids:
            self._emit(
                connection,
                project_id,
                "task.assignment_cancelled",
                actor_session_id=actor_session_id,
                task_id=task["id"],
                payload={
                    "assignment_id": assignment_id,
                    "by": "task_release",
                    "reason_code": clean_reason_code,
                    "previous_owner_session_id": previous_owner,
                },
            )
        for handoff_id in invalidated_handoff_ids:
            self._emit(
                connection,
                project_id,
                "task.handoff_cancelled",
                actor_session_id=actor_session_id,
                task_id=task["id"],
                payload={
                    "handoff_id": handoff_id,
                    "by": "task_release",
                    "reason_code": clean_reason_code,
                    "previous_owner_session_id": previous_owner,
                },
            )
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        ).fetchone()
        return {
            "task": self._task_with_dependencies(connection, row),
            "released": True,
            "already_released": False,
            "reason_code": clean_reason_code,
            "reason": clean_reason,
            "released_lease_ids": released_lease_ids,
            "invalidated_assignment_ids": invalidated_assignment_ids,
            "invalidated_handoff_ids": invalidated_handoff_ids,
            "event_id": event_id,
            "cursor": event_id,
        }

    @idempotent_write("task.release")
    def release_task(
        self,
        project_id: str,
        task_id: str,
        *,
        reason_code: str,
        reason: str = "",
        session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        """Release an owned task back to the claimable pool.

        The current owner may release its own task; management may release on
        behalf of an owner that is offline, unreachable, or out of quota. The
        task returns to todo with its contract, progress, and history intact.

        ``management_authorized`` is set only by an adapter that has already
        verified an explicit management credential (or an equivalent local
        management surface). A missing Session credential is not management
        authority: without this flag the release is rejected instead of being
        attributed to an unauthenticated caller.
        """
        with self.database.connect(write=True) as connection:
            task = self._require_task(connection, project_id, task_id)
            actor_session_id: str | None = None
            initiator = "management"
            if session_id:
                if not token:
                    raise DomainError(
                        "invalid_session_token",
                        "session_id and token must be supplied together",
                        status_code=401,
                    )
                self._authenticate(connection, project_id, session_id, token)
                if task["owner_session_id"] != session_id:
                    if task["owner_session_id"] is None and task["execution_status"] == "todo":
                        # 已释放（或本就无主）的任务重复释放：幂等返回而不是
                        # 对旧 owner 误报 403。
                        return self._release_task_locked(
                            connection,
                            project_id,
                            task,
                            reason_code=reason_code,
                            reason=reason,
                            initiator="owner",
                            actor_session_id=session_id,
                        )
                    raise DomainError(
                        "not_task_owner",
                        "Only the task owner or management can release this task",
                        status_code=403,
                    )
                initiator = "owner"
                actor_session_id = session_id
            elif not management_authorized:
                raise _management_authorization_required("release this task")
            return self._release_task_locked(
                connection,
                project_id,
                task,
                reason_code=reason_code,
                reason=reason,
                initiator=initiator,
                actor_session_id=actor_session_id,
            )

    @idempotent_write("task.assign")
    def assign_task(
        self,
        project_id: str,
        task_id: str,
        *,
        assigned_by_session_id: str | None = None,
        token: str | None = None,
        assigned_to_session_id: str | None = None,
        assigned_to_member_id: str | None = None,
        target_role: str = "",
        required_capability: str = "",
        note: str = "",
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        role = target_role.strip()
        capability = required_capability.strip()
        member_target = (assigned_to_member_id or "").strip()
        if member_target and assigned_to_session_id:
            raise DomainError(
                "invalid_assignment_target",
                "Provide either an Agent session or a persistent member target, not both",
            )
        if not assigned_to_session_id and not member_target and not role and not capability:
            raise DomainError(
                "missing_assignment_target",
                "An assignment requires an Agent session, role, or capability target",
            )
        with self.database.connect(write=True) as connection:
            if bool(assigned_by_session_id) != bool(token):
                raise DomainError(
                    "invalid_session_token",
                    "assigned_by_session_id and token must be supplied together",
                    status_code=401,
                )
            if assigned_by_session_id:
                self._authenticate(
                    connection,
                    project_id,
                    assigned_by_session_id,
                    token or "",
                )
            elif not management_authorized:
                raise _management_authorization_required("assign this task")
            task = self._require_task(connection, project_id, task_id)
            if task["execution_status"] in {"completed", "cancelled"}:
                raise DomainError(
                    "task_not_assignable",
                    "Completed or cancelled tasks cannot receive a new assignment",
                    status_code=409,
                )
            target_offline = False
            if member_target:
                member = connection.execute(
                    "SELECT * FROM project_members WHERE id = ? AND project_id = ?",
                    (member_target, project_id),
                ).fetchone()
                if member is None:
                    raise DomainError(
                        "assignment_target_not_found",
                        "The target Agent identity does not exist in this Project",
                        status_code=404,
                    )
                if member["status"] == "revoked":
                    raise DomainError(
                        "assignment_target_revoked",
                        "Revoked Agent identities cannot receive assignments",
                        status_code=409,
                    )
                latest_session = connection.execute(
                    """
                    SELECT * FROM agent_sessions
                    WHERE project_id = ? AND member_id = ?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (project_id, member_target),
                ).fetchone()
                if latest_session is None:
                    raise DomainError(
                        "assignment_target_not_found",
                        "The Agent identity has never joined this Project, so there is no resolvable delayed target",
                        status_code=404,
                    )
                target_offline = (
                    self._agent_dict(latest_session)["status"] == "offline"
                )
                # A delayed assignment records both the persistent identity and
                # its latest Session. Any later Session of that same identity may
                # acknowledge it without rewriting the original target history.
                assigned_to_session_id = str(latest_session["id"])
            if assigned_to_session_id:
                target = connection.execute(
                    "SELECT id FROM agent_sessions WHERE id = ? AND project_id = ?",
                    (assigned_to_session_id, project_id),
                ).fetchone()
                if target is None:
                    raise DomainError(
                        "assignment_target_not_found",
                        "The target Agent session does not exist in this Project",
                        status_code=404,
                    )
            existing = connection.execute(
                """
                SELECT * FROM task_assignments
                WHERE project_id = ? AND task_id = ? AND status = 'pending'
                  AND COALESCE(assigned_by_session_id, '') = COALESCE(?, '')
                  AND COALESCE(assigned_to_session_id, '') = COALESCE(?, '')
                  AND target_role = ? AND required_capability = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    project_id,
                    task_id,
                    assigned_by_session_id,
                    assigned_to_session_id,
                    role,
                    capability,
                ),
            ).fetchone()
            if existing is not None:
                if member_target and not existing["assigned_to_member_id"]:
                    connection.execute(
                        "UPDATE task_assignments SET assigned_to_member_id = ? WHERE id = ?",
                        (member_target, existing["id"]),
                    )
                    existing = connection.execute(
                        "SELECT * FROM task_assignments WHERE id = ?",
                        (existing["id"],),
                    ).fetchone()
                return {
                    "assignment": dict(existing),
                    "task": self._task_with_dependencies(connection, task),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            # 原子重派：同一任务先失效既有 pending 指派（目标不同才失效，
            # 同目标重复指派已在上方幂等返回），再创建唯一的新 pending。
            stale = connection.execute(
                """
                SELECT id, assigned_to_session_id, assigned_to_member_id FROM task_assignments
                WHERE project_id = ? AND task_id = ? AND status = 'pending'
                  AND (
                    COALESCE(assigned_to_session_id, '') <> COALESCE(?, '')
                    OR COALESCE(assigned_to_member_id, '') <> COALESCE(?, '')
                  )
                ORDER BY created_at
                """,
                (project_id, task_id, assigned_to_session_id or "", member_target),
            ).fetchall()
            now_reassign = iso_now()
            for row in stale:
                connection.execute(
                    """
                    UPDATE task_assignments
                    SET status = 'cancelled', responded_by_session_id = ?,
                        response_note = 'superseded by reassignment', responded_at = ?
                    WHERE id = ?
                    """,
                    (assigned_by_session_id, now_reassign, row["id"]),
                )
                self._emit(
                    connection,
                    project_id,
                    "task.assignment_cancelled",
                    actor_session_id=assigned_by_session_id,
                    task_id=task_id,
                    payload={
                        "assignment_id": str(row["id"]),
                        "by": "reassign",
                        "previous_target_session_id": str(
                            row["assigned_to_session_id"] or ""
                        ),
                        "task_number": task["task_number"],
                    },
                )
            assignment_id = new_id("assignment")
            now = iso_now()
            connection.execute(
                """
                INSERT INTO task_assignments(
                    id, project_id, task_id, assigned_by_session_id,
                    assigned_to_session_id, assigned_to_member_id, responded_by_session_id,
                    target_role, required_capability, status, note,
                    response_note, created_at, responded_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'pending', ?, '', ?, NULL)
                """,
                (
                    assignment_id,
                    project_id,
                    task_id,
                    assigned_by_session_id,
                    assigned_to_session_id,
                    member_target or None,
                    role,
                    capability,
                    note.strip(),
                    now,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "task.assigned",
                actor_session_id=assigned_by_session_id,
                task_id=task_id,
                payload={
                    "assignment_id": assignment_id,
                    "assigned_to_session_id": assigned_to_session_id,
                    "assigned_to_member_id": member_target,
                    "target_offline": target_offline if member_target else None,
                    "target_role": role,
                    "required_capability": capability,
                    "note": note.strip(),
                    "assigned_by": (
                        "agent" if assigned_by_session_id else "manager"
                    ),
                    "requires_acknowledgement": True,
                },
            )
            assignment = connection.execute(
                "SELECT * FROM task_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
            return {
                "assignment": dict(assignment),
                "task": self._task_with_dependencies(connection, task),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.assignment_acknowledge")
    def acknowledge_task_assignment(
        self,
        project_id: str,
        task_id: str,
        assignment_id: str,
        *,
        session_id: str,
        token: str,
        response: str,
        note: str = "",
    ) -> dict[str, Any]:
        if response not in ASSIGNMENT_RESPONSES:
            raise DomainError(
                "invalid_assignment_response",
                "Assignment response must be accepted, declined, or blocked",
            )
        with self.database.connect(write=True) as connection:
            agent = self._authenticate(connection, project_id, session_id, token)
            task = self._require_task(connection, project_id, task_id)
            assignment = connection.execute(
                """
                SELECT * FROM task_assignments
                WHERE id = ? AND project_id = ? AND task_id = ?
                """,
                (assignment_id, project_id, task_id),
            ).fetchone()
            if assignment is None:
                raise DomainError(
                    "assignment_not_found",
                    "Task assignment does not exist",
                    status_code=404,
                )
            capabilities = json_load(agent["capabilities_json"], {})
            capability_match = not assignment["required_capability"] or bool(
                capabilities.get(assignment["required_capability"])
            )
            member_target_match = bool(
                assignment["assigned_to_member_id"]
                and assignment["assigned_to_member_id"] == agent["member_id"]
            )
            eligible = (
                (
                    assignment["assigned_to_session_id"] in {None, session_id}
                    or member_target_match
                )
                and (not assignment["target_role"] or assignment["target_role"] == agent["role"])
                and capability_match
            )
            if not eligible:
                raise DomainError(
                    "assignment_not_for_agent",
                    "This Agent session does not match the assignment target",
                    status_code=403,
                )
            if assignment["status"] != "pending":
                if (
                    assignment["status"] == response
                    and assignment["responded_by_session_id"] == session_id
                ):
                    return {
                        "assignment": dict(assignment),
                        "task": self._task_with_dependencies(connection, task),
                        "event_id": None,
                        "cursor": self.latest_cursor(connection, project_id),
                    }
                raise DomainError(
                    "assignment_already_acknowledged",
                    "Task assignment already has a response",
                    status_code=409,
                )
            reclaimed_from_session_id: str | None = None
            transferred_lease_ids: list[str] = []
            if response == "accepted":
                if task["owner_session_id"] not in {None, session_id}:
                    previous_owner_id = str(task["owner_session_id"])
                    liveness = self._owner_liveness(
                        connection, project_id, previous_owner_id
                    )
                    # Accepting an assignment is explicit intent for this task,
                    # but taking it away from another Session is a reclaim and
                    # must satisfy the same contract task_claim enforces:
                    # same software identity, provable absence, unfinished work.
                    if not self._same_agent_identity(agent, liveness["owner"]):
                        raise DomainError(
                            "task_reclaim_forbidden",
                            "Only the owning software identity may take back "
                            "unfinished execution work through an assignment",
                            status_code=403,
                            details={
                                "owner_session_id": previous_owner_id,
                                "required_action": "task_claim_by_owner_identity_or_management_reassignment",
                            },
                        )
                    if not (
                        task["execution_status"] == "in_progress"
                        and task["verification_status"] == "changes_requested"
                    ):
                        raise DomainError(
                            "task_already_claimed",
                            "Task is already owned by another Agent",
                            status_code=409,
                            details={"owner_session_id": previous_owner_id},
                        )
                    if liveness["connected"]:
                        raise DomainError(
                            "task_owner_session_connected",
                            "The owning Session is still connected; it must release "
                            "the task or its own identity has to take over explicitly",
                            status_code=409,
                            details={
                                "owner_session_id": previous_owner_id,
                                "owner_transport_state": liveness["transport_state"],
                                "required_action": "task_claim(takeover=true)_by_owner_identity",
                            },
                        )
                    reclaimed_from_session_id = previous_owner_id
                if task["execution_status"] not in {"todo", "claimed"} and not (
                    task["execution_status"] == "in_progress"
                    and task["verification_status"] == "changes_requested"
                    and reclaimed_from_session_id is not None
                ):
                    raise DomainError(
                        "task_not_assignable",
                        "Task is not available for assignment acceptance",
                        status_code=409,
                    )
                if task["owner_session_id"] is None:
                    owner_clause = "owner_session_id IS NULL"
                    owner_params: tuple[Any, ...] = ()
                else:
                    owner_clause = "owner_session_id = ?"
                    owner_params = (task["owner_session_id"],)
                self._require_finished_dependencies(connection, task_id)
                updated_task = connection.execute(
                    f"""
                    UPDATE tasks
                    SET owner_session_id = ?, status = 'claimed',
                        execution_status = 'claimed', updated_at = ?
                    WHERE id = ? AND project_id = ? AND {owner_clause}
                      AND execution_status = ?
                    """,
                    (
                        session_id,
                        iso_now(),
                        task_id,
                        project_id,
                        *owner_params,
                        task["execution_status"],
                    ),
                )
                if updated_task.rowcount != 1:
                    raise DomainError(
                        "task_already_claimed",
                        "Task was modified concurrently",
                        status_code=409,
                    )
                if reclaimed_from_session_id is not None:
                    # Ownership moved, so the departed owner's task-scoped
                    # exclusive leases have to move with it; leaving them behind
                    # blocks the new owner on its own task until the TTL expires.
                    lease_rows = connection.execute(
                        """
                        SELECT id FROM file_leases
                        WHERE project_id = ? AND task_id = ? AND session_id = ?
                          AND released_at IS NULL
                        """,
                        (project_id, task_id, reclaimed_from_session_id),
                    ).fetchall()
                    transferred_lease_ids = [str(row["id"]) for row in lease_rows]
                    if transferred_lease_ids:
                        connection.execute(
                            """
                            UPDATE file_leases
                            SET session_id = ?, renewed_at = ?
                            WHERE project_id = ? AND task_id = ? AND session_id = ?
                              AND released_at IS NULL
                            """,
                            (
                                session_id,
                                iso_now(),
                                project_id,
                                task_id,
                                reclaimed_from_session_id,
                            ),
                        )
            now = iso_now()
            updated_assignment = connection.execute(
                """
                UPDATE task_assignments
                SET status = ?, responded_by_session_id = ?, response_note = ?,
                    responded_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (response, session_id, note.strip(), now, assignment_id),
            )
            if updated_assignment.rowcount != 1:
                raise DomainError(
                    "assignment_already_acknowledged",
                    "Task assignment was updated concurrently",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "task.assignment_acknowledged",
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "assignment_id": assignment_id,
                    "response": response,
                    "note": note.strip(),
                    "reclaimed_from_session_id": reclaimed_from_session_id,
                    "transferred_lease_ids": transferred_lease_ids,
                },
            )
            updated_assignment = connection.execute(
                "SELECT * FROM task_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
            updated_task = self._require_task(connection, project_id, task_id)
            return {
                "assignment": dict(updated_assignment),
                "task": self._task_with_dependencies(connection, updated_task),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.handoff")
    def handoff_task(
        self,
        project_id: str,
        task_id: str,
        *,
        to_session_id: str,
        summary: str,
        next_step: str,
        from_session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
        completed_items: list[str] | None = None,
        pending_items: list[str] | None = None,
        files: list[str] | None = None,
        risks: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_summary = summary.strip()
        clean_next_step = next_step.strip()
        if not clean_summary or not clean_next_step:
            raise DomainError(
                "insufficient_handoff_context",
                "Task handoff requires a summary and an explicit next step",
            )

        def clean_items(values: list[str] | None, field: str) -> list[str]:
            bounded = self._require_list_input_limit(
                field,
                values or [],
                limit=int(getattr(self.settings, "list_input_max_count", 100)),
            )
            return list(
                dict.fromkeys(
                    str(item).strip() for item in bounded if str(item).strip()
                )
            )

        clean_completed = clean_items(completed_items, "completed_items")
        clean_pending = clean_items(pending_items, "pending_items")
        clean_files = sorted(
            {
                normalize_evidence_path(path)
                for path in self._require_list_input_limit(
                    "files",
                    files or [],
                    limit=int(getattr(self.settings, "list_input_max_count", 100)),
                )
            }
        )
        clean_risks = clean_items(risks, "risks")
        with self.database.connect(write=True) as connection:
            if bool(from_session_id) != bool(token):
                raise DomainError(
                    "invalid_session_token",
                    "from_session_id and token must be supplied together",
                    status_code=401,
                )
            if from_session_id:
                self._authenticate(
                    connection,
                    project_id,
                    from_session_id,
                    token or "",
                )
            task = self._require_task(connection, project_id, task_id)
            # "No credentials presented" is not owner authority. A handoff moves
            # ownership once the target accepts, and accepting releases the
            # source's active leases, so the caller must either prove it is the
            # current owner or arrive through an authenticated management
            # request. The unauthenticated path used to borrow the owner's own
            # session id, which made the ownership check below compare a value
            # with itself and never fail, and the audit then recorded another
            # Agent's intrusion as requested_by "manager".
            if not from_session_id and not management_authorized:
                raise _management_authorization_required(
                    "hand this task off using the owner's credentials"
                )
            if from_session_id:
                if task["owner_session_id"] != from_session_id:
                    raise DomainError(
                        "not_task_owner",
                        "Only the current task owner can hand off this task",
                        status_code=403,
                        details={
                            "required_action": "handoff_requires_current_owner_credentials"
                        },
                    )
                source_session_id = from_session_id
            else:
                source_session_id = task["owner_session_id"]
            if not source_session_id:
                raise DomainError(
                    "handoff_source_missing",
                    "Task must have an owner before it can be handed off",
                    status_code=409,
                )
            if task["execution_status"] == "cancelled":
                raise DomainError(
                    "task_not_handoffable",
                    "Cancelled tasks cannot be handed off",
                    status_code=409,
                )
            if task["execution_status"] == "completed" and (
                task["verification_status"] != "approved"
                or task["integration_status"] != "pending"
            ):
                raise DomainError(
                    "task_not_handoffable",
                    "Completed tasks can be handed off only after independent approval and before integration",
                    status_code=409,
                )
            if to_session_id == source_session_id:
                raise DomainError(
                    "invalid_handoff_target",
                    "Task handoff target must be a different Agent session",
                )
            target = connection.execute(
                "SELECT id FROM agent_sessions WHERE id = ? AND project_id = ?",
                (to_session_id, project_id),
            ).fetchone()
            if target is None:
                raise DomainError(
                    "handoff_target_not_found",
                    "The target Agent session does not exist in this Project",
                    status_code=404,
                )
            existing = connection.execute(
                """
                SELECT * FROM task_handoffs
                WHERE project_id = ? AND task_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, task_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["from_session_id"] == source_session_id
                    and existing["to_session_id"] == to_session_id
                ):
                    return {
                        "handoff": self._handoff_dict(existing),
                        "task": self._task_with_dependencies(connection, task),
                        "event_id": None,
                        "cursor": self.latest_cursor(connection, project_id),
                    }
                raise DomainError(
                    "handoff_already_pending",
                    "Task already has a pending handoff",
                    status_code=409,
                    details={"handoff_id": existing["id"]},
                )
            handoff_id = new_id("handoff")
            now = iso_now()
            connection.execute(
                """
                INSERT INTO task_handoffs(
                    id, project_id, task_id, from_session_id, to_session_id,
                    requested_by_session_id, responded_by_session_id, status,
                    summary, completed_items_json, pending_items_json,
                    files_json, risks_json, next_step, response_note,
                    created_at, responded_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?, ?, ?, ?, ?, '', ?, NULL)
                """,
                (
                    handoff_id,
                    project_id,
                    task_id,
                    source_session_id,
                    to_session_id,
                    from_session_id,
                    clean_summary,
                    json_dump(clean_completed),
                    json_dump(clean_pending),
                    json_dump(clean_files),
                    json_dump(clean_risks),
                    clean_next_step,
                    now,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "task.handoff_requested",
                actor_session_id=from_session_id,
                task_id=task_id,
                payload={
                    "handoff_id": handoff_id,
                    "from_session_id": source_session_id,
                    "to_session_id": to_session_id,
                    "requested_by": "agent" if from_session_id else "manager",
                    "summary": clean_summary,
                    "completed_items": clean_completed,
                    "pending_items": clean_pending,
                    "files": clean_files,
                    "risks": clean_risks,
                    "next_step": clean_next_step,
                    "requires_acknowledgement": True,
                },
            )
            handoff = connection.execute(
                "SELECT * FROM task_handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            return {
                "handoff": self._handoff_dict(handoff),
                "task": self._task_with_dependencies(connection, task),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.handoff_acknowledge")
    def acknowledge_task_handoff(
        self,
        project_id: str,
        task_id: str,
        handoff_id: str,
        *,
        session_id: str,
        token: str,
        response: str,
        note: str = "",
    ) -> dict[str, Any]:
        if response not in HANDOFF_RESPONSES:
            raise DomainError(
                "invalid_handoff_response",
                "Handoff response must be accepted, declined, or blocked",
            )
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            task = self._require_task(connection, project_id, task_id)
            handoff = connection.execute(
                """
                SELECT * FROM task_handoffs
                WHERE id = ? AND project_id = ? AND task_id = ?
                """,
                (handoff_id, project_id, task_id),
            ).fetchone()
            if handoff is None:
                raise DomainError(
                    "handoff_not_found",
                    "Task handoff does not exist",
                    status_code=404,
                )
            if handoff["to_session_id"] != session_id:
                raise DomainError(
                    "handoff_not_for_agent",
                    "Only the target Agent session can acknowledge this handoff",
                    status_code=403,
                )
            if handoff["status"] != "pending":
                if (
                    handoff["status"] == response
                    and handoff["responded_by_session_id"] == session_id
                ):
                    return {
                        "handoff": self._handoff_dict(handoff),
                        "task": self._task_with_dependencies(connection, task),
                        "event_id": None,
                        "cursor": self.latest_cursor(connection, project_id),
                    }
                raise DomainError(
                    "handoff_already_acknowledged",
                    "Task handoff already has a response",
                    status_code=409,
                )
            released_lease_ids: list[str] = []
            if response == "accepted":
                if task["owner_session_id"] != handoff["from_session_id"]:
                    raise DomainError(
                        "handoff_owner_changed",
                        "Task ownership changed after the handoff was requested",
                        status_code=409,
                    )
                if task["execution_status"] == "cancelled":
                    raise DomainError(
                        "task_not_handoffable",
                        "Cancelled tasks cannot accept a handoff",
                        status_code=409,
                    )
                self._require_finished_dependencies(connection, task_id)
                if task["execution_status"] == "completed" and (
                    task["verification_status"] != "approved"
                    or task["integration_status"] != "pending"
                ):
                    raise DomainError(
                        "task_not_handoffable",
                        "Completed tasks can accept a handoff only after independent approval and before integration",
                        status_code=409,
                    )
                now = iso_now()
                active_leases = connection.execute(
                    """
                    SELECT id FROM file_leases
                    WHERE project_id = ? AND task_id = ? AND session_id = ?
                      AND released_at IS NULL
                    """,
                    (project_id, task_id, handoff["from_session_id"]),
                ).fetchall()
                released_lease_ids = [row["id"] for row in active_leases]
                if released_lease_ids:
                    connection.execute(
                        """
                        UPDATE file_leases SET released_at = ?
                        WHERE project_id = ? AND task_id = ? AND session_id = ?
                          AND released_at IS NULL
                        """,
                        (now, project_id, task_id, handoff["from_session_id"]),
                    )
                updated_task = connection.execute(
                    """
                    UPDATE tasks
                    SET owner_session_id = ?, current_step = ?, next_step = '',
                        updated_at = ?
                    WHERE id = ? AND project_id = ? AND owner_session_id = ?
                    """,
                    (session_id, handoff["next_step"], now, task_id, project_id, handoff["from_session_id"]),
                )
                if updated_task.rowcount != 1:
                    raise DomainError(
                        "task_handoff_conflict",
                        "Task ownership changed concurrently",
                        status_code=409,
                    )
            now = iso_now()
            updated_handoff = connection.execute(
                """
                UPDATE task_handoffs
                SET status = ?, responded_by_session_id = ?, response_note = ?,
                    responded_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (response, session_id, note.strip(), now, handoff_id),
            )
            if updated_handoff.rowcount != 1:
                raise DomainError(
                    "handoff_already_acknowledged",
                    "Task handoff was updated concurrently",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "task.handoff_acknowledged",
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "handoff_id": handoff_id,
                    "from_session_id": handoff["from_session_id"],
                    "to_session_id": handoff["to_session_id"],
                    "response": response,
                    "note": note.strip(),
                    "released_lease_ids": released_lease_ids,
                },
            )
            updated_handoff = connection.execute(
                "SELECT * FROM task_handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            updated_task = self._require_task(connection, project_id, task_id)
            return {
                "handoff": self._handoff_dict(updated_handoff),
                "task": self._task_with_dependencies(connection, updated_task),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.update")
    def update_task(
        self,
        project_id: str,
        task_id: str,
        *,
        status: str | None = None,
        title: str | None = None,
        description: str | None = None,
        acceptance_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
        priority: int | None = None,
        progress_percent: int | None = None,
        current_step: str | None = None,
        blocker_reason: str | None = None,
        next_step: str | None = None,
        session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            if session_id:
                self._authenticate(connection, project_id, session_id, token or "")
            task = self._require_task(connection, project_id, task_id)
            # Ownership is checked before any field or status handling: the
            # status=todo compatibility release below must never become an
            # unaudited way for another Session to release someone else's task.
            is_owner = bool(session_id) and task["owner_session_id"] == session_id
            if session_id and task["owner_session_id"] not in {None, session_id}:
                raise DomainError("not_task_owner", "Only the task owner can update this task", status_code=403)
            if not session_id and not management_authorized:
                raise _management_authorization_required("update this task")
            next_status = status or task["status"]
            is_cancelling = next_status == "cancelled" and task["status"] != "cancelled"
            # Cancellation is an ownership decision, not a field edit. An
            # unowned task used to fall through the ownership check above, so
            # any authenticated Session could cancel it and, through the
            # terminal cleanup, release another Agent's exclusive lease on it.
            # A cancelled task must never be reopened through this method: it
            # is terminal, and the state machine below no longer offers the
            # edge that made a silent cancelled -> todo hop possible.
            if is_cancelling and not (is_owner or management_authorized):
                raise DomainError(
                    "management_auth_required",
                    "Cancelling a task requires the task owner's own credentials "
                    "or an authenticated management request",
                    status_code=403,
                    details={
                        "required_action": (
                            "use_task_owner_credentials_or_management_authorization"
                        ),
                        "attempted_status": next_status,
                    },
                )
            if task["status"] == "cancelled" and next_status != "cancelled":
                raise DomainError(
                    "task_is_terminal",
                    "A cancelled task is terminal and cannot be reopened through "
                    "task_update; create a replacement task instead",
                    status_code=409,
                    details={
                        "required_action": "create_a_replacement_task",
                        "status": task["status"],
                    },
                )
            text_limit = self.settings.task_text_max_length
            for field_name, value in (
                ("title", title),
                ("description", description),
                ("current_step", current_step),
                ("blocker_reason", blocker_reason),
                ("next_step", next_step),
            ):
                if value is not None and len(str(value)) > text_limit:
                    raise DomainError(
                        "task_text_too_long",
                        f"Task {field_name} exceeds task_text_max_length ({text_limit})",
                        details={"field": field_name, "limit": text_limit},
                    )
            if acceptance_criteria is not None:
                list_limit = int(getattr(self.settings, "list_input_max_count", 100))
                self._require_list_input_limit(
                    "acceptance_criteria", acceptance_criteria, limit=list_limit
                )
                for item in acceptance_criteria:
                    if len(str(item)) > text_limit:
                        raise DomainError(
                            "task_text_too_long",
                            "Acceptance criterion exceeds task_text_max_length "
                            f"({text_limit})",
                            details={
                                "field": "acceptance_criteria",
                                "limit": text_limit,
                            },
                        )
            if depends_on is not None:
                list_limit = int(getattr(self.settings, "list_input_max_count", 100))
                self._require_list_input_limit(
                    "depends_on", depends_on, limit=list_limit
                )
            if (
                status is not None
                and status != task["status"]
                and status in {"awaiting_review", "verified", "done"}
            ):
                raise DomainError(
                    "structured_transition_required",
                    "Review and integration states must be entered through work reports, reviews, and integrations",
                    status_code=409,
                )
            if next_status not in TASK_STATUSES:
                raise DomainError("invalid_task_status", "Unsupported task status")
            if next_status not in TASK_TRANSITIONS[task["status"]]:
                raise DomainError(
                    "invalid_transition",
                    f"Task cannot move from {task['status']} to {next_status}",
                    status_code=409,
                )
            if (
                status == "todo"
                and task["owner_session_id"] is not None
                and task["execution_status"] in TASK_RELEASE_EXECUTION_STATUSES
            ):
                if not (is_owner or management_authorized):
                    raise _management_authorization_required(
                        "release this task through task_update(status=todo)"
                    )
                release_result = self._release_task_locked(
                    connection,
                    project_id,
                    task,
                    reason_code=TASK_RELEASE_COMPAT_REASON_CODE,
                    reason="released through task_update(status=todo); "
                    "prefer task_release for an explicit reason code",
                    initiator="owner" if is_owner else "management",
                    actor_session_id=session_id if is_owner else None,
                )
                task = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if all(
                    value is None
                    for value in (
                        title,
                        description,
                        acceptance_criteria,
                        depends_on,
                        priority,
                        progress_percent,
                        current_step,
                        blocker_reason,
                        next_step,
                    )
                ):
                    return release_result
            (
                next_execution_status,
                next_verification_status,
                next_integration_status,
            ) = task_state_for_legacy_status(
                next_status,
                previous_verification=task["verification_status"],
                previous_integration=task["integration_status"],
            )
            priority = require_int_field(priority, "priority")
            progress_percent = require_int_field(progress_percent, "progress_percent")
            if priority is not None and not 0 <= priority <= 4:
                raise DomainError("invalid_priority", "Priority must be between 0 and 4")
            if progress_percent is not None and not 0 <= progress_percent <= 100:
                raise DomainError(
                    "invalid_task_progress", "Task progress must be between 0 and 100"
                )
            next_title = task["title"] if title is None else title.strip()
            if not next_title:
                raise DomainError("invalid_task", "Task title is required")
            next_criteria = (
                json_load(task["acceptance_criteria_json"], [])
                if acceptance_criteria is None
                else [item.strip() for item in acceptance_criteria if item.strip()]
            )
            if not next_criteria:
                raise DomainError(
                    "missing_acceptance_criteria",
                    "At least one acceptance criterion is required",
                )
            if task["status"] in {"awaiting_review", "verified", "done"} and any(
                value is not None
                for value in (title, acceptance_criteria, depends_on)
            ):
                raise DomainError(
                    "task_contract_locked",
                    "Task title, dependencies, and acceptance criteria are locked during review",
                    status_code=409,
                )
            next_dependencies = (
                [
                    row["depends_on_task_id"]
                    for row in connection.execute(
                        "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY created_at",
                        (task_id,),
                    ).fetchall()
                ]
                if depends_on is None
                else list(dict.fromkeys(depends_on))
            )
            for dependency_id in next_dependencies:
                if dependency_id == task_id:
                    raise DomainError(
                        "task_dependency_cycle", "A task cannot depend on itself"
                    )
                self._require_satisfiable_dependency(connection, project_id, dependency_id)
                reaches_current = connection.execute(
                    """
                    WITH RECURSIVE chain(task_id) AS (
                        SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?
                        UNION
                        SELECT d.depends_on_task_id
                        FROM task_dependencies d JOIN chain c ON d.task_id = c.task_id
                    )
                    SELECT 1 FROM chain WHERE task_id = ? LIMIT 1
                    """,
                    (dependency_id, task_id),
                ).fetchone()
                if reaches_current:
                    raise DomainError(
                        "task_dependency_cycle",
                        "Task dependencies cannot contain a cycle",
                    )
            next_blocker_reason = (
                task["blocker_reason"]
                if blocker_reason is None
                else blocker_reason.strip()
            )
            if next_status == "blocked" and not next_blocker_reason:
                raise DomainError(
                    "missing_blocker_reason",
                    "A blocked task requires a blocker reason",
                )
            if task["status"] == "blocked" and next_status != "blocked":
                next_blocker_reason = ""
            owner = task["owner_session_id"]
            if next_status in {"todo", "cancelled"}:
                owner = None
            changed_fields = [
                field
                for field, old, new in (
                    ("status", task["status"], next_status),
                    (
                        "execution_status",
                        task["execution_status"],
                        next_execution_status,
                    ),
                    (
                        "verification_status",
                        task["verification_status"],
                        next_verification_status,
                    ),
                    (
                        "integration_status",
                        task["integration_status"],
                        next_integration_status,
                    ),
                    ("title", task["title"], next_title),
                    (
                        "description",
                        task["description"],
                        description if description is not None else task["description"],
                    ),
                    ("acceptance_criteria", json_load(task["acceptance_criteria_json"], []), next_criteria),
                    ("priority", task["priority"], priority if priority is not None else task["priority"]),
                    (
                        "progress_percent",
                        task["progress_percent"],
                        progress_percent if progress_percent is not None else task["progress_percent"],
                    ),
                    (
                        "current_step",
                        task["current_step"],
                        current_step.strip() if current_step is not None else task["current_step"],
                    ),
                    ("blocker_reason", task["blocker_reason"], next_blocker_reason),
                    (
                        "next_step",
                        task["next_step"],
                        next_step.strip() if next_step is not None else task["next_step"],
                    ),
                    (
                        "depends_on",
                        [
                            row["depends_on_task_id"]
                            for row in connection.execute(
                                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY created_at",
                                (task_id,),
                            ).fetchall()
                        ],
                        next_dependencies,
                    ),
                )
                if old != new
            ]
            if task["owner_session_id"] is None:
                owner_clause = "owner_session_id IS NULL"
                owner_params: tuple[Any, ...] = ()
            else:
                owner_clause = "owner_session_id = ?"
                owner_params = (task["owner_session_id"],)

            updated = connection.execute(
                f"""
                UPDATE tasks SET status = ?, execution_status = ?,
                                 verification_status = ?, integration_status = ?,
                                 title = ?, description = ?,
                                 acceptance_criteria_json = ?, priority = ?,
                                 owner_session_id = ?, progress_percent = ?,
                                 current_step = ?, blocker_reason = ?, next_step = ?,
                                 updated_at = ?
                WHERE id = ? AND project_id = ? AND status = ? AND {owner_clause}
                """,
                (
                    next_status,
                    next_execution_status,
                    next_verification_status,
                    next_integration_status,
                    next_title,
                    description if description is not None else task["description"],
                    json_dump(next_criteria),
                    priority if priority is not None else task["priority"],
                    owner,
                    progress_percent if progress_percent is not None else task["progress_percent"],
                    current_step.strip() if current_step is not None else task["current_step"],
                    next_blocker_reason,
                    next_step.strip() if next_step is not None else task["next_step"],
                    iso_now(),
                    task_id,
                    project_id,
                    task["status"],
                    *owner_params,
                ),
            )
            if updated.rowcount != 1:
                raise DomainError(
                    "task_modified_concurrently",
                    "Task was modified concurrently; reload the task and retry",
                    status_code=409,
                )
            if depends_on is not None:
                connection.execute(
                    "DELETE FROM task_dependencies WHERE task_id = ?", (task_id,)
                )
                for dependency_id in next_dependencies:
                    connection.execute(
                        """
                        INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (task_id, dependency_id, iso_now()),
                    )
            # Cancellation is decided by the target state alone. It used to sit
            # behind the unblocked branch, so a blocked -> cancelled move was
            # recorded as task.unblocked and skipped the terminal cleanup
            # entirely: the task's exclusive leases stayed held for their full
            # TTL and its pending assignments/handoffs stayed actionable.
            is_cancellation = (
                next_status == "cancelled" and task["status"] != "cancelled"
            )
            if is_cancellation:
                event_type = "task.cancelled"
            elif next_status == "blocked" and task["status"] != "blocked":
                event_type = "task.blocked"
            elif next_status == "todo" and task["owner_session_id"]:
                event_type = "task.released"
            elif task["status"] == "blocked" and next_status != "blocked":
                event_type = "task.unblocked"
            else:
                event_type = "task.updated"
            cancellation_cleanup: dict[str, Any] | None = None
            if is_cancellation:
                cancellation_cleanup = self._cancel_task_resources_locked(
                    connection, project_id, task_id, session_id
                )
            event_id = self._emit(
                connection,
                project_id,
                event_type,
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "from_status": task["status"],
                    "status": next_status,
                    "from_execution_status": task["execution_status"],
                    "execution_status": next_execution_status,
                    "from_verification_status": task["verification_status"],
                    "verification_status": next_verification_status,
                    "from_integration_status": task["integration_status"],
                    "integration_status": next_integration_status,
                    "changed_fields": changed_fields,
                    "blocker_reason": next_blocker_reason,
                    **(
                        {"cancellation": cancellation_cleanup}
                        if cancellation_cleanup is not None
                        else {}
                    ),
                },
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return {
                "task": self._task_with_dependencies(connection, row),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("lease.acquire")
    def acquire_lease(
        self,
        project_id: str,
        *,
        session_id: str,
        token: str,
        path_pattern: str,
        mode: str = "exclusive",
        task_id: str | None = None,
        reason: str = "",
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        pattern = normalize_pattern(path_pattern)
        if mode not in LEASE_MODES:
            raise DomainError(
                "invalid_lease_mode",
                "Lease mode must be readonly, shared, or exclusive",
            )
        ttl = (
            self.settings.default_lease_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        if not 1 <= ttl <= self.settings.max_lease_ttl_seconds:
            raise DomainError(
                "invalid_lease_ttl",
                f"Lease TTL must be between 1 and {self.settings.max_lease_ttl_seconds}",
            )
        now = utc_now()
        expires = now + timedelta(seconds=ttl)
        with self.database.connect(write=True) as connection:
            claimant = self._authenticate(connection, project_id, session_id, token)
            claimant_member_id = (
                claimant["member_id"] if "member_id" in claimant.keys() else None
            )
            if task_id:
                task = self._require_task(connection, project_id, task_id)
                if task["execution_status"] in {"cancelled", "completed"}:
                    raise DomainError("task_not_leasable", "Terminal tasks cannot acquire file leases", status_code=409)
                if task["owner_session_id"] not in {None, session_id}:
                    raise DomainError("not_task_owner", "Lease task belongs to another agent", status_code=403)
            # Leases are active purely by their own TTL: an owner that is
            # merely silent (long quiet work past the heartbeat window) keeps
            # the lease enforced. Only expiry or explicit release ends it.
            active = connection.execute(
                """
                SELECT l.*, a.name AS agent_name, a.member_id AS owner_member_id
                FROM file_leases l
                JOIN agent_sessions a ON a.id = l.session_id
                WHERE l.project_id = ? AND l.released_at IS NULL AND l.expires_at > ?
                """,
                (project_id, now.isoformat().replace("+00:00", "Z")),
            ).fetchall()
            conflicts = []
            superseded: list[dict[str, Any]] = []
            for row in active:
                if row["session_id"] == session_id:
                    continue
                if not patterns_overlap(pattern, row["path_pattern"]):
                    continue
                if not lease_modes_conflict(mode, row["mode"]):
                    continue
                if (
                    claimant_member_id
                    and row["owner_member_id"]
                    and row["owner_member_id"] == claimant_member_id
                ):
                    # Same software identity, new session: the successor
                    # transparently takes over the stale lease of its own
                    # identity (a reaped transport cannot release it itself) -
                    # but only where the new scope actually replaces the old one.
                    if _pattern_covers(pattern, row["path_pattern"]):
                        superseded.append(dict(row))
                    continue
                conflicts.append(
                    {
                        "lease_id": row["id"],
                        "session_id": row["session_id"],
                        "agent_name": row["agent_name"],
                        "path_pattern": row["path_pattern"],
                        "mode": row["mode"],
                        "expires_at": row["expires_at"],
                    }
                )
            if conflicts:
                event_id = self._emit(
                    connection,
                    project_id,
                    "lease.conflict",
                    actor_session_id=session_id,
                    task_id=task_id,
                    payload={"path_pattern": pattern, "conflicts": conflicts},
                )
                conflict_result = {
                    "conflicts": conflicts,
                    "event_id": event_id,
                    "cursor": event_id,
                }
            else:
                # The same-identity takeover only happens when this request is
                # actually granted. Releasing the predecessor's leases before a
                # rejection destroyed coverage the caller never obtained.
                for stale in superseded:
                    connection.execute(
                        "UPDATE file_leases SET released_at = ? WHERE id = ?",
                        (now.isoformat().replace("+00:00", "Z"), stale["id"]),
                    )
                    self._emit(
                        connection,
                        project_id,
                        "lease.released",
                        actor_session_id=session_id,
                        payload={
                            "lease_id": stale["id"],
                            "path_pattern": stale["path_pattern"],
                            "reason": "same_member_takeover",
                        },
                    )
                # 幂等：同一 Session 对同一（归一化）范围的活跃租约重复申请
                # 直接续用已有租约，不产生并行的重复占用行。
                existing = next(
                    (
                        row
                        for row in active
                        if row["session_id"] == session_id
                        and row["path_pattern"] == pattern
                    ),
                    None,
                )
                if existing is not None:
                    if existing["mode"] != mode:
                        if (
                            LEASE_MODE_STRENGTH[mode]
                            < LEASE_MODE_STRENGTH[existing["mode"]]
                        ):
                            raise DomainError(
                                "lease_mode_downgrade_requires_release",
                                "An active lease cannot be weakened in place; "
                                "release it and acquire the weaker mode explicitly",
                                status_code=409,
                                details={
                                    "required_action": "release_lease_then_reacquire",
                                    "lease_id": existing["id"],
                                    "current_mode": existing["mode"],
                                    "requested_mode": mode,
                                },
                            )
                        # Raising the protection level of a scope the caller
                        # already holds is a mode change, not a reuse of the old
                        # mode: returning the readonly lease while the caller
                        # believes it holds exclusive would leave check_leases
                        # and the pre-commit policy blind to real writers.
                        stamped = now.isoformat().replace("+00:00", "Z")
                        connection.execute(
                            """
                            UPDATE file_leases
                            SET mode = ?, expires_at = ?, renewed_at = ?, ttl_seconds = ?
                            WHERE id = ?
                            """,
                            (
                                mode,
                                expires.isoformat().replace("+00:00", "Z"),
                                stamped,
                                ttl,
                                existing["id"],
                            ),
                        )
                        mode_event_id = self._emit(
                            connection,
                            project_id,
                            "lease.mode_changed",
                            actor_session_id=session_id,
                            task_id=task_id,
                            payload={
                                "lease_id": existing["id"],
                                "path_pattern": pattern,
                                "from_mode": existing["mode"],
                                "to_mode": mode,
                            },
                        )
                        changed = connection.execute(
                            "SELECT * FROM file_leases WHERE id = ?", (existing["id"],)
                        ).fetchone()
                        return {
                            "lease": self._lease_dict(changed),
                            "already_held": True,
                            "mode_changed": True,
                            "event_id": mode_event_id,
                            "cursor": mode_event_id,
                        }
                    return {
                        "lease": self._lease_dict(existing),
                        "already_held": True,
                        "event_id": None,
                        "cursor": self.latest_cursor(connection, project_id),
                    }
                lease_id = new_id("lease")
                created_at = now.isoformat().replace("+00:00", "Z")
                expires_at = expires.isoformat().replace("+00:00", "Z")
                connection.execute(
                    """
                    INSERT INTO file_leases(
                        id, project_id, session_id, task_id, path_pattern, mode,
                        reason, ttl_seconds, expires_at, renewed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        project_id,
                        session_id,
                        task_id,
                        pattern,
                        mode,
                        reason.strip(),
                        ttl,
                        expires_at,
                        created_at,
                        created_at,
                    ),
                )
                event_id = self._emit(
                    connection,
                    project_id,
                    "lease.acquired",
                    actor_session_id=session_id,
                    task_id=task_id,
                    payload={"lease_id": lease_id, "path_pattern": pattern, "mode": mode},
                )
                row = connection.execute("SELECT * FROM file_leases WHERE id = ?", (lease_id,)).fetchone()
                result = {
                    "lease": self._lease_dict(row),
                    "event_id": event_id,
                    "cursor": event_id,
                }
        if conflicts:
            raise DomainError(
                "lease_conflict",
                "Requested file range overlaps an active lease",
                status_code=409,
                details=conflict_result,
            )
        return result

    def list_leases(self, project_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """
                SELECT l.*, a.last_heartbeat AS owner_last_heartbeat
                FROM file_leases l
                JOIN agent_sessions a ON a.id = l.session_id
                WHERE l.project_id = ? ORDER BY l.created_at DESC
                """,
                (project_id,),
            ).fetchall()
            leases = [self._lease_dict(row) for row in rows]
            return leases if include_inactive else [lease for lease in leases if lease["active"]]

    def check_leases(
        self,
        project_id: str,
        *,
        paths: list[str],
        session_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        if bool(session_id) != bool(token):
            raise DomainError(
                "invalid_session_token",
                "session_id and token must be supplied together",
                status_code=401,
            )
        paths = self._require_list_input_limit(
            "paths",
            paths,
            limit=int(getattr(self.settings, "list_input_max_count", 100)),
        )
        clean_paths = sorted({normalize_pattern(path) for path in paths})
        if not clean_paths:
            raise DomainError("missing_paths", "At least one file path is required")
        with self.database.connect(write=True) as connection:
            project = self._require_project(connection, project_id)
            caller_member_id: str | None = None
            if session_id:
                caller = self._authenticate(
                    connection, project_id, session_id, token or ""
                )
                caller_member_id = (
                    caller["member_id"] if "member_id" in caller.keys() else None
                )
            now = iso_now()
            # Presence is display-only (#119): a silent lease owner still
            # enforces its lease until the TTL lapses or it releases, so
            # conflict checking must not consult last_heartbeat — otherwise
            # check_leases and acquire_lease would disagree.
            active = connection.execute(
                """
                SELECT l.*, a.name AS agent_name, a.member_id AS owner_member_id
                FROM file_leases l
                JOIN agent_sessions a ON a.id = l.session_id
                WHERE l.project_id = ? AND l.released_at IS NULL
                  AND l.expires_at > ? AND l.mode = 'exclusive'
                """,
                (project_id, now),
            ).fetchall()
            conflicts = [
                {
                    "path": path,
                    "lease_id": lease["id"],
                    "session_id": lease["session_id"],
                    "agent_name": lease["agent_name"],
                    "path_pattern": lease["path_pattern"],
                    "expires_at": lease["expires_at"],
                }
                for path in clean_paths
                for lease in active
                if lease["session_id"] != session_id
                and not (
                    # A lease belonging to the caller's own software identity is
                    # takeable, not a conflict: acquire_lease treats it that way,
                    # so the check must not disagree (the invariant this function
                    # states in its own comment).
                    caller_member_id
                    and lease["owner_member_id"]
                    and lease["owner_member_id"] == caller_member_id
                )
                and patterns_overlap(path, lease["path_pattern"])
            ]
            settings = normalize_project_settings(
                json_load(project["settings_json"], {})
            )
            blocked = settings["lease_conflict_policy"] == "pre_commit_block" and bool(
                conflicts
            )
            cursor = self.latest_cursor(connection, project_id)
            if blocked:
                cursor = self._emit(
                    connection,
                    project_id,
                    "lease.pre_commit_blocked",
                    actor_session_id=session_id,
                    payload={"paths": clean_paths, "conflicts": conflicts},
                )
            result = {
                "policy": settings["lease_conflict_policy"],
                "blocked": blocked,
                "conflicts": conflicts,
                "cursor": cursor,
            }
        if blocked:
            raise DomainError(
                "pre_commit_blocked",
                "Commit contains files covered by another Agent's exclusive lease",
                status_code=409,
                details=result,
            )
        return result

    @idempotent_write("lease.release")
    def release_lease(
        self,
        project_id: str,
        lease_id: str,
        session_id: str,
        token: str,
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            lease = connection.execute(
                "SELECT * FROM file_leases WHERE id = ? AND project_id = ?",
                (lease_id, project_id),
            ).fetchone()
            if lease is None:
                raise DomainError("lease_not_found", "Lease does not exist", status_code=404)
            if lease["session_id"] != session_id:
                raise DomainError("not_lease_owner", "Only the lease owner can release it", status_code=403)
            if lease["released_at"] is not None:
                # 幂等：重复释放不再追加事件，返回可区分的 already_released 结果。
                return {
                    "released": False,
                    "already_released": True,
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            connection.execute(
                "UPDATE file_leases SET released_at = ? WHERE id = ?", (iso_now(), lease_id)
            )
            event_id = self._emit(
                connection,
                project_id,
                "lease.released",
                actor_session_id=session_id,
                task_id=lease["task_id"],
                payload={"lease_id": lease_id, "path_pattern": lease["path_pattern"]},
            )
            return {"released": True, "event_id": event_id, "cursor": event_id}

    @idempotent_write("work_report.submit")
    def submit_work_report(
        self,
        project_id: str,
        task_id: str,
        *,
        session_id: str,
        token: str,
        summary: str,
        files: list[str],
        tests: list[dict[str, Any]],
        commit_hash: str = "",
        no_code_change_reason: str = "",
    ) -> dict[str, Any]:
        clean_files = self._require_list_input_limit(
            "files",
            sorted({normalize_evidence_path(path) for path in files}),
            limit=int(getattr(self.settings, "list_input_max_count", 100)),
        )
        clean_no_code_reason = no_code_change_reason.strip()
        if not summary.strip() or not tests or (not clean_files and not clean_no_code_reason):
            raise DomainError(
                "insufficient_work_evidence",
                "Work report requires summary, test evidence, and either changed files or a no-code-change reason",
            )
        tests = self._validate_test_evidence_list(tests, field="tests")
        with self.database.connect() as connection:
            agent = self._authenticate(connection, project_id, session_id, token)
            project = self._require_project(connection, project_id)
            task = self._require_task(connection, project_id, task_id)
            if task["owner_session_id"] != session_id:
                raise DomainError(
                    "not_task_owner",
                    "Only the task owner can report work",
                    status_code=403,
                )
            if task["execution_status"] not in {"claimed", "in_progress", "blocked"}:
                raise DomainError(
                    "invalid_transition",
                    "Task is not in a state that accepts a work report",
                    status_code=409,
                )
            worktree = self._trusted_worktree(connection, project, agent)
        system_evidence = self._collect_git_evidence_from_worktree(
            worktree, commit_hash
        )
        stored_commit = str(system_evidence.get("commit_hash") or "")
        if commit_hash.strip() and (
            not system_evidence.get("captured")
            or system_evidence.get("reported_commit_exists") is not True
        ):
            with self.database.connect(write=True) as connection:
                self._authenticate(connection, project_id, session_id, token)
                event_id = self._emit(
                    connection,
                    project_id,
                    "work.commit_unverified",
                    actor_session_id=session_id,
                    task_id=task_id,
                    payload={
                        "commit_hash": "unknown",
                        "reported_commit_hash": commit_hash.strip(),
                        "worktree": str(worktree),
                        "captured": bool(system_evidence.get("captured")),
                        "reported_commit_exists": system_evidence.get(
                            "reported_commit_exists"
                        ),
                        "reason": system_evidence.get("reason") or "unverified_commit",
                    },
                )
            raise DomainError(
                "invalid_commit_hash",
                "Reported commit_hash could not be verified in the registered worktree",
                status_code=400,
                details={"commit_hash": "unknown", "event_id": event_id},
            )
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            task = self._require_task(connection, project_id, task_id)
            if task["owner_session_id"] != session_id:
                raise DomainError("not_task_owner", "Only the task owner can report work", status_code=403)
            if task["execution_status"] not in {"claimed", "in_progress", "blocked"}:
                raise DomainError(
                    "invalid_transition",
                    "Task is not in a state that accepts a work report",
                    status_code=409,
                )
            report_id = new_id("report")
            now = iso_now()
            connection.execute(
                """
                INSERT INTO work_reports(
                    id, project_id, task_id, session_id, summary, files_json,
                    no_code_change_reason, commit_hash, tests_json,
                    system_evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    project_id,
                    task_id,
                    session_id,
                    summary.strip(),
                    json_dump(clean_files),
                    clean_no_code_reason,
                    stored_commit,
                    json_dump(tests),
                    json_dump(system_evidence),
                    now,
                ),
            )
            updated_task = connection.execute(
                """
                UPDATE tasks
                SET status = 'awaiting_review', execution_status = 'completed',
                    verification_status = 'pending', integration_status = 'pending',
                    progress_percent = 100, blocker_reason = '', updated_at = ?
                WHERE id = ? AND project_id = ? AND owner_session_id = ?
                  AND execution_status IN ('claimed', 'in_progress', 'blocked')
                """,
                (now, task_id, project_id, session_id),
            )
            if updated_task.rowcount != 1:
                raise DomainError(
                    "task_modified_concurrently",
                    "Task was modified concurrently; reload the task and retry",
                    status_code=409,
                )
            active_leases = connection.execute(
                """
                SELECT id FROM file_leases
                WHERE project_id = ? AND task_id = ? AND session_id = ?
                  AND released_at IS NULL
                """,
                (project_id, task_id, session_id),
            ).fetchall()
            released_lease_ids = [row["id"] for row in active_leases]
            if released_lease_ids:
                connection.execute(
                    """
                    UPDATE file_leases SET released_at = ?
                    WHERE project_id = ? AND task_id = ? AND session_id = ?
                      AND released_at IS NULL
                    """,
                    (now, project_id, task_id, session_id),
                )
            report_event_id = self._emit(
                connection,
                project_id,
                "work.reported",
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "report_id": report_id,
                    "summary": summary.strip(),
                    "files": clean_files,
                    "no_code_change_reason": clean_no_code_reason,
                    "commit_hash": commit_hash.strip(),
                    "tests": tests,
                    "system_evidence": system_evidence,
                    "released_lease_ids": released_lease_ids,
                    "spec_receipt": self._latest_claim_document_snapshot(
                        connection, project_id, task_id
                    ),
                },
            )
            completion_event_id = self._emit(
                connection,
                project_id,
                "task.completed",
                actor_session_id=session_id,
                task_id=task_id,
                payload={
                    "report_id": report_id,
                    "no_code_change_reason": clean_no_code_reason,
                    "execution_status": "completed",
                    "verification_status": "pending",
                    "integration_status": "pending",
                },
            )
            return {
                "report_id": report_id,
                "no_code_change_reason": clean_no_code_reason,
                "task_status": "awaiting_review",
                "execution_status": "completed",
                "verification_status": "pending",
                "integration_status": "pending",
                "report_event_id": report_event_id,
                "event_id": completion_event_id,
                "cursor": completion_event_id,
            }

    @idempotent_write("review.submit")
    def submit_review(
        self,
        project_id: str,
        task_id: str,
        *,
        reviewer_session_id: str,
        token: str,
        verdict: str,
        criteria: list[dict[str, Any]],
        notes: str = "",
    ) -> dict[str, Any]:
        if verdict not in REVIEW_VERDICTS:
            raise DomainError("invalid_review_verdict", "Unsupported review verdict")
        if not criteria:
            raise DomainError("missing_review_criteria", "Review must assess acceptance criteria")
        criteria = self._require_list_input_limit(
            "criteria",
            criteria,
            limit=int(getattr(self.settings, "list_input_max_count", 100)),
        )
        assessed = self._review_criteria_map(criteria)
        with self.database.connect(write=True) as connection:
            reviewer = self._authenticate(
                connection, project_id, reviewer_session_id, token
            )
            task = self._require_task(connection, project_id, task_id)
            if (
                task["execution_status"] != "completed"
                or task["verification_status"] != "pending"
            ):
                raise DomainError(
                    "invalid_transition", "Task is not awaiting review", status_code=409
                )
            owner = connection.execute(
                "SELECT * FROM agent_sessions WHERE id = ?",
                (task["owner_session_id"],),
            ).fetchone()
            if self._same_agent_identity(owner, reviewer):
                raise DomainError(
                    "reviewer_not_independent",
                    "The executing software identity cannot independently verify its own work",
                    status_code=409,
                )
            expected = json_load(task["acceptance_criteria_json"], [])
            if verdict == "approved" and (
                any(assessed.get(item) != "passed" for item in expected)
                or any(value != "passed" for value in assessed.values())
            ):
                raise DomainError(
                    "acceptance_criteria_not_satisfied",
                    "Approved reviews must mark every acceptance criterion as passed",
                    status_code=409,
                    details={"expected": expected, "assessed": assessed},
                )
            review_id = new_id("review")
            now = iso_now()
            next_status = "verified" if verdict == "approved" else "in_progress"
            next_execution_status = "completed" if verdict == "approved" else "in_progress"
            next_verification_status = (
                "approved" if verdict == "approved" else "changes_requested"
            )
            next_progress = 100 if verdict == "approved" else min(
                int(task["progress_percent"]), 99
            )
            connection.execute(
                """
                INSERT INTO reviews(
                    id, project_id, task_id, reviewer_session_id, verdict,
                    criteria_json, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    project_id,
                    task_id,
                    reviewer_session_id,
                    verdict,
                    json_dump(criteria),
                    notes.strip(),
                    now,
                ),
            )
            updated_task = connection.execute(
                """
                UPDATE tasks
                SET status = ?, execution_status = ?, verification_status = ?,
                    integration_status = 'pending', progress_percent = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'awaiting_review'
                  AND verification_status = 'pending'
                """,
                (
                    next_status,
                    next_execution_status,
                    next_verification_status,
                    next_progress,
                    now,
                    task_id,
                    project_id,
                ),
            )
            if updated_task.rowcount != 1:
                raise DomainError(
                    "task_modified_concurrently",
                    "Task was modified concurrently; reload the task and retry",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "review.submitted",
                actor_session_id=reviewer_session_id,
                task_id=task_id,
                payload={
                    "review_id": review_id,
                    "verdict": verdict,
                    "criteria": criteria,
                    "notes": notes.strip(),
                    "execution_status": next_execution_status,
                    "verification_status": next_verification_status,
                    "integration_status": "pending",
                },
            )
            return {
                "review_id": review_id,
                "task_status": next_status,
                "execution_status": next_execution_status,
                "verification_status": next_verification_status,
                "integration_status": "pending",
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("integration.submit")
    def submit_integration(
        self,
        project_id: str,
        task_id: str,
        *,
        result: str,
        summary: str,
        tests: list[dict[str, Any]],
        files: list[str] | None = None,
        commit_hash: str = "",
        integrator_session_id: str | None = None,
        token: str | None = None,
        management_authorized: bool = False,
    ) -> dict[str, Any]:
        if result not in INTEGRATION_RESULTS:
            raise DomainError(
                "invalid_integration_result",
                "Integration result must be done or failed",
            )
        clean_summary = summary.strip()
        if not clean_summary or not tests:
            raise DomainError(
                "insufficient_integration_evidence",
                "Integration requires a summary and structured test evidence",
            )
        tests = self._validate_test_evidence_list(tests, field="tests")
        if result == "done" and any(test["exit_code"] != 0 for test in tests):
            raise DomainError(
                "integration_tests_failed",
                "A successful integration requires every submitted test to pass",
                status_code=409,
            )
        clean_files = sorted(
            {
                normalize_evidence_path(path)
                for path in self._require_list_input_limit(
                    "files",
                    files or [],
                    limit=int(getattr(self.settings, "list_input_max_count", 100)),
                )
            }
        )
        with self.database.connect(write=True) as connection:
            if bool(integrator_session_id) != bool(token):
                raise DomainError(
                    "invalid_session_token",
                    "integrator_session_id and token must be supplied together",
                    status_code=401,
                )
            if integrator_session_id:
                self._authenticate(
                    connection,
                    project_id,
                    integrator_session_id,
                    token or "",
                )
            elif not management_authorized:
                # Same gate as handoff/assign/release/update. An integration
                # without credentials used to be honoured as a management write
                # simply because nobody had authenticated it, which let any
                # caller stamp the terminal integration state with no actor at
                # all.
                raise _management_authorization_required("record this integration")
            task = self._require_task(connection, project_id, task_id)
            if (
                task["execution_status"] != "completed"
                or task["verification_status"] != "approved"
            ):
                raise DomainError(
                    "task_not_ready_for_integration",
                    "Task must be execution-complete and independently approved before integration",
                    status_code=409,
                )
            if task["integration_status"] == "done":
                raise DomainError(
                    "task_already_integrated",
                    "Task integration is already complete",
                    status_code=409,
                )
            # A commit hash carried by an integration is only as trustworthy as
            # the commit the approved Work Report already proved against the
            # trusted worktree. Re-running git here would put subprocess work
            # back inside the write transaction (#337), so anything that does
            # not match that proven commit is recorded honestly as
            # self-reported instead of being rendered as captured evidence.
            clean_commit = commit_hash.strip()
            reported_commit = ""
            for report_row in connection.execute(
                """
                SELECT payload_json FROM events
                WHERE project_id = ? AND task_id = ? AND event_type = 'work.report'
                ORDER BY id DESC LIMIT 1
                """,
                (project_id, task_id),
            ).fetchall():
                reported_commit = str(
                    json_load(report_row["payload_json"], {}).get("commit_hash") or ""
                ).strip()
            commit_verified = bool(clean_commit) and clean_commit == reported_commit
            integration_id = new_id("integration")
            now = iso_now()
            connection.execute(
                """
                INSERT INTO task_integrations(
                    id, project_id, task_id, integrator_session_id, result,
                    summary, files_json, commit_hash, tests_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    integration_id,
                    project_id,
                    task_id,
                    integrator_session_id,
                    result,
                    clean_summary,
                    json_dump(clean_files),
                    clean_commit,
                    json_dump(tests),
                    now,
                ),
            )
            next_status = "done" if result == "done" else "verified"
            updated_task = connection.execute(
                """
                UPDATE tasks
                SET status = ?, integration_status = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'verified'
                  AND verification_status = 'approved'
                """,
                (next_status, result, now, task_id, project_id),
            )
            if updated_task.rowcount != 1:
                raise DomainError(
                    "task_modified_concurrently",
                    "Task was modified concurrently; reload the task and retry",
                    status_code=409,
                )
            event_type = (
                "task.integration_completed"
                if result == "done"
                else "task.integration_failed"
            )
            event_id = self._emit(
                connection,
                project_id,
                event_type,
                actor_session_id=integrator_session_id,
                task_id=task_id,
                payload={
                    "integration_id": integration_id,
                    "result": result,
                    "summary": clean_summary,
                    "files": clean_files,
                    "commit_hash": clean_commit,
                    "commit_verified": commit_verified,
                    "commit_self_reported": (
                        bool(clean_commit) and not commit_verified
                    ),
                    "tests": tests,
                    "integrated_by": (
                        "agent" if integrator_session_id else "manager"
                    ),
                    "execution_status": "completed",
                    "verification_status": "approved",
                    "integration_status": result,
                },
            )
            integration = connection.execute(
                "SELECT * FROM task_integrations WHERE id = ?",
                (integration_id,),
            ).fetchone()
            updated_task = self._require_task(connection, project_id, task_id)
            return {
                "integration": self._integration_dict(integration),
                "task": self._task_with_dependencies(connection, updated_task),
                "event_id": event_id,
                "cursor": event_id,
            }

    def _require_knowledge_asset(
        self, connection: Any, project_id: str, asset_id: str
    ) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM knowledge_assets WHERE id = ? AND project_id = ?",
            (asset_id, project_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                "knowledge_asset_not_found",
                "Knowledge asset does not exist in this project",
                status_code=404,
            )
        return row

    def _validate_knowledge_source(
        self,
        connection: Any,
        project_id: str,
        *,
        source_type: str,
        source_task_id: str,
        source_report_id: str,
        source_review_id: str,
        source_integration_id: str,
        source_event_ids: list[int],
    ) -> str:
        if source_type not in KNOWLEDGE_SOURCE_TYPES:
            raise DomainError(
                "invalid_knowledge_source_type",
                "Knowledge source type must be one of "
                + ", ".join(sorted(KNOWLEDGE_SOURCE_TYPES)),
                details={"source_type": source_type},
            )
        task: Mapping[str, Any] | None = None
        if source_task_id:
            task = connection.execute(
                "SELECT id, verification_status FROM tasks WHERE id = ? AND project_id = ?",
                (source_task_id, project_id),
            ).fetchone()
            if task is None:
                raise DomainError(
                    "knowledge_source_not_found",
                    "Source task does not exist in this project",
                    status_code=404,
                    details={"source_task_id": source_task_id},
                )
        referenced_task_ids: set[str] = set()
        for field, value, table in (
            ("source_report_id", source_report_id, "work_reports"),
            ("source_review_id", source_review_id, "reviews"),
            ("source_integration_id", source_integration_id, "task_integrations"),
        ):
            if not value:
                continue
            row = connection.execute(
                f"SELECT id, task_id FROM {table} WHERE id = ? AND project_id = ?",
                (value, project_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "knowledge_source_not_found",
                    f"{field} does not exist in this project",
                    status_code=404,
                    details={field: value},
                )
            if row["task_id"]:
                referenced_task_ids.add(str(row["task_id"]))
            if source_task_id and row["task_id"] != source_task_id:
                raise DomainError(
                    "knowledge_source_mismatch",
                    f"{field} belongs to a different task than source_task_id",
                    status_code=409,
                    details={field: value, "source_task_id": source_task_id},
                )
        if len(referenced_task_ids) > 1:
            raise DomainError(
                "knowledge_source_mismatch",
                "Referenced sources belong to different tasks",
                status_code=409,
                details={"source_task_ids": sorted(referenced_task_ids)},
            )
        derived_task_id = next(iter(referenced_task_ids), "")
        normalized_task_id = source_task_id or derived_task_id
        if source_type == "task_result" and not normalized_task_id:
            raise DomainError(
                "knowledge_source_task_required",
                "task_result knowledge must identify its source task directly or "
                "through a referenced report, review, or integration",
                status_code=409,
                details={"source_type": source_type},
            )
        for event_id in source_event_ids:
            row = connection.execute(
                "SELECT id FROM events WHERE project_seq = ? AND project_id = ?",
                (event_id, project_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "knowledge_source_not_found",
                    "source_event_ids must reference events in this project "
                    "(Agent-visible numbers are project_seq)",
                    status_code=404,
                    details={"event_id": event_id},
                )
        return normalized_task_id

    @idempotent_write("knowledge.candidate_submit")
    def submit_knowledge_candidate(
        self,
        project_id: str,
        *,
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
    ) -> dict[str, Any]:
        clean_title = title.strip()
        clean_body = body.strip()
        clean_kind = kind.strip()
        clean_summary = summary.strip()
        # Knowledge content was the one user-facing write with no size bound,
        # while every sibling write (task text, project documents) has one.
        # Reuse the existing configured limits rather than adding new ones.
        text_limit = int(getattr(self.settings, "task_text_max_length", 32768))
        for field_name, value in (
            ("title", clean_title),
            ("summary", clean_summary),
        ):
            if len(value) > text_limit:
                raise DomainError(
                    "knowledge_content_too_long",
                    f"Knowledge {field_name} exceeds task_text_max_length ({text_limit})",
                    status_code=422,
                    details={
                        "field": field_name,
                        "limit": text_limit,
                        "length": len(value),
                    },
                )
        body_limit = int(getattr(self.settings, "project_document_max_bytes", 262144))
        body_bytes = len(clean_body.encode("utf-8"))
        if body_bytes > body_limit:
            raise DomainError(
                "knowledge_content_too_large",
                "Knowledge body exceeds project_document_max_bytes "
                f"({body_limit})",
                status_code=422,
                details={"field": "body", "limit": body_limit, "size": body_bytes},
            )
        list_limit = int(getattr(self.settings, "list_input_max_count", 100))
        clean_tags = self._require_list_input_limit(
            "tags",
            sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()}),
            limit=list_limit,
        )
        clean_event_ids = self._require_list_input_limit(
            "source_event_ids",
            sorted({int(event_id) for event_id in (source_event_ids or [])}),
            limit=list_limit,
        )
        if not clean_title or not clean_body:
            raise DomainError(
                "insufficient_knowledge_content",
                "Knowledge candidate requires a title and a body",
            )
        allowed_kinds = self.settings.knowledge_kinds
        if clean_kind not in allowed_kinds:
            raise DomainError(
                "invalid_knowledge_kind",
                "Knowledge kind is not enabled for this deployment",
                details={"kind": clean_kind, "allowed": list(allowed_kinds)},
            )
        clean_task_id = source_task_id.strip()
        clean_report_id = source_report_id.strip()
        clean_review_id = source_review_id.strip()
        clean_integration_id = source_integration_id.strip()
        clean_source_type = source_type.strip()
        with self.database.connect(write=True) as connection:
            agent = self._authenticate(connection, project_id, session_id, token)
            self._require_project(connection, project_id)
            clean_task_id = self._validate_knowledge_source(
                connection,
                project_id,
                source_type=clean_source_type,
                source_task_id=clean_task_id,
                source_report_id=clean_report_id,
                source_review_id=clean_review_id,
                source_integration_id=clean_integration_id,
                source_event_ids=clean_event_ids,
            )
            now = iso_now()
            target_asset_id = asset_id.strip()
            revision = False
            if target_asset_id:
                asset = self._require_knowledge_asset(
                    connection, project_id, target_asset_id
                )
                if clean_kind != asset["kind"]:
                    raise DomainError(
                        "knowledge_kind_immutable",
                        "Knowledge kind is fixed per asset; create a new asset for "
                        "a different kind",
                        status_code=409,
                        details={"asset_kind": asset["kind"], "kind": clean_kind},
                    )
                if asset["status"] == "archived":
                    raise DomainError(
                        "knowledge_asset_archived",
                        "Archived knowledge assets cannot receive new versions",
                        status_code=409,
                    )
                if asset["status"] != "candidate" and "candidate" not in (
                    KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]
                ):
                    raise DomainError(
                        "knowledge_invalid_transition",
                        f"Knowledge asset in status {asset['status']} must be "
                        "superseded before a new version is submitted",
                        status_code=409,
                        details={
                            "status": asset["status"],
                            "allowed": sorted(
                                KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]
                            ),
                        },
                    )
                revision = True
                previous_version_id = asset["current_version_id"]
                next_version_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) AS latest FROM knowledge_asset_versions WHERE asset_id = ?",
                        (target_asset_id,),
                    ).fetchone()["latest"]
                ) + 1
                previous_status = asset["status"]
                owner_kind = asset["owner_kind"]
                owner_id = asset["owner_id"]
                new_asset_id = target_asset_id
            else:
                if agent["member_id"]:
                    owner_kind, owner_id = "member", agent["member_id"]
                else:
                    owner_kind = "agent_key"
                    owner_id = str(agent["agent_key"] or "").strip()
                previous_version_id = None
                next_version_number = 1
                previous_status = None
                new_asset_id = new_id("kasset")
            version_id = new_id("kversion")
            content_hash = hashlib.sha256(
                json_dump(
                    {
                        "title": clean_title,
                        "body": clean_body,
                        "summary": clean_summary,
                        "tags": clean_tags,
                    }
                ).encode()
            ).hexdigest()
            if revision:
                connection.execute(
                    """
                    UPDATE knowledge_assets
                    SET status = 'candidate', updated_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (now, new_asset_id, project_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO knowledge_assets(
                        id, project_id, kind, owner_kind, owner_id, status,
                        current_version_id, created_by_session_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'candidate', NULL, ?, ?, ?)
                    """,
                    (
                        new_asset_id,
                        project_id,
                        clean_kind,
                        owner_kind,
                        owner_id,
                        session_id,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO knowledge_asset_versions(
                    id, project_id, asset_id, version, title, body, summary,
                    content_hash, tags_json, source_type, source_task_id,
                    source_report_id, source_review_id, source_integration_id,
                    source_event_ids_json, created_by_session_id,
                    supersedes_version_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    new_asset_id,
                    next_version_number,
                    clean_title,
                    clean_body,
                    clean_summary,
                    content_hash,
                    json_dump(clean_tags),
                    clean_source_type,
                    clean_task_id or None,
                    clean_report_id or None,
                    clean_review_id or None,
                    clean_integration_id or None,
                    json_dump({"v": 1, "space": "project_seq", "ids": clean_event_ids}),
                    session_id,
                    previous_version_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE knowledge_assets
                SET current_version_id = ?, updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (version_id, now, new_asset_id, project_id),
            )
            payload = {
                "asset_id": new_asset_id,
                "version_id": version_id,
                "version": next_version_number,
                "kind": clean_kind,
                "title": clean_title,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "source_type": clean_source_type,
                "source_task_id": clean_task_id,
                "source_report_id": clean_report_id,
                "source_review_id": clean_review_id,
                "source_integration_id": clean_integration_id,
                "source_event_ids": clean_event_ids,
                "supersedes_version_id": previous_version_id,
                "status": "candidate",
                "created_by_agent_key": str(agent["agent_key"] or "").strip(),
            }
            if revision:
                payload["previous_status"] = previous_status
            event_id = self._emit(
                connection,
                project_id,
                "knowledge.candidate_submitted",
                actor_session_id=session_id,
                task_id=clean_task_id or None,
                payload=payload,
            )
            asset_row = connection.execute(
                "SELECT * FROM knowledge_assets WHERE id = ?", (new_asset_id,)
            ).fetchone()
            return {
                "asset": self._knowledge_asset_dict(connection, asset_row),
                "version_id": version_id,
                "version": next_version_number,
                "status": "candidate",
                "revision": revision,
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("knowledge.review")
    def submit_knowledge_review(
        self,
        project_id: str,
        asset_id: str,
        *,
        reviewer_session_id: str,
        token: str,
        verdict: str,
        criteria: list[dict[str, Any]],
        notes: str = "",
        expected_version_id: str = "",
    ) -> dict[str, Any]:
        if verdict not in KNOWLEDGE_REVIEW_VERDICTS:
            raise DomainError(
                "invalid_review_verdict", "Unsupported review verdict"
            )
        if not criteria:
            raise DomainError(
                "missing_review_criteria", "Review must assess acceptance criteria"
            )
        criteria = self._require_list_input_limit(
            "criteria",
            criteria,
            limit=int(getattr(self.settings, "list_input_max_count", 100)),
        )
        assessed = self._review_criteria_map(criteria)
        if verdict == "approved" and any(
            status != "passed" for status in assessed.values()
        ):
            raise DomainError(
                "acceptance_criteria_not_satisfied",
                "Approved reviews must mark every criterion as passed",
                status_code=409,
                details={"assessed": assessed},
            )
        if verdict == "approved" and not expected_version_id.strip():
            # Approving is what puts unreviewed content into the Room, so the
            # caller has to name the version it actually read. Rejecting and
            # requesting changes cannot launder content, so they stay optional.
            raise DomainError(
                "knowledge_version_pin_required",
                "Approving a candidate requires expected_version_id so the review "
                "is bound to the content that was read",
                status_code=422,
                details={"required_action": "reread_and_review_current_version"},
            )
        with self.database.connect(write=True) as connection:
            reviewer = self._authenticate(
                connection, project_id, reviewer_session_id, token
            )
            asset = self._require_knowledge_asset(connection, project_id, asset_id)
            if asset["status"] != "candidate":
                raise DomainError(
                    "knowledge_not_reviewable",
                    "Only candidate knowledge assets can be reviewed",
                    status_code=409,
                    details={"status": asset["status"]},
                )
            version = connection.execute(
                "SELECT * FROM knowledge_asset_versions WHERE id = ?",
                (asset["current_version_id"],),
            ).fetchone()
            if version is None:
                raise DomainError(
                    "knowledge_version_not_found",
                    "Knowledge asset has no current version",
                    status_code=409,
                )
            # A review must approve the exact content the reviewer read. The
            # current version is dereferenced here, at write time, so without a
            # precondition a v2 submitted mid-review would silently inherit
            # criteria the reviewer wrote against v1. Callers pin with the
            # version_id (or content_hash) returned by get/list.
            pinned_version = expected_version_id.strip()
            if pinned_version and pinned_version != str(version["id"]):
                raise DomainError(
                    "knowledge_version_stale",
                    "The knowledge asset moved to a new version after you read it; "
                    "re-read the current version and review that content",
                    status_code=409,
                    details={
                        "expected_version_id": pinned_version,
                        "current_version_id": str(version["id"]),
                        "required_action": "reread_and_review_current_version",
                    },
                )
            creator = connection.execute(
                "SELECT id, member_id, agent_key FROM agent_sessions WHERE id = ?",
                (version["created_by_session_id"],),
            ).fetchone()
            if self._same_agent_identity(creator, reviewer):
                raise DomainError(
                    "knowledge_reviewer_not_independent",
                    "The submitting software identity cannot independently approve its own "
                    "knowledge candidate",
                    status_code=409,
                )
            next_status = "approved" if verdict == "approved" else "rejected"
            if next_status not in KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]:
                raise DomainError(
                    "knowledge_invalid_transition",
                    f"Knowledge asset cannot move from {asset['status']} to {next_status}",
                    status_code=409,
                )
            if verdict == "approved" and self.settings.knowledge_require_verified_task:
                version_task_id = str(version["source_task_id"] or "")
                if version["source_type"] == "task_result" and not version_task_id:
                    raise DomainError(
                        "knowledge_source_task_required",
                        "task_result knowledge must identify its source task "
                        "before approval",
                        status_code=409,
                    )
                if version_task_id:
                    source_task = connection.execute(
                        "SELECT verification_status FROM tasks WHERE id = ? AND project_id = ?",
                        (version_task_id, project_id),
                    ).fetchone()
                    if source_task is None or source_task["verification_status"] != "approved":
                        raise DomainError(
                            "knowledge_task_not_verified",
                            "Knowledge tied to a task can be approved only after the task "
                            "is independently verified",
                            status_code=409,
                            details={
                                "source_task_id": version_task_id,
                                "verification_status": (
                                    source_task["verification_status"]
                                    if source_task
                                    else "missing"
                                ),
                            },
                        )
            review_id = new_id("kreview")
            now = iso_now()
            connection.execute(
                """
                INSERT INTO knowledge_reviews(
                    id, project_id, asset_id, version_id, reviewer_session_id,
                    verdict, criteria_json, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    project_id,
                    asset_id,
                    version["id"],
                    reviewer_session_id,
                    verdict,
                    json_dump(criteria),
                    notes.strip(),
                    now,
                ),
            )
            reviewed = connection.execute(
                """
                UPDATE knowledge_assets
                SET status = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = ?
                """,
                (next_status, now, asset_id, project_id, asset["status"]),
            )
            if reviewed.rowcount != 1:
                raise DomainError(
                    "knowledge_concurrent_transition",
                    "The knowledge asset status changed concurrently; retry",
                    status_code=409,
                )
            review_event_id = self._emit(
                connection,
                project_id,
                "knowledge.reviewed",
                actor_session_id=reviewer_session_id,
                task_id=version["source_task_id"] or None,
                payload={
                    "review_id": review_id,
                    "asset_id": asset_id,
                    "version_id": version["id"],
                    "verdict": verdict,
                    "criteria": criteria,
                    "notes": notes.strip(),
                    "from_status": asset["status"],
                    "status": next_status,
                },
            )
            event_id = review_event_id
            if verdict == "approved":
                event_id = self._emit(
                    connection,
                    project_id,
                    "knowledge.approved",
                    actor_session_id=reviewer_session_id,
                    task_id=version["source_task_id"] or None,
                    payload={
                        "review_id": review_id,
                        "asset_id": asset_id,
                        "version_id": version["id"],
                        "status": next_status,
                    },
                )
            asset_row = connection.execute(
                "SELECT * FROM knowledge_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            review_row = connection.execute(
                "SELECT * FROM knowledge_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            return {
                "asset": self._knowledge_asset_dict(connection, asset_row),
                "review": self._knowledge_review_dict(review_row),
                "status": next_status,
                "review_event_id": review_event_id,
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("knowledge.supersede")
    def supersede_knowledge_asset(
        self,
        project_id: str,
        asset_id: str,
        *,
        session_id: str,
        token: str,
        reason: str = "",
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            asset = self._require_knowledge_asset(connection, project_id, asset_id)
            if "superseded" not in KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]:
                raise DomainError(
                    "knowledge_invalid_transition",
                    f"Knowledge asset in status {asset['status']} cannot be superseded",
                    status_code=409,
                    details={
                        "status": asset["status"],
                        "allowed": sorted(
                            KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]
                        ),
                    },
                )
            now = iso_now()
            superseded = connection.execute(
                """
                UPDATE knowledge_assets
                SET status = 'superseded', updated_at = ?
                WHERE id = ? AND project_id = ? AND status = ?
                """,
                (now, asset_id, project_id, asset["status"]),
            )
            if superseded.rowcount != 1:
                raise DomainError(
                    "knowledge_concurrent_transition",
                    "The knowledge asset status changed concurrently; retry",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "knowledge.superseded",
                actor_session_id=session_id,
                payload={
                    "asset_id": asset_id,
                    "version_id": asset["current_version_id"],
                    "from_status": asset["status"],
                    "status": "superseded",
                    "reason": reason.strip(),
                },
            )
            asset_row = connection.execute(
                "SELECT * FROM knowledge_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            return {
                "asset": self._knowledge_asset_dict(connection, asset_row),
                "status": "superseded",
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("knowledge.archive")
    def archive_knowledge_asset(
        self,
        project_id: str,
        asset_id: str,
        *,
        session_id: str,
        token: str,
        reason: str = "",
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            asset = self._require_knowledge_asset(connection, project_id, asset_id)
            if "archived" not in KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]:
                raise DomainError(
                    "knowledge_invalid_transition",
                    f"Knowledge asset in status {asset['status']} cannot be archived",
                    status_code=409,
                    details={
                        "status": asset["status"],
                        "allowed": sorted(
                            KNOWLEDGE_ASSET_TRANSITIONS[asset["status"]]
                        ),
                    },
                )
            now = iso_now()
            archived = connection.execute(
                """
                UPDATE knowledge_assets
                SET status = 'archived', updated_at = ?
                WHERE id = ? AND project_id = ? AND status = ?
                """,
                (now, asset_id, project_id, asset["status"]),
            )
            if archived.rowcount != 1:
                raise DomainError(
                    "knowledge_concurrent_transition",
                    "The knowledge asset status changed concurrently; retry",
                    status_code=409,
                )
            event_id = self._emit(
                connection,
                project_id,
                "knowledge.archived",
                actor_session_id=session_id,
                payload={
                    "asset_id": asset_id,
                    "version_id": asset["current_version_id"],
                    "from_status": asset["status"],
                    "status": "archived",
                    "reason": reason.strip(),
                },
            )
            asset_row = connection.execute(
                "SELECT * FROM knowledge_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            return {
                "asset": self._knowledge_asset_dict(connection, asset_row),
                "status": "archived",
                "event_id": event_id,
                "cursor": event_id,
            }

    def get_knowledge_asset(
        self, project_id: str, asset_id: str, *, version_id: str = ""
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            asset = self._require_knowledge_asset(connection, project_id, asset_id)
            data = self._knowledge_asset_dict(connection, asset)
            versions = [
                self._knowledge_version_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM knowledge_asset_versions
                    WHERE asset_id = ? ORDER BY version ASC
                    """,
                    (asset_id,),
                ).fetchall()
            ]
            reviews = [
                self._knowledge_review_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM knowledge_reviews
                    WHERE asset_id = ? ORDER BY created_at ASC
                    """,
                    (asset_id,),
                ).fetchall()
            ]
            selected = None
            clean_version_id = version_id.strip()
            if clean_version_id:
                selected = next(
                    (item for item in versions if item["id"] == clean_version_id),
                    None,
                )
                if selected is None:
                    raise DomainError(
                        "knowledge_version_not_found",
                        "Knowledge version does not belong to this asset",
                        status_code=404,
                        details={"version_id": clean_version_id},
                    )
            else:
                selected = next(
                    (item for item in versions if item["id"] == asset["current_version_id"]),
                    None,
                )
            return {
                **data,
                "versions": versions,
                "reviews": reviews,
                "version": selected,
            }

    def list_knowledge_assets(
        self,
        project_id: str,
        *,
        status: str | None = None,
        kind: str | None = None,
        source_task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clean_status = (status or "").strip()
        if clean_status and clean_status not in KNOWLEDGE_ASSET_STATUSES:
            raise DomainError(
                "invalid_knowledge_status",
                "Unsupported knowledge asset status",
                details={"status": clean_status},
            )
        clauses = ["a.project_id = ?"]
        parameters: list[Any] = [project_id]
        if clean_status:
            clauses.append("a.status = ?")
            parameters.append(clean_status)
        clean_kind = (kind or "").strip()
        if clean_kind:
            clauses.append("a.kind = ?")
            parameters.append(clean_kind)
        clean_task_id = (source_task_id or "").strip()
        if clean_task_id:
            clauses.append(
                "a.current_version_id IN (SELECT id FROM knowledge_asset_versions "
                "WHERE source_task_id = ?)"
            )
            parameters.append(clean_task_id)
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                f"""
                SELECT a.* FROM knowledge_assets a
                WHERE {' AND '.join(clauses)}
                ORDER BY a.updated_at DESC
                """,
                parameters,
            ).fetchall()
            return [self._knowledge_asset_dict(connection, row) for row in rows]

    def _trusted_worktree(
        self,
        connection: Any,
        project: Mapping[str, Any],
        agent: Mapping[str, Any],
    ) -> Path:
        workspace = None
        agent_data = dict(agent)
        workspace_id = str(agent_data.get("workspace_id") or "").strip()
        if workspace_id:
            workspace = connection.execute(
                """
                SELECT * FROM workspaces
                WHERE id = ? AND project_id = ?
                """,
                (workspace_id, project["id"]),
            ).fetchone()
        if workspace is None:
            raise DomainError(
                "workspace_not_registered",
                "未登记工作区: the Agent has no registered workspace",
                status_code=409,
            )
        root = Path(str(workspace["local_path"])).expanduser().resolve(strict=False)
        reported_raw = str(agent_data.get("worktree") or "").strip()
        if not reported_raw:
            reported = root
        else:
            # A legacy row may hold a relative value. Resolving it against the
            # process CWD would make the containment verdict depend on where the
            # server was started; anchoring it at the registered workspace root is
            # the only reading that keeps `relative_to(root)` meaningful.
            reported_candidate = Path(reported_raw).expanduser()
            if not reported_candidate.is_absolute():
                reported_candidate = root / reported_candidate
            reported = reported_candidate.resolve(strict=False)
        try:
            reported.relative_to(root)
        except ValueError as error:
            raise DomainError(
                "untrusted_worktree",
                "Agent-reported worktree is outside the registered workspace",
                status_code=403,
                details={"workspace_id": workspace["id"]},
            ) from error
        return reported

    def _collect_git_status(self, worktree: Path) -> dict[str, Any]:
        return self._collect_git_evidence_from_worktree(worktree, "")

    def checkout_cleanup_scope(self, project_id: str) -> dict[str, Any]:
        """The checkouts a permanent Project delete may clean up, plus its identity.

        Session worktrees are caller-supplied strings and legacy rows were stored
        unvalidated, so the cleanup set is filtered to absolute local paths inside
        the Project root. An unrelated or remote directory a Session happened to
        report must never be stat/unlinked by deleting a Room.
        """
        with self.database.connect() as connection:
            project = self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT worktree FROM agent_sessions WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        root = Path(str(project["root_path"])).expanduser().resolve(strict=False)
        paths = {str(project["root_path"])}
        for row in rows:
            candidate = str(row["worktree"] or "").strip()
            if not candidate:
                continue
            try:
                validated = self._validate_workspace_path("worktree", candidate)
                Path(validated).expanduser().resolve(strict=False).relative_to(root)
            except (DomainError, OSError, ValueError):
                continue
            paths.add(validated)
        return {
            "project_key": str(project["project_key"]),
            "logical_path": str(project["logical_path"] or ""),
            "paths": sorted(paths),
        }

    def _collect_git_evidence_from_worktree(
        self,
        worktree: Path,
        commit_hash: str,
    ) -> dict[str, Any]:
        def git(*arguments: str) -> tuple[int, str]:
            returncode, stdout, _stderr = run_git_capture(
                ["git", "-C", str(worktree), *arguments]
            )
            return returncode, stdout

        inside_code, inside = git("rev-parse", "--is-inside-work-tree")
        if inside_code != 0 or inside != "true":
            return {
                "captured": False,
                "reason": "not_a_git_worktree",
                "worktree": str(worktree),
                "reported_commit_exists": False,
                "commit_hash": "unknown" if commit_hash.strip() else "",
            }
        _, head = git("rev-parse", "HEAD")
        _, branch = git("branch", "--show-current")
        _, status = git("status", "--porcelain")
        commit_exists = None
        commit_files: list[str] = []
        stored_commit = commit_hash.strip()
        if stored_commit:
            commit_code, _ = git("rev-parse", "--verify", f"{stored_commit}^{{commit}}")
            commit_exists = commit_code == 0
            if commit_exists:
                _, changed = git("show", "--format=", "--name-only", stored_commit)
                commit_files = sorted(
                    {line for line in changed.splitlines() if line.strip()}
                )
            else:
                stored_commit = "unknown"
        return {
            "captured": True,
            "worktree": str(worktree.resolve()),
            "head": head,
            "branch": branch,
            "dirty_files": status.splitlines()[:200] if status else [],
            "reported_commit_exists": commit_exists,
            "commit_files": commit_files,
            "commit_hash": stored_commit,
        }

    def latest_cursor(self, connection: Any, project_id: str) -> int:
        # 对外游标空间 = project_seq（与 Web 展示、Agent 引用一致）。
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(project_seq), 0) AS cursor FROM events WHERE project_id = ?",
                (project_id,),
            ).fetchone()["cursor"]
        )

    def snapshot(
        self,
        project_id: str,
        *,
        reports_before: str | None = None,
        reports_after: str | None = None,
        reviews_before: str | None = None,
        reviews_after: str | None = None,
        acknowledgements_before: str | None = None,
        acknowledgements_after: str | None = None,
    ) -> dict[str, Any]:
        """Project snapshot with optional list cursors for older/newer pages (#183).

        Cursor format is ``created_at|id`` (acks: ``created_at|event_id|session_id``)
        so ties on ``created_at`` resolve by primary key. Pass ``page_info.*.next``
        as ``*_before`` to fetch the older page, or ``page_info.*.before`` as
        ``*_after`` to fetch the newer page.
        """
        with self.database.connect() as connection:
            return self._snapshot(
                connection,
                project_id,
                reports_before=reports_before,
                reports_after=reports_after,
                reviews_before=reviews_before,
                reviews_after=reviews_after,
                acknowledgements_before=acknowledgements_before,
                acknowledgements_after=acknowledgements_after,
            )

    @staticmethod
    def _encode_page_cursor(row: Mapping[str, Any], *fields: str) -> str:
        return "|".join(str(row.get(field) or "") for field in fields)

    @staticmethod
    def _decode_page_cursor(cursor: str | None, expected_parts: int) -> list[str] | None:
        if not cursor:
            return None
        parts = cursor.split("|")
        if len(parts) != expected_parts or any(not part for part in parts):
            raise DomainError(
                "invalid_page_cursor",
                "Snapshot page cursor must be created_at|id (acks add event_id|session_id)",
                status_code=422,
                details={"cursor": cursor},
            )
        return parts

    def list_session_presence(self, project_id: str) -> list[dict[str, Any]]:
        """Presence fields for one project's Sessions, without the Room projection.

        The GUI polls this every few seconds per project and only ever reads
        ``id``/``name``/``status``/``left_at``. `snapshot()` additionally pages
        events, tasks with relations, reports, reviews, acknowledgements,
        leases and knowledge assets, and computes an unread counter per Session,
        so a presence check cost a full Room rebuild each time. Status still comes
        from `_agent_dict`, so the two surfaces cannot disagree about who is online.
        """
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """
                SELECT a.*, (
                    SELECT MAX(e.created_at)
                    FROM events e
                    WHERE e.project_id = a.project_id
                      AND e.actor_session_id = a.id
                ) AS last_activity_at
                FROM agent_sessions a
                WHERE a.project_id = ?
                ORDER BY a.created_at
                """,
                (project_id,),
            ).fetchall()
        return [
            {
                "id": str(agent["id"]),
                "name": str(agent.get("name") or ""),
                "status": str(agent.get("status") or ""),
                "left_at": agent.get("left_at"),
            }
            for agent in (self._agent_dict(row) for row in rows)
        ]

    def _snapshot(
        self,
        connection: Any,
        project_id: str,
        *,
        reports_before: str | None = None,
        reports_after: str | None = None,
        reviews_before: str | None = None,
        reviews_after: str | None = None,
        acknowledgements_before: str | None = None,
        acknowledgements_after: str | None = None,
    ) -> dict[str, Any]:
            project = self._project_dict(self._require_project(connection, project_id))
            agent_rows = connection.execute(
                """
                SELECT a.*, (
                    SELECT MAX(e.created_at)
                    FROM events e
                    WHERE e.project_id = a.project_id
                      AND e.actor_session_id = a.id
                ) AS last_activity_at,
                (
                    SELECT COUNT(*)
                    FROM events e
                    WHERE e.project_id = a.project_id
                      AND e.project_seq > a.last_read_cursor
                ) AS unread_count
                FROM agent_sessions a
                WHERE a.project_id = ?
                ORDER BY a.created_at
                """,
                (project_id,),
            ).fetchall()
            agents = []
            for row in agent_rows:
                agent = self._agent_dict(row)
                agent["unread_count"] = int(row["unread_count"] or 0)
                agents.append(agent)
            tasks = self._tasks_with_relations(
                connection,
                connection.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority, created_at",
                    (project_id,),
                ).fetchall(),
            )
            members = self._list_project_members(connection, project_id)
            recent_limit = int(getattr(self.settings, "snapshot_recent_limit", 200))
            leases = [
                lease
                for lease in (
                    self._lease_dict(row)
                    for row in connection.execute(
                        """
                        SELECT l.*, a.last_heartbeat AS owner_last_heartbeat
                        FROM file_leases l
                        JOIN agent_sessions a ON a.id = l.session_id
                        WHERE l.project_id = ? ORDER BY l.created_at DESC
                        """,
                        (project_id,),
                    ).fetchall()
                )
                if lease["active"]
            ]

            def _fetch_tied_page(
                table: str,
                *,
                before: str | None,
                after: str | None,
            ) -> tuple[list[dict[str, Any]], bool, bool]:
                """Return (rows, has_more_older, has_more_newer).

                has_more_* come from real boundary probes against the page window
                (not total>len, not bool(rows)) so the last page never advertises
                a fake continuation (#183). Rows are normalized newest-first
                before probing, so the boundary rows mean the same thing on
                ``*_before`` and ``*_after`` pages alike.
                """
                if before and after:
                    raise DomainError(
                        "invalid_page_cursor",
                        "Provide only one of before/after for a snapshot list page",
                        status_code=422,
                    )
                where = ["project_id = ?"]
                params: list[Any] = [project_id]
                order_time = "DESC"
                order_id = "DESC"
                cursor_parts = self._decode_page_cursor(before or after, 2)
                if cursor_parts is not None:
                    created_at, row_id = cursor_parts
                    if before:
                        where.append(
                            "(created_at < ? OR (created_at = ? AND id < ?))"
                        )
                        params.extend([created_at, created_at, row_id])
                    else:
                        where.append(
                            "(created_at > ? OR (created_at = ? AND id > ?))"
                        )
                        params.extend([created_at, created_at, row_id])
                        order_time = "ASC"
                        order_id = "ASC"
                params.append(recent_limit)
                rows = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} WHERE {' AND '.join(where)} "
                        f"ORDER BY created_at {order_time}, id {order_id} LIMIT ?",
                        params,
                    ).fetchall()
                ]
                if after:
                    rows.reverse()

                def _exists(direction: str, boundary: Mapping[str, Any]) -> bool:
                    op = ">" if direction == "newer" else "<"
                    hit = connection.execute(
                        f"SELECT 1 FROM {table} WHERE project_id = ? AND "
                        f"(created_at {op} ? OR (created_at = ? AND id {op} ?)) "
                        f"LIMIT 1",
                        (
                            project_id,
                            boundary["created_at"],
                            boundary["created_at"],
                            boundary["id"],
                        ),
                    ).fetchone()
                    return hit is not None

                if not rows:
                    return rows, False, False
                has_more_older = _exists("older", rows[-1])
                has_more_newer = _exists("newer", rows[0])
                return rows, has_more_older, has_more_newer

            def _fetch_ack_page(
                *,
                before: str | None,
                after: str | None,
            ) -> tuple[list[dict[str, Any]], bool, bool]:
                if before and after:
                    raise DomainError(
                        "invalid_page_cursor",
                        "Provide only one of before/after for a snapshot list page",
                        status_code=422,
                    )
                where = ["e.project_id = ?"]
                params: list[Any] = [project_id]
                order_time = "DESC"
                order_seq = "DESC"
                order_session = "DESC"
                cursor_parts = self._decode_page_cursor(before or after, 3)
                if cursor_parts is not None:
                    created_at, event_seq, session_id = cursor_parts
                    # Cursor encodes project_seq; compare project_seq (not e.id).
                    seq_value = int(event_seq)
                    if before:
                        where.append(
                            "(a.created_at < ? OR (a.created_at = ? AND "
                            "(e.project_seq < ? OR (e.project_seq = ? AND a.session_id < ?))))"
                        )
                        params.extend(
                            [created_at, created_at, seq_value, seq_value, session_id]
                        )
                    else:
                        where.append(
                            "(a.created_at > ? OR (a.created_at = ? AND "
                            "(e.project_seq > ? OR (e.project_seq = ? AND a.session_id > ?))))"
                        )
                        params.extend(
                            [created_at, created_at, seq_value, seq_value, session_id]
                        )
                        order_time = "ASC"
                        order_seq = "ASC"
                        order_session = "ASC"
                params.append(recent_limit)
                raw_rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT e.project_seq AS event_id, a.session_id, a.created_at, "
                        "e.id AS physical_id "
                        "FROM event_acknowledgements a "
                        "JOIN events e ON e.id = a.event_id "
                        f"WHERE {' AND '.join(where)} "
                        f"ORDER BY a.created_at {order_time}, e.project_seq {order_seq}, "
                        f"a.session_id {order_session} LIMIT ?",
                        params,
                    ).fetchall()
                ]
                rows = raw_rows[:recent_limit]
                if after:
                    rows.reverse()

                def _exists(direction: str, boundary: Mapping[str, Any]) -> bool:
                    op = ">" if direction == "newer" else "<"
                    hit = connection.execute(
                        "SELECT 1 FROM event_acknowledgements a "
                        "JOIN events e ON e.id = a.event_id "
                        "WHERE e.project_id = ? AND (a.created_at {op} ? OR "
                        "(a.created_at = ? AND (e.project_seq {op} ? OR "
                        "(e.project_seq = ? AND a.session_id {op} ?)))) "
                        "LIMIT 1".format(op=op),
                        (
                            project_id,
                            boundary["created_at"],
                            boundary["created_at"],
                            int(boundary["event_id"]),
                            int(boundary["event_id"]),
                            boundary["session_id"],
                        ),
                    ).fetchone()
                    return hit is not None

                if not rows:
                    return rows, False, False
                has_more_older = _exists("older", rows[-1])
                has_more_newer = _exists("newer", rows[0])
                for row in rows:
                    row.pop("physical_id", None)
                return rows, has_more_older, has_more_newer

            reports, reports_older, reports_newer = _fetch_tied_page(
                "work_reports", before=reports_before, after=reports_after
            )
            reports = [self._work_report_dict(report) for report in reports]

            reviews, reviews_older, reviews_newer = _fetch_tied_page(
                "reviews", before=reviews_before, after=reviews_after
            )
            reviews = [self._review_dict(review) for review in reviews]

            acknowledgements, acks_older, acks_newer = _fetch_ack_page(
                before=acknowledgements_before,
                after=acknowledgements_after,
            )

            totals = {
                "reports": int(
                    connection.execute(
                        "SELECT COUNT(*) AS c FROM work_reports WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()["c"]
                ),
                "reviews": int(
                    connection.execute(
                        "SELECT COUNT(*) AS c FROM reviews WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()["c"]
                ),
                "acknowledgements": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS c
                        FROM event_acknowledgements a
                        JOIN events e ON e.id = a.event_id
                        WHERE e.project_id = ?
                        """,
                        (project_id,),
                    ).fetchone()["c"]
                ),
            }
            cursor = self.latest_cursor(connection, project_id)
            # 历史接入会话只保留前端渲染与名称查找所需的字段，完整记录
            # 留在数据库；成员身份聚合（agent_identities）基于全量会话计算。
            agents_payload = [
                {
                    "id": agent["id"],
                    "name": agent["name"],
                    "client": agent["client"],
                    "model": agent["model"],
                    "role": agent["role"],
                    "status": agent["status"],
                    "current_task_id": agent.get("current_task_id"),
                    "current_task_title": agent.get("current_task_title"),
                    "last_heartbeat": agent["last_heartbeat"],
                    "last_activity_at": agent["last_activity_at"],
                    "unread_count": agent["unread_count"],
                    "left_at": agent["left_at"],
                    "created_at": agent["created_at"],
                }
                for agent in agents
            ]

            def _page_cursors(
                rows: list[dict[str, Any]],
                has_more_older: bool,
                has_more_newer: bool,
                *cursor_fields: str,
            ) -> dict[str, Any]:
                info: dict[str, Any] = {
                    "returned": len(rows),
                    "has_more": has_more_older,
                    "has_more_newer": has_more_newer,
                    "next": None,
                    "before": None,
                }
                if rows:
                    # High-water mark of this window: pass as *_after to fetch
                    # strictly newer rows (poll), even when nothing is newer yet.
                    info["before"] = self._encode_page_cursor(
                        rows[0], *cursor_fields
                    )
                    if has_more_older:
                        info["next"] = self._encode_page_cursor(
                            rows[-1], *cursor_fields
                        )
                return info

            reports_info = _page_cursors(
                reports, reports_older, reports_newer, "created_at", "id"
            )
            reports_info["total"] = totals["reports"]

            reviews_info = _page_cursors(
                reviews, reviews_older, reviews_newer, "created_at", "id"
            )
            reviews_info["total"] = totals["reviews"]

            acks_info = _page_cursors(
                acknowledgements,
                acks_older,
                acks_newer,
                "created_at",
                "event_id",
                "session_id",
            )
            acks_info["total"] = totals["acknowledgements"]

            return {
                "project": project,
                "members": members,
                "agents": agents_payload,
                "agent_identities": self._agent_identities(agents, tasks, members),
                "tasks": tasks,
                "leases": leases,
                "reports": reports,
                "reviews": reviews,
                "acknowledgements": acknowledgements,
                "documents": self.list_project_documents(project_id)["documents"],
                "cursor": cursor,
                "totals": totals,
                "page_info": {
                    "limit": recent_limit,
                    "reports": reports_info,
                    "reviews": reviews_info,
                    "acknowledgements": acks_info,
                },
            }

    def _work_report_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        report = dict(row)
        report["files"] = json_load(report.pop("files_json", "[]"), [])
        report["tests"] = json_load(report.pop("tests_json", "[]"), [])
        report["system_evidence"] = json_load(
            report.pop("system_evidence_json", "{}"), {}
        )
        return report

    def _review_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        review = dict(row)
        review["criteria"] = json_load(review.pop("criteria_json", "[]"), [])
        return review

    def _acknowledgement_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        acknowledgement = dict(row)
        acknowledgement.pop("physical_id", None)
        return acknowledgement

    def _export_room_writes(
        self, project_id: str, cursor: int
    ) -> dict[str, list[dict[str, Any]]]:
        """Every Work Report, review, and acknowledgement for one export.

        The snapshot lists these to a recency window, so an older report used to
        vanish from the archive without a trace. Acknowledgements are capped at
        the export cursor because that is what makes the event list complete.
        """
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            return {
                "reports": [
                    self._work_report_dict(row)
                    for row in connection.execute(
                        "SELECT * FROM work_reports WHERE project_id = ? "
                        "ORDER BY created_at ASC, id ASC",
                        (project_id,),
                    ).fetchall()
                ],
                "reviews": [
                    self._review_dict(row)
                    for row in connection.execute(
                        "SELECT * FROM reviews WHERE project_id = ? "
                        "ORDER BY created_at ASC, id ASC",
                        (project_id,),
                    ).fetchall()
                ],
                "acknowledgements": [
                    self._acknowledgement_dict(row)
                    for row in connection.execute(
                        "SELECT e.project_seq AS event_id, a.session_id, a.created_at, "
                        "e.id AS physical_id "
                        "FROM event_acknowledgements a "
                        "JOIN events e ON e.id = a.event_id "
                        "WHERE e.project_id = ? AND e.project_seq <= ? "
                        "ORDER BY a.created_at ASC, e.project_seq ASC, a.session_id ASC",
                        (project_id, cursor),
                    ).fetchall()
                ],
            }

    def _export_knowledge_assets(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            assets: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT * FROM knowledge_assets WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall():
                asset = self._knowledge_asset_dict(connection, row)
                asset["versions"] = [
                    self._knowledge_version_dict(version)
                    for version in connection.execute(
                        """
                        SELECT * FROM knowledge_asset_versions
                        WHERE asset_id = ? ORDER BY version ASC
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                asset["reviews"] = [
                    self._knowledge_review_dict(review)
                    for review in connection.execute(
                        """
                        SELECT * FROM knowledge_reviews
                        WHERE asset_id = ? ORDER BY created_at ASC
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                assets.append(asset)
            return assets

    def export_project(self, project_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(project_id)
        cursor = snapshot["cursor"]
        events: list[dict[str, Any]] = []
        after = 0
        while after < cursor:
            batch = self.list_events(project_id, after=after, limit=1000)
            current = [
                event for event in batch["events"] if event["project_seq"] <= cursor
            ]
            if not current:
                break
            events.extend(current)
            after = current[-1]["project_seq"]
        writes = self._export_room_writes(project_id, cursor)
        leases = self.list_leases(project_id, include_inactive=True)
        assets = self._export_knowledge_assets(project_id)
        return {
            "export_schema_version": 2,
            "exported_at": iso_now(),
            "project": snapshot["project"],
            "members": snapshot["members"],
            "cursor": cursor,
            "agents": snapshot["agents"],
            "tasks": snapshot["tasks"],
            "leases": leases,
            "reports": writes["reports"],
            "reviews": writes["reviews"],
            "acknowledgements": writes["acknowledgements"],
            "knowledge_assets": assets,
            "events": events,
            "counts": {
                "members": len(snapshot["members"]),
                "agents": len(snapshot["agents"]),
                "tasks": len(snapshot["tasks"]),
                "leases": len(leases),
                "reports": len(writes["reports"]),
                "reviews": len(writes["reviews"]),
                "acknowledgements": len(writes["acknowledgements"]),
                "knowledge_assets": len(assets),
                "events": len(events),
            },
        }

    # ------------------------------------------------------------------
    # Database backup / restore (product-level wrappers around backup.py)
    # ------------------------------------------------------------------

    def _backup_directory(self) -> Path:
        return Path(self.settings.data_dir) / "backups"

    def _backup_targets(self, connection: Any) -> list[str]:
        rows = connection.execute(
            "SELECT id FROM projects WHERE archived_at IS NULL"
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def _emit_backup_event(
        self,
        event_type: str,
        *,
        payload: dict[str, Any],
        actor_session_id: str | None = None,
    ) -> None:
        with self.database.connect(write=True) as connection:
            for project_id in self._backup_targets(connection):
                self._emit(
                    connection,
                    project_id,
                    event_type,
                    actor_session_id=actor_session_id,
                    payload=payload,
                )

    def create_backup(
        self,
        *,
        actor_session_id: str | None = None,
        source: str = "management",
        max_kept: int | None = None,
    ) -> dict[str, Any]:
        """Snapshot the live database into the managed backups directory."""
        with self._backup_restore_lock:
            directory = self._backup_directory()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = iso_now().replace(":", "").replace("-", "")
            target = directory / f"backup-{stamp}.sqlite"
            backend = self.settings.database_backend
            if backend == "sqlite":
                result = backup_sqlite(self.database.path, target)
            elif backend == "postgresql":
                database_url = os.getenv(self.settings.database_url_env, "")
                result = backup_postgresql(database_url, target)
            else:
                raise DomainError(
                    "backup_backend_unsupported",
                    f"Backups are not supported for backend {backend}",
                    status_code=422,
                )
            latest_event_id = self._latest_event_id()
            manifest_path = Path(result["manifest"])
            manifest = json_load(manifest_path.read_text(encoding="utf-8"), {})
            manifest["latest_event_id"] = latest_event_id
            manifest["source"] = source
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._emit_backup_event(
                "backup.created",
                payload={
                    "backend": backend,
                    "output": result["output"],
                    "manifest": str(manifest_path),
                    "latest_event_id": latest_event_id,
                    "source": source,
                },
                actor_session_id=actor_session_id,
            )
            # The audit event about this backup is part of the protected history:
            # record the cursor again so a fresh backup is never "stale".
            latest_event_id = self._latest_event_id()
            manifest["latest_event_id"] = latest_event_id
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            kept = self._prune_backups(max_kept)
            return {**result, "latest_event_id": latest_event_id, "pruned": kept}

    def _read_backup_manifest(self, manifest_path: Path) -> dict[str, Any] | None:
        """Read one manifest, returning None when it cannot be trusted.

        A single truncated, binary or unparseable manifest must not take the
        backup subsystem down: listing feeds the admin page *and* the auto
        backup worker, so an exception here would return 500 and silently stop
        automatic backups for good.
        """
        try:
            raw = manifest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _current_backup_max_kept(self) -> int:
        if self._applied_backup_max_kept is not None:
            return self._applied_backup_max_kept
        return int(getattr(self.settings, "auto_backup_max_kept", 10))

    def list_backups(self) -> list[dict[str, Any]]:
        directory = self._backup_directory()
        if not directory.is_dir():
            return []
        backups: list[dict[str, Any]] = []
        for manifest_path in sorted(directory.glob("*.manifest.json")):
            data = self._read_backup_manifest(manifest_path)
            target = manifest_path.with_name(manifest_path.name[: -len(".manifest.json")])
            if not target.name.startswith("backup-"):
                # 历史遗留快照（如迁移前 .db 备份）不属于本功能管理列表，
                # 保留在磁盘上但不进产品列表：它们多为旧 schema，已不可回滚。
                continue
            try:
                size = target.stat().st_size if target.is_file() else 0
            except OSError:
                size = 0
            backups.append(
                {
                    "file": str(target),
                    "size": size,
                    "created_at": (data or {}).get("created_at"),
                    "backend": (data or {}).get("backend"),
                    "schema_version": (data or {}).get("database_schema_version"),
                    "latest_event_id": (data or {}).get("latest_event_id"),
                    "source": (data or {}).get("source"),
                    # Surfaced so the operator can see the file needs attention
                    # instead of silently treating a broken manifest as "no
                    # cursor recorded", which reads as "stale".
                    "manifest_readable": data is not None,
                }
            )
        backups.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return backups

    def _prune_backups(self, kept_max: int | None = None) -> list[str]:
        kept_max = (
            int(kept_max) if kept_max is not None else self._current_backup_max_kept()
        )
        if kept_max < 0:
            kept_max = 0
        backups = self.list_backups()
        removed: list[str] = []
        for stale in backups[kept_max:]:
            target = Path(stale["file"])
            try:
                target.unlink(missing_ok=True)
                target.with_name(target.name + ".manifest.json").unlink(missing_ok=True)
            except OSError:
                # A locked or vanished file must not abort the rest of the
                # sweep, and must not be reported as removed.
                continue
            removed.append(str(target))
        return removed

    def enforce_backup_retention(self, max_kept: int) -> list[str]:
        """Apply the retention policy immediately (settings save path)."""
        self._applied_backup_max_kept = int(max_kept)
        return self._prune_backups(self._applied_backup_max_kept)

    def delete_backup(
        self,
        filename: str,
        *,
        actor: str | None = None,
        actor_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete a managed backup file and its manifest after strict path validation.

        Deletes must be audited. Only managed backups matching the filename pattern
        are allowed to be deleted to protect arbitrary filesystem locations and legacy snapshots.
        """
        if not filename or not re.match(r"^backup-[a-zA-Z0-9_T.-]+\.sqlite$", filename):
            raise DomainError(
                "invalid_backup_filename",
                f"Invalid backup filename: {filename}",
                status_code=400,
            )
        directory = self._backup_directory().resolve()
        target = (directory / filename).resolve()
        try:
            target.relative_to(directory)
        except ValueError:
            raise DomainError(
                "invalid_backup_filename",
                "Backup path must be within the backup directory",
                status_code=400,
            )
        if not target.is_file():
            raise DomainError(
                "backup_not_found",
                f"Backup file not found: {filename}",
                status_code=404,
            )
        size = target.stat().st_size
        manifest_path = target.with_name(target.name + ".manifest.json")
        target.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        self._emit_backup_event(
            "backup.deleted",
            payload={
                "file": str(target),
                "filename": target.name,
                "size": size,
                "actor": actor or "management",
            },
            actor_session_id=actor_session_id,
        )
        return {
            "deleted": True,
            "filename": target.name,
            "file": str(target),
            "size": size,
        }

    def _latest_event_id(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id), 0) AS latest FROM events").fetchone()
            return int(row["latest"])

    def restore_backup(
        self,
        backup_path: str,
        *,
        confirm: str = "",
        allow_data_loss: bool = False,
        actor_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a managed backup after explicit safety checks.

        Rejections and completions are audited. A successful restore rewinds
        the shared database to the snapshot; audit events written after the
        backup stop existing, which is why data-loss acknowledgement and the
        typed confirmation are mandatory.
        """
        if confirm != "REPLACE":
            raise DomainError(
                "backup_confirm_required",
                'Restore requires the typed confirmation confirm="REPLACE"',
                status_code=422,
            )
        target = Path(backup_path).expanduser().resolve()
        manifest_path = target.with_name(target.name + ".manifest.json")
        # The path arrives from a management request. A restore copies this
        # file over the live database, so an unconstrained path would let any
        # readable file on the host be installed as the Room's database. Only
        # managed snapshots inside the backups directory are restorable.
        backup_root = self._backup_directory().resolve()
        if target.parent != backup_root or not target.name.startswith("backup-"):
            self._emit_backup_event(
                "backup.restore_rejected",
                payload={"file": str(target), "reason": "backup_path_outside_root"},
                actor_session_id=actor_session_id,
            )
            raise DomainError(
                "backup_path_outside_root",
                "Only managed backups in the backups directory can be restored",
                status_code=403,
                details={"required_action": "select_a_listed_backup"},
            )
        if not target.is_file() or not manifest_path.is_file():
            self._emit_backup_event(
                "backup.restore_rejected",
                payload={"file": str(target), "reason": "backup_not_found"},
                actor_session_id=actor_session_id,
            )
            raise DomainError(
                "backup_not_found",
                f"Backup or manifest does not exist: {target}",
                status_code=404,
            )
        manifest = self._read_backup_manifest(manifest_path)
        if manifest is None:
            self._emit_backup_event(
                "backup.restore_rejected",
                payload={"file": str(target), "reason": "backup_manifest_unreadable"},
                actor_session_id=actor_session_id,
            )
            raise DomainError(
                "backup_manifest_unreadable",
                "The backup manifest cannot be parsed; refusing to restore an "
                "unverifiable snapshot",
                status_code=409,
                details={"required_action": "delete_backup_and_retake"},
            )
        current_schema = SCHEMA_VERSION
        if int(manifest.get("database_schema_version") or 0) != current_schema:
            self._emit_backup_event(
                "backup.restore_rejected",
                payload={
                    "file": str(target),
                    "reason": "backup_schema_mismatch",
                    "backup_schema": manifest.get("database_schema_version"),
                    "current_schema": current_schema,
                },
                actor_session_id=actor_session_id,
            )
            raise DomainError(
                "backup_schema_mismatch",
                "Backup schema version does not match the current schema",
                status_code=409,
                details={"backup_schema": manifest.get("database_schema_version"), "current_schema": current_schema},
            )
        # A manifest with no recorded cursor is not the same as a cursor of 0.
        # CLI-produced backups never wrote one, and reading that as 0 made
        # every such backup look older than any non-empty database.
        raw_cursor = manifest.get("latest_event_id")
        backup_event_id = (
            int(raw_cursor)
            if isinstance(raw_cursor, int) and not isinstance(raw_cursor, bool)
            else None
        )
        with self._backup_restore_lock:
            if self.settings.database_backend == "sqlite":
                # Hold process mutex + SQLite EXCLUSIVE locking from the stale
                # check through the page copy so concurrent writers cannot land
                # in the window that allow_data_loss=False claims is empty.
                # Same configured wait the adapter layer uses; this used to be a
                # third copy of a hardcoded 10 seconds.
                sqlite_wait_seconds = float(
                    self.settings.database_connect_timeout_seconds
                )
                live_connection = sqlite3.connect(
                    self.database.path, timeout=sqlite_wait_seconds
                )
                try:
                    live_connection.execute(
                        f"PRAGMA busy_timeout={int(sqlite_wait_seconds * 1000)}"
                    )
                    live_connection.execute("PRAGMA locking_mode=EXCLUSIVE")
                    row = live_connection.execute(
                        "SELECT COALESCE(MAX(id), 0) AS latest FROM events"
                    ).fetchone()
                    latest_event_id = int(row[0])
                    stale = (
                        backup_event_id is not None
                        and backup_event_id < latest_event_id
                    )
                    unknown_cursor = backup_event_id is None
                    if (stale or unknown_cursor) and not allow_data_loss:
                        live_connection.close()
                        live_connection = None
                        reason = "backup_stale" if stale else "backup_cursor_unknown"
                        self._emit_backup_event(
                            "backup.restore_rejected",
                            payload={
                                "file": str(target),
                                "reason": reason,
                                "backup_latest_event_id": backup_event_id,
                                "current_latest_event_id": latest_event_id,
                            },
                            actor_session_id=actor_session_id,
                        )
                        if unknown_cursor:
                            raise DomainError(
                                "backup_cursor_unknown",
                                "The backup manifest records no event cursor, so it "
                                "cannot be proven current; pass allow_data_loss to "
                                "restore it anyway",
                                status_code=409,
                                details={
                                    "backup_latest_event_id": None,
                                    "current_latest_event_id": latest_event_id,
                                    "required_action": "retake_backup_or_allow_data_loss",
                                },
                            )
                        raise DomainError(
                            "backup_stale",
                            "The backup is older than the current database; pass allow_data_loss "
                            "to accept losing newer events",
                            status_code=409,
                            details={
                                "backup_latest_event_id": backup_event_id,
                                "current_latest_event_id": latest_event_id,
                            },
                        )
                    source_connection = sqlite3.connect(
                        f"file:{target.as_posix()}?mode=ro", uri=True
                    )
                    try:
                        # Dest is the same connection that already holds the
                        # EXCLUSIVE lock and has no open transaction.
                        source_connection.backup(live_connection)
                    finally:
                        source_connection.close()
                except sqlite3.Error as error:
                    if live_connection is not None:
                        live_connection.close()
                    raise DomainError(
                        "database_busy",
                        f"The live database is busy; stop writers before restoring: {error}",
                        status_code=409,
                    ) from error
                finally:
                    if live_connection is not None:
                        live_connection.close()
                self._emit_backup_event(
                    "backup.restore_started",
                    payload={"file": str(target), "stale": stale},
                    actor_session_id=actor_session_id,
                )
            else:
                latest_event_id = self._latest_event_id()
                stale = (
                    backup_event_id is not None
                    and backup_event_id < latest_event_id
                )
                unknown_cursor = backup_event_id is None
                if (stale or unknown_cursor) and not allow_data_loss:
                    reason = "backup_stale" if stale else "backup_cursor_unknown"
                    self._emit_backup_event(
                        "backup.restore_rejected",
                        payload={
                            "file": str(target),
                            "reason": reason,
                            "backup_latest_event_id": backup_event_id,
                            "current_latest_event_id": latest_event_id,
                        },
                        actor_session_id=actor_session_id,
                    )
                    if unknown_cursor:
                        raise DomainError(
                            "backup_cursor_unknown",
                            "The backup manifest records no event cursor, so it "
                            "cannot be proven current; pass allow_data_loss to "
                            "restore it anyway",
                            status_code=409,
                            details={
                                "backup_latest_event_id": None,
                                "current_latest_event_id": latest_event_id,
                                "required_action": "retake_backup_or_allow_data_loss",
                            },
                        )
                    raise DomainError(
                        "backup_stale",
                        "The backup is older than the current database; pass allow_data_loss "
                        "to accept losing newer events",
                        status_code=409,
                        details={
                            "backup_latest_event_id": backup_event_id,
                            "current_latest_event_id": latest_event_id,
                        },
                    )
                database_url = os.getenv(self.settings.database_url_env, "")
                # pg_restore rewrites the database it targets, so an audit row
                # written before it runs is destroyed by its own restore. The
                # started/completed trail is emitted around the call below, the
                # same way the SQLite branch does it.
                restore_postgresql(database_url, target)
                self._emit_backup_event(
                    "backup.restore_started",
                    payload={"file": str(target), "stale": stale},
                    actor_session_id=actor_session_id,
                )
        self._emit_backup_event(
            "backup.restore_completed",
            payload={"file": str(target), "restored_latest_event_id": backup_event_id},
            actor_session_id=actor_session_id,
        )
        return {
            "backend": self.settings.database_backend,
            "input": str(target),
            "restored": True,
            "data_loss": stale,
        }

    def _ensure_sqlite_restorable(self, backup_path: Path) -> None:
        """Fail loudly when the live SQLite database cannot take a restore."""
        probe = sqlite3.connect(self.database.path, timeout=0.5)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
        except sqlite3.Error as error:
            raise DomainError(
                "database_busy",
                f"The live database is busy; stop writers before restoring: {error}",
                status_code=409,
            ) from error
        finally:
            probe.close()

    # ------------------------------------------------------------------
    # Project documents: versioned binding/reference docs injected to Agents
    # ------------------------------------------------------------------

    PROJECT_DOCUMENT_KINDS = ("binding", "reference")

    def list_project_documents(self, project_id: str, *, include_archived: bool = False) -> dict[str, Any]:
        clause = "" if include_archived else "AND h.archived_at IS NULL"
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                f"""
                SELECT h.doc_key, h.kind, h.title, h.current_version, h.archived_at,
                       h.updated_at, d.size
                FROM project_document_heads h
                JOIN project_documents d
                  ON d.project_id = h.project_id AND d.doc_key = h.doc_key
                 AND d.version = h.current_version
                WHERE h.project_id = ? {clause}
                ORDER BY h.kind, h.doc_key
                """,
                (project_id,),
            ).fetchall()
            return {
                "documents": [
                    {
                        "doc_key": row["doc_key"],
                        "kind": row["kind"],
                        "title": row["title"],
                        "version": row["current_version"],
                        "size": row["size"],
                        "archived_at": row["archived_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
            }

    def get_project_document(
        self, project_id: str, doc_key: str, *, version: int | None = None
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            head = connection.execute(
                "SELECT * FROM project_document_heads WHERE project_id = ? AND doc_key = ?",
                (project_id, doc_key),
            ).fetchone()
            if head is None:
                raise DomainError(
                    "project_document_not_found",
                    "Project document does not exist",
                    status_code=404,
                )
            wanted = int(version) if version else int(head["current_version"])
            row = connection.execute(
                """
                SELECT * FROM project_documents
                WHERE project_id = ? AND doc_key = ? AND version = ?
                """,
                (project_id, doc_key, wanted),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "project_document_not_found",
                    "Requested document version does not exist",
                    status_code=404,
                )
            history = connection.execute(
                """
                SELECT version, size, created_at, created_by
                FROM project_documents
                WHERE project_id = ? AND doc_key = ?
                ORDER BY version DESC
                """,
                (project_id, doc_key),
            ).fetchall()
            return {
                "document": {
                    "doc_key": doc_key,
                    "kind": row["kind"],
                    "title": row["title"],
                    "version": row["version"],
                    "content": row["content"],
                    "size": row["size"],
                    "created_at": row["created_at"],
                    "created_by": row["created_by"],
                    "current_version": head["current_version"],
                    "archived_at": head["archived_at"],
                    "history": [dict(item) for item in history],
                }
            }

    @idempotent_write("project.document.upsert")
    def upsert_project_document(
        self,
        project_id: str,
        *,
        doc_key: str,
        kind: str,
        title: str,
        content: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        key = str(doc_key or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key):
            raise DomainError(
                "invalid_project_document_key",
                "doc_key must match ^[a-z0-9][a-z0-9_-]{0,63}$",
                status_code=422,
            )
        if kind not in self.PROJECT_DOCUMENT_KINDS:
            raise DomainError(
                "invalid_project_document_kind",
                "kind must be one of: " + ", ".join(self.PROJECT_DOCUMENT_KINDS),
                status_code=422,
            )
        clean_title = str(title or "").strip()
        clean_content = str(content or "")
        content_bytes = len(clean_content.encode("utf-8"))
        max_bytes = int(getattr(self.settings, "project_document_max_bytes", 262144))
        if content_bytes > max_bytes:
            raise DomainError(
                "project_document_too_large",
                "Document content exceeds project_document_max_bytes "
                f"({max_bytes})",
                status_code=422,
                details={"limit": max_bytes, "size": content_bytes},
            )
        if not clean_title:
            raise DomainError(
                "invalid_project_document", "Document title is required", status_code=422
            )
        if not clean_content.strip():
            raise DomainError(
                "invalid_project_document", "Document content is required", status_code=422
            )
        now = iso_now()
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            head = connection.execute(
                "SELECT * FROM project_document_heads WHERE project_id = ? AND doc_key = ?",
                (project_id, key),
            ).fetchone()
            created = head is None
            next_version = int(head["current_version"]) + 1 if head else 1
            document_id = new_id("doc")
            connection.execute(
                """
                INSERT INTO project_documents(
                    id, project_id, doc_key, kind, title, version, content, size,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    project_id,
                    key,
                    kind,
                    clean_title,
                    next_version,
                    clean_content,
                    len(clean_content.encode("utf-8")),
                    actor,
                    now,
                ),
            )
            if head is None:
                connection.execute(
                    """
                    INSERT INTO project_document_heads(
                        project_id, doc_key, current_version, kind, title, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (project_id, key, next_version, kind, clean_title, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE project_document_heads
                    SET current_version = ?, kind = ?, title = ?, updated_at = ?,
                        archived_at = NULL
                    WHERE project_id = ? AND doc_key = ?
                    """,
                    (next_version, kind, clean_title, now, project_id, key),
                )
            self._emit(
                connection,
                project_id,
                "document.created" if created else "document.updated",
                payload={
                    "doc_key": key,
                    "kind": kind,
                    "title": clean_title,
                    "version": next_version,
                    "size": len(clean_content.encode("utf-8")),
                    "actor": actor,
                },
            )
            return self.get_project_document(project_id, key)

    def archive_project_document(
        self, project_id: str, doc_key: str, *, actor: str | None = None
    ) -> dict[str, Any]:
        now = iso_now()
        with self.database.connect(write=True) as connection:
            head = connection.execute(
                "SELECT * FROM project_document_heads WHERE project_id = ? AND doc_key = ?",
                (project_id, doc_key),
            ).fetchone()
            if head is None:
                raise DomainError(
                    "project_document_not_found",
                    "Project document does not exist",
                    status_code=404,
                )
            connection.execute(
                """
                UPDATE project_document_heads
                SET archived_at = ?, updated_at = ?
                WHERE project_id = ? AND doc_key = ?
                """,
                (now, now, project_id, doc_key),
            )
            self._emit(
                connection,
                project_id,
                "document.archived",
                payload={"doc_key": doc_key, "actor": actor},
            )
        return self.get_project_document(project_id, doc_key)

    def _binding_document_snapshot(self, connection: Any, project_id: str) -> dict[str, int]:
        """Versions of active binding documents, recorded on task claim."""
        rows = connection.execute(
            """
            SELECT doc_key, current_version FROM project_document_heads
            WHERE project_id = ? AND kind = 'binding' AND archived_at IS NULL
            ORDER BY doc_key
            """,
            (project_id,),
        ).fetchall()
        return {row["doc_key"]: row["current_version"] for row in rows}

    def _latest_claim_document_snapshot(
        self, connection: Any, project_id: str, task_id: str
    ) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT payload_json FROM events
            WHERE project_id = ? AND task_id = ? AND event_type = 'task.claimed'
            ORDER BY id DESC LIMIT 1
            """,
            (project_id, task_id),
        ).fetchone()
        if row is None:
            return {}
        return json_load(row["payload_json"], {}).get("project_documents") or {}

    def injectable_project_documents(
        self, project_id: str, *, connection: Any = None
    ) -> dict[str, Any]:
        """Binding documents for claim/get responses, capped by configuration.

        Content is included while the aggregate stays under the configured
        inject limit; remaining documents degrade to manifest entries with a
        fetch hint so Agents can pull them on demand via project_document_get.
        """
        limit = int(self.settings.project_doc_inject_max_chars)
        if connection is not None:
            rows = connection.execute(
                """
                SELECT d.doc_key, d.kind, d.title, d.version, d.content, d.size
                FROM project_document_heads h
                JOIN project_documents d
                  ON d.project_id = h.project_id AND d.doc_key = h.doc_key
                 AND d.version = h.current_version
                WHERE h.project_id = ? AND h.kind = 'binding' AND h.archived_at IS NULL
                ORDER BY d.size
                """,
                (project_id,),
            ).fetchall()
        else:
            with self.database.connect() as standalone:
                rows = standalone.execute(
                    """
                    SELECT d.doc_key, d.kind, d.title, d.version, d.content, d.size
                    FROM project_document_heads h
                    JOIN project_documents d
                      ON d.project_id = h.project_id AND d.doc_key = h.doc_key
                     AND d.version = h.current_version
                    WHERE h.project_id = ? AND h.kind = 'binding' AND h.archived_at IS NULL
                    ORDER BY d.size
                    """,
                    (project_id,),
                ).fetchall()
        documents = []
        budget = limit
        truncated = False
        for row in rows:
            entry = {
                "doc_key": row["doc_key"],
                "kind": row["kind"],
                "title": row["title"],
                "version": row["version"],
                "size": row["size"],
            }
            if not truncated and row["size"] <= budget:
                entry["content"] = row["content"]
                budget -= row["size"]
            else:
                truncated = True
                entry["content"] = None
                entry["hint"] = (
                    "content omitted (inject budget); call project_document_get "
                    f"for doc_key={row['doc_key']}"
                )
            documents.append(entry)
        return {"documents": documents, "inject_limit_chars": limit}

    # ------------------------------------------------------------------
    # Audit retention (project setting audit_retention_days; 0 = permanent)
    # ------------------------------------------------------------------

    def _project_retention_days(self, row: Mapping[str, Any]) -> int:
        settings = json_load(row["settings_json"], {}) or {}
        return int(
            settings.get(
                "audit_retention_days",
                PROJECT_SETTINGS_DEFAULTS["audit_retention_days"],
            )
        )

    def _audit_retention_protected_ids(
        self, connection: Any, project_id: str
    ) -> set[int]:
        """Event ids a retention purge must not remove.

        Three things still point at events. An acknowledgement cascades away
        with its event, erasing "who confirmed what, when" while a later
        message.acknowledged event keeps citing the now-missing number. Each
        task's spec_receipt is derived by scanning the newest task.claimed that
        carries the project-document snapshot, so purging that event silently
        degrades the receipt to {} — a permanent false "this task had no bound
        documents". And the audit.purged receipt records the purge, so deleting
        it would leave no surviving evidence that the cleanup ever happened.
        """
        protected: set[int] = set()
        for row in connection.execute(
            """
            SELECT a.event_id AS event_id
            FROM event_acknowledgements a
            JOIN events e ON e.id = a.event_id
            WHERE e.project_id = ?
            """,
            (project_id,),
        ).fetchall():
            protected.add(int(row["event_id"]))
        for row in connection.execute(
            """
            SELECT id FROM events
            WHERE project_id = ? AND event_type = 'audit.purged'
            """,
            (project_id,),
        ).fetchall():
            protected.add(int(row["id"]))
        for row in connection.execute(
            """
            SELECT id FROM events
            WHERE project_id = ?
              AND event_type = 'task.claimed'
              AND payload_json LIKE '%"project_documents"%'
              AND id = (
                  SELECT MAX(inner_e.id) FROM events inner_e
                  WHERE inner_e.project_id = events.project_id
                    AND inner_e.task_id = events.task_id
                    AND inner_e.event_type = 'task.claimed'
                    AND inner_e.payload_json LIKE '%"project_documents"%'
              )
            """,
            (project_id,),
        ).fetchall():
            protected.add(int(row["id"]))
        return protected

    def enforce_audit_retention(self, project_id: str) -> dict[str, Any]:
        """Delete audit events older than the project retention policy.

        Only whole expired events are removed by created_at; retained events
        are never rewritten or reordered. The purge is audited after the
        deletion so the cleanup leaves a surviving trace, and that trace is
        itself excluded from later purges — otherwise the receipt would be
        destroyed by the very statement that records it. Events still
        referenced by an acknowledgement or by a task's spec receipt are
        deferred rather than cascading into a dangling reference.
        """
        with self.database.connect(write=True) as connection:
            row = self._require_project(connection, project_id)
            retention_days = self._project_retention_days(row)
            if retention_days <= 0:
                return {"purged": 0, "retention_days": 0, "purged_before": None}
            cutoff = (utc_now() - timedelta(days=retention_days)).isoformat().replace(
                "+00:00", "Z"
            )
            protected = sorted(self._audit_retention_protected_ids(connection, project_id))
            base_scope = (
                "project_id = ? AND created_at < ? AND event_type != 'audit.purged'"
            )
            if protected:
                # NOT IN (NULL) is never true, so the clause can only be added
                # when there is at least one protected id.
                placeholders = ",".join("?" for _ in protected)
                scope = f"{base_scope} AND id NOT IN ({placeholders})"
                params = (project_id, cutoff, *protected)
            else:
                scope = base_scope
                params = (project_id, cutoff)
            bounds = connection.execute(
                f"SELECT MIN(id) AS first_id, MAX(id) AS last_id FROM events WHERE {scope}",
                params,
            ).fetchone()
            cursor = connection.execute(
                f"DELETE FROM events WHERE {scope}",
                params,
            )
            purged = max(0, int(cursor.rowcount))
            purged_before = int(bounds["last_id"] or 0) if purged else None
            if purged:
                self._emit(
                    connection,
                    project_id,
                    "audit.purged",
                    payload={
                        "retention_days": retention_days,
                        "cutoff": cutoff,
                        "deleted_events": purged,
                        "purged_from": int(bounds["first_id"] or 0),
                        "purged_before": purged_before,
                        "deferred_referenced": len(protected),
                    },
                )
            return {
                "purged": purged,
                "retention_days": retention_days,
                "purged_before": purged_before,
                "deferred_referenced": len(protected),
            }

    def enforce_audit_retention_throttled(self, project_id: str) -> dict[str, Any]:
        """Retention sweep for read paths: at most once per hour per project."""
        now = time.monotonic()
        if now < self._retention_next_run.get(project_id, 0.0):
            return {"purged": 0, "retention_days": None, "skipped": True}
        self._retention_next_run[project_id] = (
            now + self.retention_sweep_interval_seconds()
        )
        result = self.enforce_audit_retention(project_id)
        result["idempotency"] = self.enforce_idempotency_retention()
        return result
