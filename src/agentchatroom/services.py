from __future__ import annotations

import atexit
import fnmatch
import hashlib
import hmac
import inspect
import json
import logging
import os
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
from .database import DatabaseBackend
from .errors import DomainError
from .project_registration import derive_logical_path
from .task_history import (
    TASK_HISTORY_LIMIT_MAX,
    TASK_HISTORY_SCHEMA_VERSION,
    actor_snapshot,
    project_history_item,
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
}
DEFAULT_AGENT_PERMISSIONS = AGENT_PERMISSIONS - {"audit:read"}
PROJECT_SETTINGS_DEFAULTS: dict[str, Any] = {
    "lease_conflict_policy": "advisory",
    "roles": [],
    "extensions": {},
}
SOFTWARE_MEMBER_PREFIX = "software:"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SENSITIVE_REQUEST_FIELDS = {
    "token",
    "agent_token",
    "admin_token",
    "password",
    "secret",
    "authorization",
    "bearer",
    "cookie",
    "api_key",
}
logger = logging.getLogger(__name__)

_UNCLASSIFIED_TASK_VIEW_WARNED: set[str] = set()


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
    return {
        "lease_conflict_policy": policy,
        "roles": list(dict.fromkeys(role.strip() for role in roles)),
        "extensions": extensions,
    }


def normalize_remote(remote: str) -> str:
    value = remote.strip().replace("\\", "/")
    value = re.sub(r"^git@([^:]+):", r"https://\1/", value)
    value = re.sub(r"\.git$", "", value)
    return value.rstrip("/").lower()


def normalize_pattern(pattern: str) -> str:
    value = pattern.strip().replace("\\", "/")
    value = re.sub(r"^\./", "", value)
    value = re.sub(r"/+", "/", value).strip("/")
    if not value or value.startswith("../") or "/../" in f"/{value}/":
        raise DomainError("invalid_path_pattern", "Path pattern must stay inside the project")
    return value


def lease_modes_conflict(left: str, right: str) -> bool:
    if "readonly" in {left, right}:
        return False
    return "exclusive" in {left, right}


def _static_prefix(pattern: str) -> str:
    wildcard = min(
        [index for token in "*[?" if (index := pattern.find(token)) >= 0]
        or [len(pattern)]
    )
    return pattern[:wildcard].rstrip("/")


def patterns_overlap(left: str, right: str) -> bool:
    left = normalize_pattern(left)
    right = normalize_pattern(right)
    if left == right or fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left):
        return True
    left_prefix = _static_prefix(left)
    right_prefix = _static_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return (
        left_prefix == right_prefix
        or left_prefix.startswith(f"{right_prefix}/")
        or right_prefix.startswith(f"{left_prefix}/")
    )


def _project_git_info(root: Path) -> tuple[str, Path]:
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
        return remote, Path(top).resolve() if top else root
    except (OSError, subprocess.SubprocessError):
        return "", root


def _project_scope(
    remote: str, git_root: Path, logical_path: str
) -> tuple[str, str, str]:
    if remote:
        return ("git", normalize_remote(remote), logical_path)
    return ("path", os.path.normcase(str(git_root.resolve())), logical_path)


def _stored_project_scope(project: Mapping[str, Any]) -> tuple[str, str, str]:
    logical_path = str(project["logical_path"] or "")
    remote = str(project["git_remote"] or "")
    if remote:
        return ("git", normalize_remote(remote), logical_path)
    root_path = Path(str(project["root_path"])).expanduser().resolve()
    return ("path", os.path.normcase(str(root_path)), logical_path)


class AgentChatRoomService:
    def __init__(self, database: DatabaseBackend, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self._token_touch = TokenTouchBuffer(
            self,
            interval_seconds=settings.token_touch_interval_seconds,
            min_calls=settings.token_touch_min_calls,
        )

    def initialize(self) -> None:
        self.database.initialize()

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
            result = action()
            if not isinstance(result, dict):
                raise RuntimeError("Idempotent write results must be objects")
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
        if task_id and "task_number" not in event_payload:
            task = connection.execute(
                "SELECT task_number FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            ).fetchone()
            if task is not None and task["task_number"] is not None:
                event_payload["task_number"] = int(task["task_number"])
        row = connection.execute(
            """
            INSERT INTO events(project_id, event_type, actor_session_id, task_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                project_id,
                event_type,
                actor_session_id,
                task_id,
                json_dump(event_payload),
                iso_now(),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Event insert did not return an id")
        return int(row["id"])

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
        return data

    def _project_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["settings"] = normalize_project_settings(
            json_load(data.pop("settings_json"), {})
        )
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
            expected_client = str(metadata.get("client") or "").strip().casefold()
            if expected_client and expected_client != client.strip().casefold():
                raise DomainError(
                    "software_identity_client_mismatch",
                    "The software identity is already bound to another client",
                    status_code=409,
                )
            return member, False

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

    def _replace_identity_sessions(
        self,
        connection: Any,
        project_id: str,
        *,
        member_id: str,
        session_id: str,
        now: str,
    ) -> dict[str, Any]:
        previous = connection.execute(
            """
            SELECT id FROM agent_sessions
            WHERE project_id = ? AND member_id = ? AND id <> ? AND left_at IS NULL
            ORDER BY created_at
            """,
            (project_id, member_id, session_id),
        ).fetchall()
        previous_ids = [str(row["id"]) for row in previous]
        identity_sessions = connection.execute(
            """
            SELECT id FROM agent_sessions
            WHERE project_id = ? AND member_id = ? AND id <> ?
            ORDER BY created_at
            """,
            (project_id, member_id, session_id),
        ).fetchall()
        identity_session_ids = [str(row["id"]) for row in identity_sessions]
        transferred_task_ids: list[str] = []
        transferred_lease_ids: list[str] = []
        if not identity_session_ids:
            return {
                "previous_session_ids": [],
                "transferred_task_ids": [],
                "transferred_lease_ids": [],
            }

        for previous_id in identity_session_ids:
            task_rows = connection.execute(
                """
                SELECT id FROM tasks
                WHERE project_id = ? AND owner_session_id = ?
                  AND execution_status <> 'cancelled' AND integration_status <> 'done'
                """,
                (project_id, previous_id),
            ).fetchall()
            transferred_task_ids.extend(str(row["id"]) for row in task_rows)
            connection.execute(
                """
                UPDATE tasks SET owner_session_id = ?, updated_at = ?
                WHERE project_id = ? AND owner_session_id = ?
                  AND execution_status <> 'cancelled' AND integration_status <> 'done'
                """,
                (session_id, now, project_id, previous_id),
            )
            lease_rows = connection.execute(
                """
                SELECT id FROM file_leases
                WHERE project_id = ? AND session_id = ? AND released_at IS NULL
                """,
                (project_id, previous_id),
            ).fetchall()
            transferred_lease_ids.extend(str(row["id"]) for row in lease_rows)
            connection.execute(
                """
                UPDATE file_leases SET session_id = ?, renewed_at = ?
                WHERE project_id = ? AND session_id = ? AND released_at IS NULL
                """,
                (session_id, now, project_id, previous_id),
            )
            connection.execute(
                """
                UPDATE task_assignments SET assigned_to_session_id = ?
                WHERE project_id = ? AND assigned_to_session_id = ? AND status = 'pending'
                """,
                (session_id, project_id, previous_id),
            )
            connection.execute(
                """
                UPDATE task_handoffs SET from_session_id = ?
                WHERE project_id = ? AND from_session_id = ? AND status = 'pending'
                """,
                (session_id, project_id, previous_id),
            )
            connection.execute(
                """
                UPDATE task_handoffs SET to_session_id = ?
                WHERE project_id = ? AND to_session_id = ? AND status = 'pending'
                """,
                (session_id, project_id, previous_id),
            )
        connection.execute(
            """
            UPDATE agent_sessions SET status = 'offline', left_at = ?
            WHERE project_id = ? AND member_id = ? AND id <> ? AND left_at IS NULL
            """,
            (now, project_id, member_id, session_id),
        )
        return {
            "previous_session_ids": previous_ids,
            "transferred_task_ids": sorted(set(transferred_task_ids)),
            "transferred_lease_ids": sorted(set(transferred_lease_ids)),
        }

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
        self, connection: Any, current: Mapping[str, Any], next_status: str
    ) -> None:
        if next_status not in TASK_INTAKE_STATUSES:
            raise DomainError("invalid_task_intake_status", "Unsupported intake status")
        if next_status not in TASK_INTAKE_TRANSITIONS[current["status"]]:
            raise DomainError(
                "invalid_task_intake_transition",
                f"Task intake cannot move from {current['status']} to {next_status}",
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
        data["source_event_ids"] = [
            int(item) for item in json_load(data.pop("source_event_ids_json"), [])
        ]
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
            assembled.append(data)
        return assembled

    def _lease_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        owner_last_heartbeat = data.pop("owner_last_heartbeat", None)
        owner_online = owner_last_heartbeat is None or (
            utc_now() - parse_time(owner_last_heartbeat)
            <= timedelta(seconds=self.settings.heartbeat_timeout_seconds)
        )
        ttl_active = data["released_at"] is None and parse_time(
            data["expires_at"]
        ) > utc_now()
        data["active"] = ttl_active and owner_online
        data["reclaimable"] = ttl_active and not owner_online
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
        remote, git_root = _project_git_info(root)
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
                return self._project_dict(active_conflicts[0])
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
                return self._project_dict(restored)
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
            return self._project_dict(row)

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
        remote, git_root = _project_git_info(root)
        logical = derive_logical_path(root, git_root, logical_path)
        candidate_scope = _project_scope(remote, git_root, logical)
        with self.database.connect() as connection:
            scope_projects = [
                project
                for project in connection.execute("SELECT * FROM projects").fetchall()
                if _stored_project_scope(project) == candidate_scope
            ]
            active_scope = [
                project for project in scope_projects if project["archived_at"] is None
            ]
            if len(active_scope) == 1:
                return self._project_dict(active_scope[0])
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
            return [self._project_dict(row) for row in rows]

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
        with self.database.connect() as connection:
            return self._project_dict(self._require_project(connection, project_id))

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
            return {
                "project": self._project_dict(row),
                "event_id": event_id if changed_fields else None,
                "cursor": event_id,
            }

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
    ) -> dict[str, Any]:
        clean_key = member_key.strip()
        clean_name = name.strip()
        clean_kind = kind.strip()
        clean_role = role.strip()
        clean_status = self._validate_member_status(status)
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
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            member = self._require_member(connection, project_id, member_id)
            if actor_session_id:
                self._authenticate(connection, project_id, actor_session_id, token or "")
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
            event_id = self._emit(
                connection,
                project_id,
                "member.revoked" if clean_status == "revoked" else "member.updated",
                actor_session_id=actor_session_id,
                payload={
                    "member_id": member_id,
                    "changed_fields": changed_fields,
                    "status": clean_status,
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

    @idempotent_write("project_member.revoke")
    def revoke_project_member(
        self,
        project_id: str,
        member_id: str,
        *,
        actor_session_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        return self.update_project_member(
            project_id,
            member_id,
            status="revoked",
            actor_session_id=actor_session_id,
            token=token,
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
        selected = sorted(set(permissions or DEFAULT_AGENT_PERMISSIONS))
        unknown = sorted(set(selected) - AGENT_PERMISSIONS)
        if unknown:
            raise DomainError(
                "invalid_agent_permissions",
                "Agent token contains unsupported permissions",
                details={"unknown": unknown},
            )
        ttl = expires_in_seconds or self.settings.agent_token_ttl_seconds
        if not 300 <= ttl <= self.settings.max_agent_token_ttl_seconds:
            raise DomainError(
                "invalid_agent_token_ttl",
                "Agent token TTL is outside the configured range",
                details={
                    "minimum": 300,
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
                    "agent_token_revoked", "Agent token has been revoked", status_code=401
                )
            if parse_time(row["expires_at"]) <= utc_now():
                raise DomainError(
                    "agent_token_expired", "Agent token has expired", status_code=401
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

    def rotate_agent_token(
        self,
        project_id: str,
        credential_id: str,
        *,
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        ttl = expires_in_seconds or self.settings.agent_token_ttl_seconds
        if not 300 <= ttl <= self.settings.max_agent_token_ttl_seconds:
            raise DomainError(
                "invalid_agent_token_ttl",
                "Agent token TTL is outside the configured range",
                details={
                    "minimum": 300,
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
    ) -> dict[str, Any]:
        if not name.strip() or not client.strip():
            raise DomainError("invalid_agent", "Agent name and client are required")
        if not model.strip():
            raise DomainError(
                "invalid_agent_model",
                "Agent model code is required; use unknown only when unavailable",
            )
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
            if credential_id:
                credential = connection.execute(
                    """
                    SELECT id, member_id FROM agent_credentials
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
            # Adopt matching pre-managed Sessions so historical role/name aliases
            # remain audit history under the software installation that created them.
            connection.execute(
                """
                UPDATE agent_sessions SET member_id = ?, agent_key = ?
                WHERE project_id = ? AND member_id IS NULL
                  AND lower(trim(client)) = lower(trim(?))
                """,
                (member_id, canonical_agent_key, project_id, client),
            )
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
                    branch.strip(),
                    worktree.strip(),
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
            replacement = self._replace_identity_sessions(
                connection,
                project_id,
                member_id=canonical_agent_key,
                session_id=session_id,
                now=now,
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
            if replacement["previous_session_ids"]:
                self._emit(
                    connection,
                    project_id,
                    "agent.session_replaced",
                    actor_session_id=session_id,
                    payload=replacement,
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
                    "member_id": member_id,
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
                "replaced": replacement,
                "token": token,
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
        agent_key: str,
        name: str,
        client: str,
        model: str,
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
        joined = self.join_room(
            project_id,
            agent_key=agent_key,
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
            member_id=credential.get("member_id"),
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
        rows = connection.execute(
            """
            SELECT * FROM events
            WHERE project_id = ? AND id > ?
            ORDER BY id ASC LIMIT ?
            """,
            (project_id, after, limit),
        ).fetchall()
        events = [self._event_dict(row, connection) for row in rows]
        cursor = events[-1]["id"] if events else after
        latest = connection.execute(
            "SELECT COALESCE(MAX(id), 0) AS cursor FROM events WHERE project_id = ?",
            (project_id,),
        ).fetchone()["cursor"]
        return {"events": events, "cursor": cursor, "latest_cursor": latest}

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
        limit: int = 200,
        event_type: str | None = None,
        actor_session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 1000))
        clauses = ["project_id = ?", "id > ?"]
        parameters: list[Any] = [project_id, after]
        if event_type:
            clauses.append("event_type = ?")
            parameters.append(event_type)
        if actor_session_id:
            clauses.append("actor_session_id = ?")
            parameters.append(actor_session_id)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        parameters.append(limit)
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE {' AND '.join(clauses)}
                ORDER BY id ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
            events = [self._event_dict(row, connection) for row in rows]
            cursor = events[-1]["id"] if events else after
            latest = self.latest_cursor(connection, project_id)
            return {
                "events": events,
                "cursor": cursor,
                "latest_cursor": latest,
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
                page_clauses.append("id > ?")
                page_parameters.append(after)
            if before:
                page_clauses.append("id < ?")
                page_parameters.append(before)
            order = "DESC" if descending or (before and not after) else "ASC"
            if after == 0 and before == 0:
                order = "DESC"
            page_parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE {' AND '.join(page_clauses)}
                ORDER BY id {order} LIMIT ?
                """,
                page_parameters,
            ).fetchall()
            events = [self._event_dict(row, connection) for row in rows]
            if order == "DESC":
                events.reverse()
            items = self._project_task_history_items(connection, events)
            next_after = items[-1]["event_id"] if items else after
            next_before = items[0]["event_id"] if items else before
            # The Room-wide cursor moves with unrelated tasks, so forward
            # pagination bounds must be computed from THIS task's events:
            # only an event of this task after ``next_after`` means more.
            task_latest_sql = (
                f"SELECT MAX(id) AS max_id FROM events WHERE {' AND '.join(clauses)}"
            )
            task_latest_row = connection.execute(task_latest_sql, parameters).fetchone()
            task_latest = int(task_latest_row["max_id"] or 0)
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
                "has_more_after": bool(items) and next_after < task_latest,
                "has_more_before": bool(items) and next_before > 1 and total > len(items),
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
        event_ids = [int(event["id"]) for event in events]
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
        acknowledgements_by_event: dict[int, list[dict[str, Any]]] = {
            event_id: [] for event_id in event_ids
        }
        if event_ids:
            placeholders = _sql_placeholders(len(event_ids))
            for row in connection.execute(
                f"""
                SELECT event_id, session_id, created_at
                FROM event_acknowledgements
                WHERE event_id IN ({placeholders})
                ORDER BY created_at
                """,
                event_ids,
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
                acknowledgements_by_event[int(row["event_id"])].append(
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
            session = sessions.get(str(event.get("actor_session_id") or ""))
            member = members.get(str((session or {}).get("member_id") or ""))
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
                    actor=actor_snapshot(session, member),
                    related=related,
                    acknowledgements=acknowledgements_by_event.get(int(event["id"]), []),
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
    ) -> dict[str, Any]:
        if bool(session_id) != bool(token):
            raise DomainError(
                "invalid_session_token",
                "session_id and token must be supplied together",
                status_code=401,
            )
        if session_id:
            self.heartbeat(project_id, session_id, token or "")
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            if session_id:
                self._authenticate(connection, project_id, session_id, token or "")
            event_result = self._list_events(
                connection, project_id, after=after
            )
            snapshot = self._snapshot(connection, project_id)
            unread_count = None
            if session_id:
                read_cursor = min(event_result["cursor"], event_result["latest_cursor"])
                unread_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS unread_count
                        FROM events WHERE project_id = ? AND id > ?
                        """,
                        (project_id, read_cursor),
                    ).fetchone()["unread_count"]
                )
        if session_id:
            read_cursor = min(event_result["cursor"], event_result["latest_cursor"])
            with self.database.connect(write=True) as connection:
                self._authenticate(connection, project_id, session_id, token or "")
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
        clean_files = sorted({normalize_pattern(path) for path in (files or [])})
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
                    "mentions": list(dict.fromkeys(mentions or [])),
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
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            event = connection.execute(
                "SELECT * FROM events WHERE id = ? AND project_id = ?",
                (event_id, project_id),
            ).fetchone()
            if event is None:
                raise DomainError("event_not_found", "Event does not exist", status_code=404)
            payload = json_load(event["payload_json"], {})
            if not payload.get("requires_ack"):
                raise DomainError(
                    "acknowledgement_not_required",
                    "This event does not require acknowledgement",
                    status_code=409,
                )
            connection.execute(
                """
                INSERT INTO event_acknowledgements(event_id, session_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event_id, session_id) DO NOTHING
                """,
                (event_id, session_id, iso_now()),
            )
            cursor = self._emit(
                connection,
                project_id,
                "message.acknowledged",
                actor_session_id=session_id,
                task_id=event["task_id"],
                payload={"event_id": event_id},
            )
            return {
                "acknowledged": True,
                "acknowledged_event_id": event_id,
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
            connection.execute(
                """
                UPDATE task_intakes
                SET target_member_id = ?, target_session_id = ?, status = 'pending',
                    note = ?, updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (
                    member["id"],
                    clean_session_id,
                    note.strip(),
                    now,
                    intake_id,
                    project_id,
                ),
            )
            event_id = self._emit(
                connection,
                project_id,
                "task.intake_reassigned",
                payload={
                    "intake_id": intake_id,
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
            now = iso_now()
            connection.execute(
                """
                UPDATE task_intakes
                SET target_session_id = ?, status = ?, note = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'pending'
                """,
                (session_id, next_status, note.strip(), now, intake_id, project_id),
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
        priority: int = 2,
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
            connection.execute(
                """
                UPDATE task_intakes
                SET status = 'defined', formal_task_id = ?, note = ?, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = 'accepted'
                """,
                (task["id"], note.strip(), now, intake_id, project_id),
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
        priority: int = 2,
        actor_session_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise DomainError("invalid_task", "Task title is required")
        if not 0 <= priority <= 4:
            raise DomainError("invalid_priority", "Priority must be between 0 and 4")
        criteria = [item.strip() for item in (acceptance_criteria or []) if item.strip()]
        if not criteria:
            raise DomainError(
                "missing_acceptance_criteria",
                "At least one acceptance criterion is required",
            )
        dependencies = list(dict.fromkeys(depends_on or []))
        now = iso_now()
        task_id = new_id("task")
        with self.database.connect(write=True) as connection:
            self._require_project(connection, project_id)
            if actor_session_id:
                self._authenticate(connection, project_id, actor_session_id, token or "")
            for dependency_id in dependencies:
                self._require_task(connection, project_id, dependency_id)
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

    def get_task_by_number(self, project_id: str, task_number: int) -> dict[str, Any]:
        """Resolve a human-readable Project task number to the full task."""
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            task = self._require_task_by_number(connection, project_id, task_number)
            return self._task_with_dependencies(connection, task)

    @idempotent_write("task.claim")
    def claim_task(
        self, project_id: str, task_id: str, session_id: str, token: str
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            task = self._require_task(connection, project_id, task_id)
            if task["owner_session_id"] == session_id:
                return {
                    "task": self._task_with_dependencies(connection, task),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            if (
                task["owner_session_id"] is not None
                or task["execution_status"] != "todo"
            ):
                raise DomainError(
                    "task_already_claimed",
                    "Task is not available for claiming",
                    status_code=409,
                    details={
                        "owner_session_id": task["owner_session_id"],
                        "status": task["status"],
                    },
                )
            blockers = connection.execute(
                """
                SELECT t.id, t.title, t.status, t.execution_status,
                       t.verification_status, t.integration_status
                FROM task_dependencies d
                JOIN tasks t ON t.id = d.depends_on_task_id
                WHERE d.task_id = ?
                  AND t.verification_status != 'approved'
                  AND t.integration_status != 'done'
                """,
                (task_id,),
            ).fetchall()
            if blockers:
                raise DomainError(
                    "task_dependencies_incomplete",
                    "Task has unfinished dependencies",
                    status_code=409,
                    details={"blockers": [dict(row) for row in blockers]},
                )
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
                    "verification_status": task["verification_status"],
                    "from_integration_status": task["integration_status"],
                    "integration_status": task["integration_status"],
                },
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return {
                "task": self._task_with_dependencies(connection, row),
                "event_id": event_id,
                "cursor": event_id,
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
    ) -> dict[str, Any]:
        """Release an owned task back to the claimable pool.

        The current owner may release its own task; management may release on
        behalf of an owner that is offline, unreachable, or out of quota. The
        task returns to todo with its contract, progress, and history intact.
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
                    raise DomainError(
                        "not_task_owner",
                        "Only the task owner or management can release this task",
                        status_code=403,
                    )
                initiator = "owner"
                actor_session_id = session_id
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
                # A delayed assignment targets the persistent identity but is
                # stored on its latest session; session replacement on rejoin
                # retargets pending assignments to the new session, so the
                # offline Agent can acknowledge it after reconnecting.
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
                return {
                    "assignment": dict(existing),
                    "task": self._task_with_dependencies(connection, task),
                    "event_id": None,
                    "cursor": self.latest_cursor(connection, project_id),
                }
            assignment_id = new_id("assignment")
            now = iso_now()
            connection.execute(
                """
                INSERT INTO task_assignments(
                    id, project_id, task_id, assigned_by_session_id,
                    assigned_to_session_id, responded_by_session_id,
                    target_role, required_capability, status, note,
                    response_note, created_at, responded_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'pending', ?, '', ?, NULL)
                """,
                (
                    assignment_id,
                    project_id,
                    task_id,
                    assigned_by_session_id,
                    assigned_to_session_id,
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
            eligible = (
                assignment["assigned_to_session_id"] in {None, session_id}
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
            if response == "accepted":
                if task["owner_session_id"] not in {None, session_id}:
                    previous_owner = connection.execute(
                        "SELECT left_at FROM agent_sessions WHERE id = ? AND project_id = ?",
                        (task["owner_session_id"], project_id),
                    ).fetchone()
                    can_reclaim = (
                        task["execution_status"] == "in_progress"
                        and task["verification_status"] == "changes_requested"
                        and previous_owner is not None
                        and previous_owner["left_at"] is not None
                    )
                    if not can_reclaim:
                        raise DomainError(
                            "task_already_claimed",
                            "Task is already owned by another Agent",
                            status_code=409,
                        )
                    reclaimed_from_session_id = task["owner_session_id"]
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
                connection.execute(
                    """
                    UPDATE tasks
                    SET owner_session_id = ?, status = 'claimed',
                        execution_status = 'claimed', updated_at = ?
                    WHERE id = ?
                    """,
                    (session_id, iso_now(), task_id),
                )
            now = iso_now()
            connection.execute(
                """
                UPDATE task_assignments
                SET status = ?, responded_by_session_id = ?, response_note = ?,
                    responded_at = ?
                WHERE id = ?
                """,
                (response, session_id, note.strip(), now, assignment_id),
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

        def clean_items(values: list[str] | None) -> list[str]:
            return list(
                dict.fromkeys(
                    str(item).strip() for item in (values or []) if str(item).strip()
                )
            )

        clean_completed = clean_items(completed_items)
        clean_pending = clean_items(pending_items)
        clean_files = sorted({normalize_pattern(path) for path in (files or [])})
        clean_risks = clean_items(risks)
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
            source_session_id = from_session_id or task["owner_session_id"]
            if not source_session_id:
                raise DomainError(
                    "handoff_source_missing",
                    "Task must have an owner before it can be handed off",
                    status_code=409,
                )
            if task["owner_session_id"] != source_session_id:
                raise DomainError(
                    "not_task_owner",
                    "Only the current task owner can hand off this task",
                    status_code=403,
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
                connection.execute(
                    """
                    UPDATE tasks
                    SET owner_session_id = ?, current_step = ?, next_step = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (session_id, handoff["next_step"], now, task_id),
                )
            now = iso_now()
            connection.execute(
                """
                UPDATE task_handoffs
                SET status = ?, responded_by_session_id = ?, response_note = ?,
                    responded_at = ?
                WHERE id = ?
                """,
                (response, session_id, note.strip(), now, handoff_id),
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
    ) -> dict[str, Any]:
        with self.database.connect(write=True) as connection:
            if session_id:
                self._authenticate(connection, project_id, session_id, token or "")
            task = self._require_task(connection, project_id, task_id)
            next_status = status or task["status"]
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
                release_result = self._release_task_locked(
                    connection,
                    project_id,
                    task,
                    reason_code=TASK_RELEASE_COMPAT_REASON_CODE,
                    reason="released through task_update(status=todo); "
                    "prefer task_release for an explicit reason code",
                    initiator="owner" if session_id else "management",
                    actor_session_id=session_id,
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
            )
            if session_id and task["owner_session_id"] not in {None, session_id}:
                raise DomainError("not_task_owner", "Only the task owner can update this task", status_code=403)
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
                self._require_task(connection, project_id, dependency_id)
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
            connection.execute(
                """
                UPDATE tasks SET status = ?, execution_status = ?,
                                 verification_status = ?, integration_status = ?,
                                 title = ?, description = ?,
                                 acceptance_criteria_json = ?, priority = ?,
                                 owner_session_id = ?, progress_percent = ?,
                                 current_step = ?, blocker_reason = ?, next_step = ?,
                                 updated_at = ?
                WHERE id = ?
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
                ),
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
            if next_status == "blocked" and task["status"] != "blocked":
                event_type = "task.blocked"
            elif next_status == "todo" and task["owner_session_id"]:
                event_type = "task.released"
            elif task["status"] == "blocked" and next_status != "blocked":
                event_type = "task.unblocked"
            elif next_status == "cancelled" and task["status"] != "cancelled":
                event_type = "task.cancelled"
            else:
                event_type = "task.updated"
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
        ttl = ttl_seconds or self.settings.default_lease_ttl_seconds
        if not 1 <= ttl <= self.settings.max_lease_ttl_seconds:
            raise DomainError(
                "invalid_lease_ttl",
                f"Lease TTL must be between 1 and {self.settings.max_lease_ttl_seconds}",
            )
        now = utc_now()
        expires = now + timedelta(seconds=ttl)
        online_cutoff = (
            now - timedelta(seconds=self.settings.heartbeat_timeout_seconds)
        ).isoformat().replace("+00:00", "Z")
        with self.database.connect(write=True) as connection:
            self._authenticate(connection, project_id, session_id, token)
            if task_id:
                task = self._require_task(connection, project_id, task_id)
                if task["owner_session_id"] not in {None, session_id}:
                    raise DomainError("not_task_owner", "Lease task belongs to another agent", status_code=403)
            active = connection.execute(
                """
                SELECT l.*, a.name AS agent_name FROM file_leases l
                JOIN agent_sessions a ON a.id = l.session_id
                WHERE l.project_id = ? AND l.released_at IS NULL AND l.expires_at > ?
                  AND a.last_heartbeat >= ?
                """,
                (project_id, now.isoformat().replace("+00:00", "Z"), online_cutoff),
            ).fetchall()
            conflicts = [
                {
                    "lease_id": row["id"],
                    "session_id": row["session_id"],
                    "agent_name": row["agent_name"],
                    "path_pattern": row["path_pattern"],
                    "mode": row["mode"],
                    "expires_at": row["expires_at"],
                }
                for row in active
                if row["session_id"] != session_id
                and patterns_overlap(pattern, row["path_pattern"])
                and lease_modes_conflict(mode, row["mode"])
            ]
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
        clean_paths = sorted({normalize_pattern(path) for path in paths})
        if not clean_paths:
            raise DomainError("missing_paths", "At least one file path is required")
        with self.database.connect(write=True) as connection:
            project = self._require_project(connection, project_id)
            if session_id:
                self._authenticate(connection, project_id, session_id, token or "")
            now = iso_now()
            online_cutoff = (
                utc_now() - timedelta(seconds=self.settings.heartbeat_timeout_seconds)
            ).isoformat().replace("+00:00", "Z")
            active = connection.execute(
                """
                SELECT l.*, a.name AS agent_name FROM file_leases l
                JOIN agent_sessions a ON a.id = l.session_id
                WHERE l.project_id = ? AND l.released_at IS NULL
                  AND l.expires_at > ? AND l.mode = 'exclusive'
                  AND a.last_heartbeat >= ?
                """,
                (project_id, now, online_cutoff),
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
        clean_files = sorted({normalize_pattern(path) for path in files})
        clean_no_code_reason = no_code_change_reason.strip()
        if not summary.strip() or not tests or (not clean_files and not clean_no_code_reason):
            raise DomainError(
                "insufficient_work_evidence",
                "Work report requires summary, test evidence, and either changed files or a no-code-change reason",
            )
        invalid_tests = [
            test
            for test in tests
            if not str(test.get("command", "")).strip()
            or not isinstance(test.get("exit_code"), int)
        ]
        if invalid_tests:
            raise DomainError(
                "invalid_test_evidence",
                "Every test entry requires a command and integer exit_code",
            )
        with self.database.connect() as connection:
            agent = self._authenticate(connection, project_id, session_id, token)
            project = self._require_project(connection, project_id)
            worktree = self._trusted_worktree(connection, project, agent)
        system_evidence = self._collect_git_evidence(
            project, agent, commit_hash, worktree=worktree
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
                    "work_report.commit_unverified",
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
            connection.execute(
                """
                UPDATE tasks
                SET status = 'awaiting_review', execution_status = 'completed',
                    verification_status = 'pending', integration_status = 'pending',
                    progress_percent = 100, blocker_reason = '', updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
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
            assessed = {
                str(item.get("criterion", "")).strip(): str(item.get("status", "")).strip()
                for item in criteria
            }
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
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, execution_status = ?, verification_status = ?,
                    integration_status = 'pending', progress_percent = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    next_execution_status,
                    next_verification_status,
                    next_progress,
                    now,
                    task_id,
                ),
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
        invalid_tests = [
            test
            for test in tests
            if not str(test.get("command", "")).strip()
            or not isinstance(test.get("exit_code"), int)
        ]
        if invalid_tests:
            raise DomainError(
                "invalid_test_evidence",
                "Every integration test entry requires a command and integer exit_code",
            )
        if result == "done" and any(test["exit_code"] != 0 for test in tests):
            raise DomainError(
                "integration_tests_failed",
                "A successful integration requires every submitted test to pass",
                status_code=409,
            )
        clean_files = sorted({normalize_pattern(path) for path in (files or [])})
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
                    commit_hash.strip(),
                    json_dump(tests),
                    now,
                ),
            )
            next_status = "done" if result == "done" else "verified"
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, integration_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, result, now, task_id),
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
                    "commit_hash": commit_hash.strip(),
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
                "SELECT id FROM events WHERE id = ? AND project_id = ?",
                (event_id, project_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "knowledge_source_not_found",
                    "source_event_ids must reference events in this project",
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
        clean_tags = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
        clean_event_ids = sorted({int(event_id) for event_id in (source_event_ids or [])})
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
                    json_dump(clean_event_ids),
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
    ) -> dict[str, Any]:
        if verdict not in KNOWLEDGE_REVIEW_VERDICTS:
            raise DomainError(
                "invalid_review_verdict", "Unsupported review verdict"
            )
        if not criteria:
            raise DomainError(
                "missing_review_criteria", "Review must assess acceptance criteria"
            )
        assessed = {
            str(item.get("criterion", "")).strip(): str(item.get("status", "")).strip()
            for item in criteria
        }
        if not all(assessed) or any(
            status not in {"passed", "failed"} for status in assessed.values()
        ):
            raise DomainError(
                "invalid_review_criteria",
                "Every review criterion requires a criterion and passed/failed status",
            )
        if verdict == "approved" and any(
            status != "passed" for status in assessed.values()
        ):
            raise DomainError(
                "acceptance_criteria_not_satisfied",
                "Approved reviews must mark every criterion as passed",
                status_code=409,
                details={"assessed": assessed},
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
            connection.execute(
                """
                UPDATE knowledge_assets
                SET status = ?, updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (next_status, now, asset_id, project_id),
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
            connection.execute(
                """
                UPDATE knowledge_assets
                SET status = 'superseded', updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (now, asset_id, project_id),
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
            connection.execute(
                """
                UPDATE knowledge_assets
                SET status = 'archived', updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (now, asset_id, project_id),
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
        reported = (
            Path(reported_raw).expanduser().resolve(strict=False) if reported_raw else root
        )
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

    def _collect_git_evidence(
        self,
        project: Mapping[str, Any],
        agent: Mapping[str, Any],
        commit_hash: str,
        *,
        worktree: Path | None = None,
    ) -> dict[str, Any]:
        resolved = worktree or Path(agent["worktree"] or project["root_path"])
        return self._collect_git_evidence_from_worktree(resolved, commit_hash)

    def _collect_git_evidence_from_worktree(
        self,
        worktree: Path,
        commit_hash: str,
    ) -> dict[str, Any]:
        def git(*arguments: str) -> tuple[int, str]:
            try:
                completed = subprocess.run(
                    ["git", "-C", str(worktree), *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                return completed.returncode, completed.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return 1, ""

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
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS cursor FROM events WHERE project_id = ?",
                (project_id,),
            ).fetchone()["cursor"]
        )

    def snapshot(self, project_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._snapshot(connection, project_id)

    def _snapshot(self, connection: Any, project_id: str) -> dict[str, Any]:
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
                      AND e.id > a.last_read_cursor
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
            reports = [
                {
                    **dict(row),
                    "files": json_load(row["files_json"], []),
                    "tests": json_load(row["tests_json"], []),
                    "system_evidence": json_load(row["system_evidence_json"], {}),
                }
                for row in connection.execute(
                    "SELECT * FROM work_reports WHERE project_id = ? ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            ]
            for report in reports:
                report.pop("files_json", None)
                report.pop("tests_json", None)
                report.pop("system_evidence_json", None)
            reviews = [
                {**dict(row), "criteria": json_load(row["criteria_json"], [])}
                for row in connection.execute(
                    "SELECT * FROM reviews WHERE project_id = ? ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            ]
            for review in reviews:
                review.pop("criteria_json", None)
            acknowledgements = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT a.event_id, a.session_id, a.created_at
                    FROM event_acknowledgements a
                    JOIN events e ON e.id = a.event_id
                    WHERE e.project_id = ?
                    ORDER BY a.created_at DESC
                    """,
                    (project_id,),
                ).fetchall()
            ]
            cursor = self.latest_cursor(connection, project_id)
            return {
                "project": project,
                "members": members,
                "agents": agents,
                "agent_identities": self._agent_identities(agents, tasks, members),
                "tasks": tasks,
                "leases": leases,
                "reports": reports,
                "reviews": reviews,
                "acknowledgements": acknowledgements,
                "cursor": cursor,
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
            current = [event for event in batch["events"] if event["id"] <= cursor]
            if not current:
                break
            events.extend(current)
            after = current[-1]["id"]
        return {
            "export_schema_version": 1,
            "exported_at": iso_now(),
            "project": snapshot["project"],
            "members": snapshot["members"],
            "cursor": cursor,
            "agents": snapshot["agents"],
            "tasks": snapshot["tasks"],
            "leases": self.list_leases(project_id, include_inactive=True),
            "reports": snapshot["reports"],
            "reviews": snapshot["reviews"],
            "acknowledgements": snapshot["acknowledgements"],
            "knowledge_assets": self._export_knowledge_assets(project_id),
            "events": events,
        }
