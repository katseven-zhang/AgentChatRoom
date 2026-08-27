from __future__ import annotations

import fnmatch
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import subprocess
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
    LEGACY_TASK_STATUSES,
    LEGACY_TASK_TRANSITIONS,
    MODEL_DISPLAY_NAME_MAX_LENGTH,
    PROJECT_MEMBER_SCHEMA_VERSION,
    PROJECT_MEMBER_STATUSES,
    TASK_EXECUTION_STATUSES,
    TASK_INTEGRATION_STATUSES,
    TASK_VERIFICATION_STATUSES,
    task_contract,
    task_state_for_legacy_status,
)
from .database import DatabaseBackend
from .errors import DomainError


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
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SENSITIVE_REQUEST_FIELDS = {
    "token",
    "agent_token",
    "admin_token",
    "password",
    "secret",
}


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


class AgentChatRoomService:
    def __init__(self, database: DatabaseBackend, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def initialize(self) -> None:
        self.database.initialize()

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
                json_dump(payload or {}),
                iso_now(),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Event insert did not return an id")
        return int(row["id"])

    def _event_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json_load(data.pop("payload_json"), {})
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

    @staticmethod
    def _agent_workspace_key(agent: Mapping[str, Any]) -> str:
        return (
            str(agent.get("workspace_id") or "").strip()
            or str(agent.get("worktree") or "").strip().replace("\\", "/").rstrip("/")
            or str(agent.get("host_id") or "").strip()
        ).casefold()

    @classmethod
    def _legacy_agent_key(cls, agent: Mapping[str, Any]) -> str:
        """Project one stable identity for Sessions created before agent_key existed."""
        fingerprint = json_dump(
            {
                "client": str(agent.get("client") or "").strip().casefold(),
                "name": str(agent.get("name") or "").strip().casefold(),
                "role": str(agent.get("role") or "").strip().casefold(),
                "workspace": cls._agent_workspace_key(agent),
            }
        )
        return f"legacy-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"

    @classmethod
    def _legacy_agent_scope_key(cls, agent: Mapping[str, Any]) -> str:
        """Return the broadest safe scope for mapping legacy Sessions to one key."""
        fingerprint = json_dump(
            {
                "client": str(agent.get("client") or "").strip().casefold(),
                "workspace": cls._agent_workspace_key(agent),
                "member_id": str(agent.get("member_id") or "").strip(),
            }
        )
        return hashlib.sha256(fingerprint.encode()).hexdigest()

    def _agent_identities(
        self, agents: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        sessions_list = list(agents)
        explicit_keys_by_legacy_fingerprint: dict[str, set[str]] = {}
        explicit_keys_by_legacy_scope: dict[str, set[str]] = {}
        for agent in sessions_list:
            explicit_key = str(agent.get("agent_key") or "").strip()
            if explicit_key:
                fingerprint = self._legacy_agent_key(agent)
                explicit_keys_by_legacy_fingerprint.setdefault(
                    fingerprint, set()
                ).add(explicit_key)
                if self._agent_workspace_key(agent):
                    scope = self._legacy_agent_scope_key(agent)
                    explicit_keys_by_legacy_scope.setdefault(scope, set()).add(
                        explicit_key
                    )

        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for agent in sessions_list:
            explicit_key = str(agent.get("agent_key") or "").strip()
            legacy_key = self._legacy_agent_key(agent)
            exact_candidates = explicit_keys_by_legacy_fingerprint.get(
                legacy_key, set()
            )
            workspace_key = self._agent_workspace_key(agent)
            scope_candidates = (
                explicit_keys_by_legacy_scope.get(
                    self._legacy_agent_scope_key(agent), set()
                )
                if workspace_key
                else set()
            )
            inferred_key = (
                next(iter(exact_candidates))
                if len(exact_candidates) == 1
                else next(iter(scope_candidates))
                if len(scope_candidates) == 1
                else legacy_key
            )
            identity_key = explicit_key or inferred_key
            grouped.setdefault(identity_key, []).append(agent)

        identities: list[dict[str, Any]] = []
        for agent_key, sessions in grouped.items():
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
            activity_status = (
                max(
                    (str(session["status"]) for session in connected),
                    key=lambda status: AGENT_ACTIVITY_PRIORITY.get(status, -1),
                )
                if connected
                else None
            )
            last_activity_values = [
                str(session["last_activity_at"])
                for session in sessions
                if session.get("last_activity_at")
            ]
            identities.append(
                {
                    "schema_version": DOMAIN_SCHEMA_VERSION,
                    "id": agent_key,
                    "agent_key": agent_key,
                    "legacy_identity": not any(
                        str(session.get("agent_key") or "").strip()
                        for session in sessions
                    ),
                    "legacy_session_count": sum(
                        not bool(str(session.get("agent_key") or "").strip())
                        for session in sessions
                    ),
                    "name": representative["name"],
                    "client": representative["client"],
                    "role": representative["role"],
                    "connection_status": "connected" if connected else "disconnected",
                    "activity_status": activity_status,
                    "status": activity_status or "registered",
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
                str(identity["agent_key"]),
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

    def _host_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        return data

    def _workspace_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json_load(data.pop("metadata_json"), {})
        return data

    def _task_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
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

    def _task_with_dependencies(
        self, connection: Any, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        data = self._task_dict(row)
        data["depends_on"] = [
            dependency["depends_on_task_id"]
            for dependency in connection.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY created_at",
                (row["id"],),
            ).fetchall()
        ]
        data["assignments"] = [
            dict(assignment)
            for assignment in connection.execute(
                """
                SELECT * FROM task_assignments
                WHERE task_id = ? ORDER BY created_at
                """,
                (row["id"],),
            ).fetchall()
        ]
        data["handoffs"] = [
            self._handoff_dict(handoff)
            for handoff in connection.execute(
                """
                SELECT * FROM task_handoffs
                WHERE task_id = ? ORDER BY created_at
                """,
                (row["id"],),
            ).fetchall()
        ]
        data["integrations"] = [
            self._integration_dict(integration)
            for integration in connection.execute(
                """
                SELECT * FROM task_integrations
                WHERE task_id = ? ORDER BY created_at
                """,
                (row["id"],),
            ).fetchall()
        ]
        return data

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
        project_key: str | None = None,
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
        logical = logical_path.strip().replace("\\", "/").strip("/")
        identity = project_key or (
            f"git:{normalize_remote(remote)}:{logical}"
            if remote
            else f"path:{os.path.normcase(str(git_root))}:{logical}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        now = iso_now()
        normalized_settings = normalize_project_settings(settings)
        with self.database.connect(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM projects WHERE project_key = ?", (identity,)
            ).fetchone()
            if existing:
                if existing["archived_at"] is not None:
                    connection.execute(
                        "UPDATE projects SET archived_at = NULL, updated_at = ? WHERE id = ?",
                        (now, existing["id"]),
                    )
                    self._emit(
                        connection,
                        existing["id"],
                        "project.restored",
                        payload={"root_path": str(root)},
                    )
                    existing = connection.execute(
                        "SELECT * FROM projects WHERE id = ?", (existing["id"],)
                    ).fetchone()
                return self._project_dict(existing)
            project_id = f"project_{digest}"
            connection.execute(
                """
                INSERT INTO projects(
                    id, project_key, name, root_path, git_remote, logical_path,
                    settings_json, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    project_id,
                    identity,
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
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return self._project_dict(row)

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
            where = "project_id = ?"
            parameters: list[Any] = [project_id]
            if not include_revoked:
                where += " AND status <> 'revoked'"
            rows = connection.execute(
                f"SELECT * FROM project_members WHERE {where} ORDER BY created_at, member_key",
                parameters,
            ).fetchall()
            members: list[dict[str, Any]] = []
            for row in rows:
                member = self._member_dict(row)
                member["credential_count"] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM agent_credentials
                        WHERE project_id = ? AND member_id = ?
                        """,
                        (project_id, row["id"]),
                    ).fetchone()["count"]
                )
                member["session_count"] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM agent_sessions
                        WHERE project_id = ? AND member_id = ?
                        """,
                        (project_id, row["id"]),
                    ).fetchone()["count"]
                )
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
        context = self.database.connect(write=touch)
        with context as connection:
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
            if touch:
                connection.execute(
                    "UPDATE agent_credentials SET last_used_at = ?, updated_at = ? WHERE id = ?",
                    (iso_now(), iso_now(), row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM agent_credentials WHERE id = ?", (row["id"],)
                ).fetchone()
            return self._credential_dict(row)

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
        agent_key: str,
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
        if not agent_key.strip():
            raise DomainError("invalid_agent_key", "Stable Agent key is required")
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
                    agent_key.strip(),
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
            event_id = self._emit(
                connection,
                project_id,
                "agent.joined",
                actor_session_id=session_id,
                payload={
                    "agent_key": agent_key.strip(),
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
        *,
        status: str = "online",
    ) -> dict[str, Any]:
        if status not in AGENT_STATUSES - {"offline"}:
            raise DomainError("invalid_agent_status", "Unsupported agent status")
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
                (status, now, token_expires_at, session_id),
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
        limit = max(1, min(limit, 1000))
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE project_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (project_id, after, limit),
            ).fetchall()
            events = [self._event_dict(row) for row in rows]
            cursor = events[-1]["id"] if events else after
            latest = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS cursor FROM events WHERE project_id = ?",
                (project_id,),
            ).fetchone()["cursor"]
            return {"events": events, "cursor": cursor, "latest_cursor": latest}

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
            events = [self._event_dict(row) for row in rows]
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
            with self.database.connect() as connection:
                agent = self._authenticate(connection, project_id, session_id, token or "")
            self.heartbeat(
                project_id,
                session_id,
                token or "",
                status=agent["status"] if agent["status"] != "offline" else "online",
            )
        event_result = self.list_events(project_id, after=after)
        unread_count = None
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
                unread_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS unread_count FROM events WHERE project_id = ? AND id > ?",
                        (project_id, read_cursor),
                    ).fetchone()["unread_count"]
                )
        return {
            "snapshot": self.snapshot(project_id),
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
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_id, title, description, acceptance_criteria_json,
                    priority, status, execution_status, verification_status,
                    integration_status, created_by_session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'todo', 'todo', 'not_required',
                          'pending', ?, ?, ?)
                """,
                (
                    task_id,
                    project_id,
                    title.strip(),
                    description.strip(),
                    json_dump(criteria),
                    priority,
                    actor_session_id,
                    now,
                    now,
                ),
            )
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

    def list_tasks(self, project_id: str, status: str | None = None) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
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
            return [self._task_with_dependencies(connection, row) for row in rows]

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        """Return one complete task without requiring a large project list."""
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            task = self._require_task(connection, project_id, task_id)
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
                payload={},
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return {
                "task": self._task_with_dependencies(connection, row),
                "event_id": event_id,
                "cursor": event_id,
            }

    @idempotent_write("task.assign")
    def assign_task(
        self,
        project_id: str,
        task_id: str,
        *,
        assigned_by_session_id: str | None = None,
        token: str | None = None,
        assigned_to_session_id: str | None = None,
        target_role: str = "",
        required_capability: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        role = target_role.strip()
        capability = required_capability.strip()
        if not assigned_to_session_id and not role and not capability:
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
            if lease["released_at"] is None:
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
        system_evidence = self._collect_git_evidence(project, agent, commit_hash)
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
                    commit_hash.strip(),
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
            self._authenticate(connection, project_id, reviewer_session_id, token)
            task = self._require_task(connection, project_id, task_id)
            if (
                task["execution_status"] != "completed"
                or task["verification_status"] != "pending"
            ):
                raise DomainError(
                    "invalid_transition", "Task is not awaiting review", status_code=409
                )
            if task["owner_session_id"] == reviewer_session_id:
                raise DomainError(
                    "reviewer_not_independent",
                    "The executing session cannot independently verify its own work",
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

    def _collect_git_evidence(
        self,
        project: Mapping[str, Any],
        agent: Mapping[str, Any],
        commit_hash: str,
    ) -> dict[str, Any]:
        worktree = Path(agent["worktree"] or project["root_path"])

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
            return {"captured": False, "reason": "not_a_git_worktree", "worktree": str(worktree)}
        _, head = git("rev-parse", "HEAD")
        _, branch = git("branch", "--show-current")
        _, status = git("status", "--porcelain")
        commit_exists = None
        commit_files: list[str] = []
        if commit_hash.strip():
            commit_code, _ = git("cat-file", "-e", f"{commit_hash.strip()}^{{commit}}")
            commit_exists = commit_code == 0
            if commit_exists:
                _, changed = git("show", "--format=", "--name-only", commit_hash.strip())
                commit_files = sorted({line for line in changed.splitlines() if line.strip()})
        return {
            "captured": True,
            "worktree": str(worktree.resolve()),
            "head": head,
            "branch": branch,
            "dirty_files": status.splitlines()[:200] if status else [],
            "reported_commit_exists": commit_exists,
            "commit_files": commit_files,
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
            project = self._project_dict(self._require_project(connection, project_id))
            agent_rows = connection.execute(
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
            agents = []
            for row in agent_rows:
                agent = self._agent_dict(row)
                agent["unread_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) AS unread_count FROM events WHERE project_id = ? AND id > ?",
                        (project_id, row["last_read_cursor"]),
                    ).fetchone()["unread_count"]
                )
                agents.append(agent)
            tasks = [
                self._task_with_dependencies(connection, row)
                for row in connection.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority, created_at",
                    (project_id,),
                ).fetchall()
            ]
            members = self.list_project_members(project_id)
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
                "agent_identities": self._agent_identities(agents),
                "tasks": tasks,
                "leases": leases,
                "reports": reports,
                "reviews": reviews,
                "acknowledgements": acknowledgements,
                "cursor": cursor,
            }

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
            "events": events,
        }
