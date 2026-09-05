from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlparse

from .errors import DomainError
from .project_registration import (
    PROJECT_REGISTRATION_RELATIVE_PATH,
    resolve_checkout_project_key,
)
from .services import AgentChatRoomService


BOOTSTRAP_SCHEMA_VERSION = 1
SOFTWARE_KEY_ENV = "AGENTCHATROOM_SOFTWARE_KEY"
SOFTWARE_NAME_ENV = "AGENTCHATROOM_SOFTWARE_NAME"
SOFTWARE_CLIENT_ENV = "AGENTCHATROOM_SOFTWARE_CLIENT"
PROJECT_PATH_ENV = "AGENTCHATROOM_PROJECT_PATH"

BOOTSTRAP_STATES = (
    "ready",
    "identity_not_configured",
    "mcp_restart_required",
    "project_not_registered",
    "registration_invalid",
    "ambiguous_workspace",
    "room_unavailable",
    "session_expired",
)

REQUIRED_ACTIONS = {
    "identity_not_configured": "open_local_mcp_config_assistant",
    "mcp_restart_required": "restart_mcp_client_session",
    "project_not_registered": "create_or_open_project_in_web",
    "registration_invalid": "recreate_checkout_registration_via_web",
    "ambiguous_workspace": "open_one_workspace_folder",
    "room_unavailable": "restore_or_wait_for_room",
    "session_expired": "call_room_bootstrap",
}

_TOKEN_KEYS = {
    "token",
    "agent_token",
    "admin_token",
    "session_token",
    "access_token",
    "secret",
    "password",
}


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    project_id: str
    session_id: str
    token: str
    cursor: int
    software_key: str
    agent_key: str
    conversation_synced: bool


@dataclass(frozen=True, slots=True)
class BootstrapOutcome:
    public: dict[str, Any]
    binding: RuntimeBinding | None = None


def configured_software_identity() -> tuple[str, str, str] | None:
    software_key = os.getenv(SOFTWARE_KEY_ENV, "").strip()
    software_name = os.getenv(SOFTWARE_NAME_ENV, "").strip()
    client = os.getenv(SOFTWARE_CLIENT_ENV, "").strip()
    values = (software_key, software_name, client)
    if not all(values) or any(value.startswith("<") for value in values):
        return None
    return software_key, software_name, client


def bootstrap_status_payload(
    status: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in BOOTSTRAP_STATES:
        raise ValueError(f"Unsupported bootstrap status: {status}")
    payload = {
        "schema_version": BOOTSTRAP_SCHEMA_VERSION,
        "status": status,
        "conversation_synced": False,
        "connection": {
            "software_configured": status not in {"identity_not_configured"},
            "process_connected": status not in {
                "identity_not_configured",
                "mcp_restart_required",
            },
            "room_session": "absent",
            "conversation_synced": False,
        },
    }
    if status != "ready":
        payload["required_action"] = REQUIRED_ACTIONS[status]
    if details:
        payload["details"] = redact_runtime_value(details)
    return payload


def workspace_path_from_file_uri(uri: str) -> Path | None:
    raw = str(uri or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme and scheme != "file":
        return None
    path = unquote(parsed.path or "")
    netloc = unquote(parsed.netloc or "")
    if netloc and netloc.lower() not in {"", "localhost", "127.0.0.1"}:
        if os.name != "nt":
            return None
        path = f"//{netloc}{path}"
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return None


def find_registered_checkout(start: str | Path) -> Path | None:
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent
    if not current.exists():
        return None
    for candidate in (current, *current.parents):
        if (candidate / PROJECT_REGISTRATION_RELATIVE_PATH).is_file():
            return candidate
    return None


def discover_workspace_candidates(
    *,
    workspace_roots: Iterable[str | Path] | None = None,
    cwd: str | Path | None = None,
    explicit_project_path: str | Path | None = None,
) -> list[Path]:
    if explicit_project_path and str(explicit_project_path).strip():
        found = find_registered_checkout(explicit_project_path)
        return [found] if found is not None else []

    ordered: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        key = os.path.normcase(str(path.resolve()))
        if key in seen:
            return
        seen.add(key)
        ordered.append(path.resolve())

    for root in workspace_roots or ():
        add(find_registered_checkout(root))
    if cwd is not None and str(cwd).strip():
        add(find_registered_checkout(cwd))
    return ordered


def redact_runtime_value(value: Any, *, key: str = "") -> Any:
    # Imported lazily: task_history is a leaf module (typing-only imports),
    # while bootstrap sits above services, so a module-level import would be
    # circular. Kept here so both redaction entry points share one policy.
    from .task_history import redact_text

    if key.lower() in _TOKEN_KEYS:
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_runtime_value(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).lower() not in _TOKEN_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [redact_runtime_value(item) for item in value]
    if isinstance(value, str) and len(value) >= 24 and "token" in key.lower():
        return "[redacted]"
    if isinstance(value, str):
        return redact_text(value)
    return value


def contains_secret(value: Any, secret: str) -> bool:
    if not secret:
        return False
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    return secret in encoded


def compact_room_snapshot(snapshot: Mapping[str, Any], *, cursor: int) -> dict[str, Any]:
    project = snapshot.get("project") or {}
    identities = snapshot.get("agent_identities") or []
    tasks = snapshot.get("tasks") or []
    return redact_runtime_value(
        {
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "root_path": project.get("root_path"),
            },
            "cursor": cursor,
            "connected_agent_count": sum(
                1
                for identity in identities
                if identity.get("connection_status") == "connected"
            ),
            "agent_count": len(identities),
            "documents": [
                {
                    "doc_key": doc.get("doc_key"),
                    "kind": doc.get("kind"),
                    "title": doc.get("title"),
                    "version": doc.get("version"),
                    "size": doc.get("size"),
                    "archived_at": doc.get("archived_at"),
                }
                for doc in (snapshot.get("documents") or [])
            ],
            "open_task_count": sum(
                1
                for task in tasks
                if task.get("execution_status") not in {"completed", "cancelled"}
                or (
                    task.get("execution_status") == "completed"
                    and task.get("integration_status") != "done"
                )
            ),
        }
    )


def bootstrap_local_room(
    service: AgentChatRoomService,
    *,
    software_key: str,
    software_name: str,
    client: str,
    model: str = "unknown",
    role: str = "executor",
    workspace_roots: Iterable[str | Path] | None = None,
    cwd: str | Path | None = None,
    explicit_project_path: str | Path | None = None,
    loaded_identity: tuple[str, str, str] | None = None,
) -> BootstrapOutcome:
    current_identity = (software_key, software_name, client)
    if loaded_identity is not None and loaded_identity != current_identity:
        return BootstrapOutcome(bootstrap_status_payload("mcp_restart_required"))

    candidates = discover_workspace_candidates(
        workspace_roots=workspace_roots,
        cwd=cwd,
        explicit_project_path=explicit_project_path,
    )
    if not candidates:
        return BootstrapOutcome(bootstrap_status_payload("project_not_registered"))
    unique_keys: dict[str, Path] = {}
    invalid: Path | None = None
    for candidate in candidates:
        try:
            project_key, present = resolve_checkout_project_key(candidate)
        except DomainError as error:
            if error.code.startswith("project_registration"):
                invalid = candidate
                continue
            return BootstrapOutcome(
                bootstrap_status_payload(
                    "registration_invalid",
                    details={"code": error.code},
                )
            )
        if not present or not project_key:
            continue
        unique_keys.setdefault(project_key, candidate)
    if invalid is not None and not unique_keys:
        return BootstrapOutcome(bootstrap_status_payload("registration_invalid"))
    if len(unique_keys) > 1:
        return BootstrapOutcome(
            bootstrap_status_payload(
                "ambiguous_workspace",
                details={"candidate_count": len(unique_keys)},
            )
        )
    if not unique_keys:
        return BootstrapOutcome(bootstrap_status_payload("project_not_registered"))

    project_key, checkout = next(iter(unique_keys.items()))
    try:
        project = service.resolve_project_for_join(
            root_path=str(checkout),
            registered_project_key=project_key,
        )
    except DomainError as error:
        if error.code in {"project_archived", "project_not_found"}:
            return BootstrapOutcome(
                bootstrap_status_payload(
                    "room_unavailable",
                    details={"code": error.code},
                )
            )
        if error.code.startswith("project_registration"):
            return BootstrapOutcome(bootstrap_status_payload("registration_invalid"))
        return BootstrapOutcome(
            bootstrap_status_payload(
                "room_unavailable",
                details={"code": error.code},
            )
        )

    workspace_path = str(checkout)
    try:
        registered = service.register_workspace(
            project["id"],
            host_key=f"host:{software_key}",
            host_name=software_name,
            local_path=workspace_path,
            worktree=workspace_path,
        )
        joined = service.join_room(
            project["id"],
            software_key=software_key,
            name=software_name,
            client=client,
            model=model.strip() or "unknown",
            role=role,
            worktree=workspace_path,
            capabilities={"mcp": True},
            host_id=registered["host"]["id"],
            workspace_id=registered["workspace"]["id"],
        )
        synced = service.room_sync(
            project["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            after=int(joined.get("cursor") or 0),
        )
    except DomainError as error:
        if error.code in {"session_token_expired", "session_closed", "invalid_session_token"}:
            return BootstrapOutcome(bootstrap_status_payload("session_expired"))
        return BootstrapOutcome(
            bootstrap_status_payload(
                "room_unavailable",
                details={"code": error.code},
            )
        )

    token = str(joined["token"])
    cursor = int(synced.get("cursor") or joined.get("cursor") or 0)
    public = bootstrap_status_payload("ready")
    public["conversation_synced"] = True
    public["connection"] = {
        "software_configured": True,
        "process_connected": True,
        "room_session": "replaced" if joined.get("replaced", {}).get("previous_session_ids") else "restored",
        "conversation_synced": True,
    }
    public["project"] = compact_room_snapshot(synced.get("snapshot") or {}, cursor=cursor)["project"]
    public["session"] = {
        "id": joined["agent"]["id"],
        "role": joined["agent"].get("role"),
        "model": joined["agent"].get("model"),
    }
    public["identity"] = {
        "name": software_name,
        "software_key": software_key,
        "client": client,
    }
    public["cursor"] = cursor
    public["snapshot"] = compact_room_snapshot(synced.get("snapshot") or {}, cursor=cursor)
    public["unread_count"] = synced.get("unread_count")
    public = redact_runtime_value(public)
    if contains_secret(public, token):
        raise DomainError(
            "runtime_secret_leaked",
            "Bootstrap result must not include a live Session Token",
            status_code=500,
        )
    return BootstrapOutcome(
        public,
        RuntimeBinding(
            project_id=str(project["id"]),
            session_id=str(joined["agent"]["id"]),
            token=token,
            cursor=cursor,
            software_key=software_key,
            agent_key=str(joined["agent"].get("agent_key") or joined["agent"]["id"]),
            conversation_synced=True,
        ),
    )


def bind_runtime_arguments(
    parameters: Iterable[str],
    arguments: Mapping[str, Any],
    binding: RuntimeBinding | None,
) -> dict[str, Any]:
    forwarded = dict(arguments)
    names = set(parameters)
    session_aliases = {
        "session_id",
        "assigned_by_session_id",
        "from_session_id",
        "reviewer_session_id",
        "integrator_session_id",
        "created_by_session_id",
    } & names

    def empty(key: str) -> bool:
        value = forwarded.get(key, "")
        return value is None or str(value).strip() == ""

    needs_session = bool(session_aliases) or "token" in names
    if binding is None:
        missing = False
        if needs_session:
            if "token" in names and empty("token"):
                missing = True
            if session_aliases and all(empty(alias) for alias in session_aliases):
                missing = True
        if "project_id" in names and empty("project_id"):
            missing = True
        if missing:
            raise DomainError(
                "session_expired",
                "Call room_bootstrap to restore the current Room session",
                status_code=401,
                details={"required_action": REQUIRED_ACTIONS["session_expired"]},
            )
        return forwarded

    if "project_id" in names:
        supplied_project = str(forwarded.get("project_id") or "").strip()
        if supplied_project and supplied_project != binding.project_id:
            raise DomainError(
                "runtime_context_mismatch",
                "Explicit project_id does not match the current Room binding",
                status_code=409,
            )
        if empty("project_id"):
            forwarded["project_id"] = binding.project_id
    if "token" in names:
        supplied_token = str(forwarded.get("token") or "").strip()
        if supplied_token and supplied_token != binding.token:
            raise DomainError(
                "runtime_context_mismatch",
                "Explicit session token does not match the current Room binding",
                status_code=409,
            )
        if empty("token"):
            forwarded["token"] = binding.token
    for alias in session_aliases:
        supplied_session = str(forwarded.get(alias) or "").strip()
        if supplied_session and supplied_session != binding.session_id:
            raise DomainError(
                "runtime_context_mismatch",
                "Explicit session_id does not match the current Room binding",
                status_code=409,
            )
        if empty(alias):
            forwarded[alias] = binding.session_id
    return forwarded
