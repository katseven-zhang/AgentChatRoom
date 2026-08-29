from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn

from .api import create_app
from .backup import (
    BackupError,
    backup_postgresql,
    backup_sqlite,
    restore_postgresql,
    restore_sqlite,
)
from .config import load_settings
from .integrations import build_mcp_integration


MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> Any:
    normalized_method = method.upper()
    effective_request_id = request_id
    headers = {"Content-Type": "application/json"}
    if normalized_method in MUTATING_HTTP_METHODS:
        effective_request_id = request_id or f"cli_{uuid.uuid4().hex}"
        headers["Idempotency-Key"] = effective_request_id
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=normalized_method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"error": {"code": "http_error", "message": body}}
        raise SystemExit(json.dumps(detail, ensure_ascii=False, indent=2)) from error
    except urllib.error.URLError as error:
        request_suffix = (
            f" (request_id={effective_request_id})" if effective_request_id else ""
        )
        raise SystemExit(
            f"AgentChatRoom service is unavailable: {error.reason}{request_suffix}"
        ) from error


def print_result(value: Any, compact: bool) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=None if compact else 2))


def service_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}"


def print_server_log(settings, *, lines: int = 80, follow: bool = False) -> None:
    log_path = settings.data_dir / "server.log"
    pid_path = settings.data_dir / "server.pid"
    if not log_path.exists():
        raise SystemExit(f"AgentChatRoom log does not exist yet: {log_path}")
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        content = handle.readlines()
        for line in content[-lines:]:
            print(line, end="")
        if not follow:
            return
        while True:
            line = handle.readline()
            if line:
                print(line, end="", flush=True)
            elif not pid_path.exists():
                return
            else:
                time.sleep(0.2)


def validate_runtime_settings(settings) -> dict[str, Any]:
    """Validate deployment-only prerequisites without opening the database."""
    checks: dict[str, str] = {
        "configuration": "valid",
        "database": settings.database_backend,
        "management_authentication": (
            "required" if settings.management_auth_required else "optional"
        ),
    }
    if settings.database_backend == "postgresql":
        if not os.getenv(settings.database_url_env, "").strip():
            raise SystemExit(
                f"{settings.database_url_env} must be set for PostgreSQL deployment"
            )
        checks["database_url"] = "configured"
    else:
        checks["database_url"] = "not_required_for_sqlite"

    if settings.management_auth_required:
        if len(os.getenv(settings.management_token_env, "")) < 24:
            raise SystemExit(
                f"{settings.management_token_env} must contain at least 24 characters "
                "when management authentication is required"
            )
        checks["management_secret"] = "configured"
    else:
        checks["management_secret"] = "not_required_for_current_profile"
    return {"valid": True, "settings": settings.public_dict(), "checks": checks}


def parse_test_evidence(values: list[str]) -> list[dict[str, Any]]:
    evidence = []
    for value in values:
        command, separator, exit_code = value.rpartition("::")
        if not separator or not command.strip():
            raise SystemExit("Each --test must use COMMAND::EXIT_CODE, for example pytest::0")
        try:
            parsed_exit_code = int(exit_code)
        except ValueError as error:
            raise SystemExit("Test exit code must be an integer") from error
        evidence.append({"command": command.strip(), "exit_code": parsed_exit_code})
    return evidence


def parse_knowledge_criteria(values: list[str]) -> list[dict[str, Any]]:
    criteria = []
    for value in values:
        criterion, separator, status = value.rpartition("::")
        if separator and status in ("passed", "failed"):
            criterion = criterion.strip()
        else:
            criterion, status = value.strip(), "passed"
        if not criterion:
            raise SystemExit(
                "Each --criterion must be a non-empty description, optionally "
                "suffixed with ::passed or ::failed"
            )
        criteria.append({"criterion": criterion, "status": status})
    return criteria


def parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} must be valid JSON: {error.msg}") from error
    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return parsed


def _linux_process_state(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except OSError:
        return None
    command_end = stat.rfind(")")
    if command_end < 0:
        return None
    fields = stat[command_end + 1 :].strip().split(maxsplit=1)
    return fields[0] if fields else None


def _posix_process_is_running(pid: int) -> bool:
    if sys.platform.startswith("linux") and _linux_process_state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    return _posix_process_is_running(pid)


def ensure_service_stopped(settings) -> None:
    pid_path = settings.data_dir / "server.pid"
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except ValueError:
        pid_path.unlink(missing_ok=True)
        return
    if process_is_running(pid):
        raise SystemExit(
            "The AgentChatRoom service is running; stop it before restoring a database"
        )
    pid_path.unlink(missing_ok=True)


def access_log_enabled() -> bool:
    """Foreground `serve` prints request logs unless explicitly disabled.

    Browser presence polling emits roughly one access-log line per second, which
    buries startup and error output in a resident console window. The
    AGENTCHATROOM_ACCESS_LOG environment variable lets launchers turn only the
    request noise off while keeping uvicorn startup and error logs visible.
    """
    return os.getenv("AGENTCHATROOM_ACCESS_LOG", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def start_detached_server(
    settings,
    host: str,
    port: int,
    config_path: str | None = None,
) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pid_path = settings.data_dir / "server.pid"
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="ascii").strip())
        except ValueError:
            pid_path.unlink(missing_ok=True)
        else:
            if process_is_running(existing_pid):
                return {
                    "started": False,
                    "already_running": True,
                    "pid": existing_pid,
                    "url": service_url(host, port),
                    "log_path": str(settings.data_dir / "server.log"),
                }
            pid_path.unlink(missing_ok=True)

    log_path = settings.data_dir / "server.log"
    environment = os.environ.copy()
    environment["AGENTCHATROOM_DATA_DIR"] = str(settings.data_dir)
    command = [
        sys.executable,
        "-m",
        "agentchatroom",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if config_path:
        command.extend(["--config", config_path])
    creationflags = 0
    popen_options: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        popen_options = {"creationflags": creationflags}
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=os.getcwd(),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **popen_options,
        )
    pid_path.write_text(str(process.pid), encoding="ascii")
    display_url = service_url(host, port)
    health_url = f"{display_url}/health"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise SystemExit(f"AgentChatRoom failed to start; inspect {log_path}")
        try:
            request_json(health_url, "GET", "")
            return {
                "started": True,
                "pid": process.pid,
                "url": display_url,
                "log_path": str(log_path),
            }
        except SystemExit:
            time.sleep(0.1)
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
    pid_path.unlink(missing_ok=True)
    raise SystemExit(f"AgentChatRoom did not become healthy; inspect {log_path}")


def stop_detached_server(settings) -> dict[str, Any]:
    pid_path = settings.data_dir / "server.pid"
    if not pid_path.exists():
        return {"stopped": False, "reason": "not_running"}
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except ValueError as error:
        pid_path.unlink(missing_ok=True)
        return {"stopped": False, "reason": str(error)}
    if not process_is_running(pid):
        pid_path.unlink(missing_ok=True)
        return {"stopped": False, "reason": "not_running", "pid": pid}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        if not process_is_running(pid):
            pid_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": pid}
        return {"stopped": False, "reason": str(error)}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            break
        time.sleep(0.1)
    if process_is_running(pid):
        return {"stopped": False, "reason": "timeout", "pid": pid}
    pid_path.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentchatroom")
    parser.add_argument(
        "--url",
        default=os.getenv("AGENTCHATROOM_URL", "http://127.0.0.1:8765"),
        help="AgentChatRoom service URL",
    )
    parser.add_argument("--json", action="store_true", help="Print compact JSON")
    parser.add_argument(
        "--request-id",
        help=(
            "Stable idempotency key for a retried write. Place this option before "
            "the command name."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the local service")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--config")
    serve.add_argument("--detach", action="store_true", help="Run in the background")
    serve.add_argument(
        "--open-browser", action="store_true", help="Open the Web interface after startup"
    )

    config_check = commands.add_parser(
        "config-check", help="Validate effective deployment configuration"
    )
    config_check.add_argument("--config")

    stop = commands.add_parser("stop", help="Stop a detached local service")
    stop.add_argument("--config")

    logs = commands.add_parser("logs", help="Show the detached service log")
    logs.add_argument("--config")
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--follow", action="store_true")

    mcp_config = commands.add_parser("mcp-config", help="Print an MCP client configuration")
    mcp_config.add_argument("--config")
    mcp_config.add_argument(
        "--data-dir",
        help="Runtime data directory to embed in local stdio MCP configuration",
    )
    mcp_config.add_argument(
        "--format",
        choices=["generic-json", "workbuddy-json", "grok-toml", "codex-toml"],
        default="generic-json",
    )
    mcp_config.add_argument(
        "--transport",
        choices=["local-stdio", "streamable-http", "remote-bridge"],
        default="local-stdio",
    )

    project = commands.add_parser("project-add", help="Add a project")
    project.add_argument("root_path")
    project.add_argument("--name")

    commands.add_parser("project-list", help="List projects")

    project_update = commands.add_parser("project-update", help="Update project settings")
    project_update.add_argument("project_id")
    project_update.add_argument("--name")
    project_update.add_argument("--settings-json")

    project_export = commands.add_parser("project-export", help="Export project room data")
    project_export.add_argument("project_id")
    project_export.add_argument("--output")

    member_list = commands.add_parser(
        "member-list", help="List Project members"
    )
    member_list.add_argument("project_id")
    member_list.add_argument(
        "--active-only", action="store_true", help="Hide revoked members"
    )

    member_create = commands.add_parser(
        "member-create", help="Create a Project member"
    )
    member_create.add_argument("project_id")
    member_create.add_argument("member_key")
    member_create.add_argument("name")
    member_create.add_argument("--kind", default="agent")
    member_create.add_argument("--role", default="")
    member_create.add_argument(
        "--status",
        choices=["invited", "active", "suspended", "revoked"],
        default="active",
    )
    member_create.add_argument("--metadata-json")

    member_update = commands.add_parser(
        "member-update", help="Update a Project member"
    )
    member_update.add_argument("project_id")
    member_update.add_argument("member_id")
    member_update.add_argument("--name")
    member_update.add_argument("--kind")
    member_update.add_argument("--role")
    member_update.add_argument(
        "--status", choices=["invited", "active", "suspended", "revoked"]
    )
    member_update.add_argument("--metadata-json")

    member_revoke = commands.add_parser(
        "member-revoke", help="Revoke a Project member"
    )
    member_revoke.add_argument("project_id")
    member_revoke.add_argument("member_id")

    backup = commands.add_parser(
        "backup", help="Create a consistent SQLite or PostgreSQL database backup"
    )
    backup.add_argument("--config")
    backup.add_argument("--output", required=True)

    restore = commands.add_parser(
        "restore", help="Restore a SQLite or PostgreSQL database backup"
    )
    restore.add_argument("--config")
    restore.add_argument("--input", required=True)
    restore.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm replacing the configured database",
    )

    project_archive = commands.add_parser("project-archive", help="Archive a project")
    project_archive.add_argument("project_id")

    project_delete = commands.add_parser(
        "project-delete", help="Permanently delete a project and all room data"
    )
    project_delete.add_argument("project_id")
    project_delete.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm irreversible deletion",
    )

    join = commands.add_parser("room-join", help="Join a project room")
    join.add_argument("project_id")
    join.add_argument("--agent-key", required=True)
    join.add_argument("--name", required=True)
    join.add_argument("--client", required=True)
    join.add_argument("--model", required=True)
    join.add_argument("--role", default="executor")
    join.add_argument("--branch", default="")
    join.add_argument("--worktree", default="")

    sync = commands.add_parser("room-sync", help="Read room state and events")
    sync.add_argument("project_id")
    sync.add_argument("--after", type=int, default=0)
    sync.add_argument("--session-id")
    sync.add_argument("--token")

    leave = commands.add_parser("room-leave", help="Close an Agent session")
    leave.add_argument("project_id")
    leave.add_argument("--session-id", required=True)
    leave.add_argument("--token", required=True)

    message = commands.add_parser("message-post", help="Post a room message")
    message.add_argument("project_id")
    message.add_argument("body")
    message.add_argument("--kind", default="message")
    message.add_argument("--session-id")
    message.add_argument("--token")
    message.add_argument(
        "--model-display-name",
        help="Model name shown in the Agent client UI; required with --session-id",
    )
    message.add_argument("--task-id")
    message.add_argument("--mention", action="append", default=[])
    message.add_argument("--file", action="append", default=[])
    message.add_argument("--requires-ack", action="store_true")
    message.add_argument("--priority", type=int, default=2)
    message.add_argument(
        "--channel",
        choices=["public", "task", "review", "system"],
        default="public",
    )

    task = commands.add_parser("task-create", help="Create a task")
    task.add_argument("project_id")
    task.add_argument("title")
    task.add_argument("--description", default="")
    task.add_argument("--accept", action="append", default=[])
    task.add_argument("--depends-on", action="append", default=[])
    task.add_argument("--priority", type=int, default=2)

    tasks = commands.add_parser("task-list", help="List tasks")
    tasks.add_argument("project_id")
    tasks.add_argument("--status")

    task_get = commands.add_parser("task-get", help="Get one task")
    task_get.add_argument("project_id")
    task_get.add_argument("task_id")

    claim = commands.add_parser("task-claim", help="Claim a task")
    claim.add_argument("project_id")
    claim.add_argument("task_id")
    claim.add_argument("--session-id", required=True)
    claim.add_argument("--token", required=True)

    task_update = commands.add_parser("task-update", help="Update a task")
    task_update.add_argument("project_id")
    task_update.add_argument("task_id")
    task_update.add_argument("--status")
    task_update.add_argument("--title")
    task_update.add_argument("--description")
    task_update.add_argument("--accept", action="append")
    task_update.add_argument("--depends-on", action="append")
    task_update.add_argument("--priority", type=int)
    task_update.add_argument("--progress", type=int)
    task_update.add_argument("--current-step")
    task_update.add_argument("--blocker-reason")
    task_update.add_argument("--next-step")
    task_update.add_argument("--session-id")
    task_update.add_argument("--token")

    handoff = commands.add_parser("task-handoff", help="Hand off an owned task")
    handoff.add_argument("project_id")
    handoff.add_argument("task_id")
    handoff.add_argument("summary")
    handoff.add_argument("--from-session-id", required=True)
    handoff.add_argument("--token", required=True)
    handoff.add_argument("--to-session-id", required=True)
    handoff.add_argument("--completed", action="append", default=[])
    handoff.add_argument("--pending", action="append", default=[])
    handoff.add_argument("--file", action="append", default=[])
    handoff.add_argument("--risk", action="append", default=[])
    handoff.add_argument("--next-step", required=True)

    handoff_ack = commands.add_parser(
        "task-handoff-ack", help="Acknowledge a pending task handoff"
    )
    handoff_ack.add_argument("project_id")
    handoff_ack.add_argument("task_id")
    handoff_ack.add_argument("handoff_id")
    handoff_ack.add_argument("--session-id", required=True)
    handoff_ack.add_argument("--token", required=True)
    handoff_ack.add_argument(
        "--response", choices=["accepted", "declined", "blocked"], required=True
    )
    handoff_ack.add_argument("--note", default="")

    lease = commands.add_parser("lease-acquire", help="Acquire a file lease")
    lease.add_argument("project_id")
    lease.add_argument("path_pattern")
    lease.add_argument("--session-id", required=True)
    lease.add_argument("--token", required=True)
    lease.add_argument("--task-id")
    lease.add_argument(
        "--mode", choices=["readonly", "shared", "exclusive"], default="exclusive"
    )
    lease.add_argument("--ttl", type=int)
    lease.add_argument("--reason", default="")

    release = commands.add_parser("lease-release", help="Release a file lease")
    release.add_argument("project_id")
    release.add_argument("lease_id")
    release.add_argument("--session-id", required=True)
    release.add_argument("--token", required=True)

    lease_check = commands.add_parser(
        "lease-check", help="Check staged paths against active exclusive leases"
    )
    lease_check.add_argument("project_id")
    lease_check.add_argument("--path", action="append", required=True)
    lease_check.add_argument("--session-id")
    lease_check.add_argument("--token")

    report = commands.add_parser("work-report", help="Submit work evidence")
    report.add_argument("project_id")
    report.add_argument("task_id")
    report.add_argument("summary")
    report.add_argument("--session-id", required=True)
    report.add_argument("--token", required=True)
    report.add_argument("--file", action="append", default=[])
    report.add_argument("--no-code-change-reason", default="")
    report.add_argument("--test", action="append", required=True, help="COMMAND::EXIT_CODE")
    report.add_argument("--commit", default="")

    review = commands.add_parser("review-submit", help="Submit independent review")
    review.add_argument("project_id")
    review.add_argument("task_id")
    review.add_argument("--session-id", required=True)
    review.add_argument("--token", required=True)
    review.add_argument("--verdict", choices=["approved", "changes_requested"], required=True)
    review.add_argument("--criterion", action="append", required=True)
    review.add_argument("--notes", default="")

    integration = commands.add_parser(
        "integration-submit", help="Submit final integration evidence"
    )
    integration.add_argument("project_id")
    integration.add_argument("task_id")
    integration.add_argument("summary")
    integration.add_argument("--session-id", required=True)
    integration.add_argument("--token", required=True)
    integration.add_argument("--result", choices=["done", "failed"], required=True)
    integration.add_argument("--file", action="append", default=[])
    integration.add_argument("--test", action="append", required=True, help="COMMAND::EXIT_CODE")
    integration.add_argument("--commit", default="")

    knowledge_submit = commands.add_parser(
        "knowledge-submit", help="Submit a Knowledge Asset candidate version"
    )
    knowledge_submit.add_argument("project_id")
    knowledge_submit.add_argument("title")
    knowledge_submit.add_argument("body")
    knowledge_submit.add_argument("--kind", required=True)
    knowledge_submit.add_argument("--session-id", required=True)
    knowledge_submit.add_argument("--token", required=True)
    knowledge_submit.add_argument("--summary", default="")
    knowledge_submit.add_argument("--tag", action="append", default=[])
    knowledge_submit.add_argument("--source-type", default="manual")
    knowledge_submit.add_argument("--source-task-id", default="")
    knowledge_submit.add_argument("--source-report-id", default="")
    knowledge_submit.add_argument("--source-review-id", default="")
    knowledge_submit.add_argument("--source-integration-id", default="")
    knowledge_submit.add_argument("--source-event", action="append", type=int, default=[])
    knowledge_submit.add_argument("--asset-id", default="")

    knowledge_list = commands.add_parser(
        "knowledge-list", help="List Knowledge Assets"
    )
    knowledge_list.add_argument("project_id")
    knowledge_list.add_argument("--status")
    knowledge_list.add_argument("--kind")
    knowledge_list.add_argument("--source-task-id")

    knowledge_get = commands.add_parser(
        "knowledge-get", help="Read one Knowledge Asset with its history"
    )
    knowledge_get.add_argument("project_id")
    knowledge_get.add_argument("asset_id")
    knowledge_get.add_argument("--version-id", default="")

    knowledge_review = commands.add_parser(
        "knowledge-review", help="Review a candidate Knowledge Asset version"
    )
    knowledge_review.add_argument("project_id")
    knowledge_review.add_argument("asset_id")
    knowledge_review.add_argument("--session-id", required=True)
    knowledge_review.add_argument("--token", required=True)
    knowledge_review.add_argument(
        "--verdict", choices=["approved", "changes_requested"], required=True
    )
    knowledge_review.add_argument(
        "--criterion",
        action="append",
        required=True,
        help="CRITERION::passed or CRITERION::failed (defaults to passed)",
    )
    knowledge_review.add_argument("--notes", default="")

    knowledge_supersede = commands.add_parser(
        "knowledge-supersede", help="Supersede an approved Knowledge Asset"
    )
    knowledge_supersede.add_argument("project_id")
    knowledge_supersede.add_argument("asset_id")
    knowledge_supersede.add_argument("--session-id", required=True)
    knowledge_supersede.add_argument("--token", required=True)
    knowledge_supersede.add_argument("--reason", default="")

    knowledge_archive = commands.add_parser(
        "knowledge-archive", help="Archive a Knowledge Asset"
    )
    knowledge_archive.add_argument("project_id")
    knowledge_archive.add_argument("asset_id")
    knowledge_archive.add_argument("--session-id", required=True)
    knowledge_archive.add_argument("--token", required=True)
    knowledge_archive.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "config-check":
        settings = load_settings(args.config)
        print_result(validate_runtime_settings(settings), args.json)
        return
    if args.command == "serve":
        settings = load_settings(args.config)
        host = args.host or settings.host
        port = args.port or settings.port
        effective_settings = replace(settings, host=host, port=port)
        validate_runtime_settings(effective_settings)
        if args.detach:
            result = start_detached_server(effective_settings, host, port, args.config)
            if args.open_browser:
                webbrowser.open(result["url"])
            print_result(result, args.json)
            return
        if args.open_browser:
            timer = threading.Timer(0.8, webbrowser.open, args=(service_url(host, port),))
            timer.daemon = True
            timer.start()
        if host not in {"127.0.0.1", "localhost", "::1"} and not effective_settings.management_auth_required:
            raise SystemExit(
                "Non-loopback server binding requires management authentication"
            )
        app = create_app(effective_settings)
        uvicorn.run(
            app,
            host=host,
            port=port,
            proxy_headers=effective_settings.trusted_proxy_headers,
            forwarded_allow_ips=effective_settings.trusted_proxy_ips,
            access_log=access_log_enabled(),
        )
        return
    if args.command == "stop":
        print_result(stop_detached_server(load_settings(args.config)), args.json)
        return
    if args.command == "logs":
        if args.lines < 1:
            raise SystemExit("--lines must be at least 1")
        print_server_log(
            load_settings(args.config), lines=args.lines, follow=args.follow
        )
        return
    if args.command == "mcp-config":
        settings = load_settings(args.config, data_dir=args.data_dir)
        integration = build_mcp_integration(settings)
        if args.transport == "remote-bridge":
            formats = {
                "generic-json": "remote_bridge_json",
                "workbuddy-json": "remote_bridge_json",
                "grok-toml": "remote_bridge_toml",
                "codex-toml": "remote_bridge_toml",
            }
        elif args.transport == "streamable-http":
            formats = {
                "generic-json": "streamable_http_json",
                "workbuddy-json": "streamable_http_json",
                "grok-toml": "grok_streamable_http_toml",
                "codex-toml": "codex_streamable_http_toml",
            }
        else:
            formats = {
                "generic-json": "generic_json",
                "workbuddy-json": "generic_json",
                "grok-toml": "grok_toml",
                "codex-toml": "codex_toml",
            }
        output = integration[formats[args.format]]
        if args.format in {"grok-toml", "codex-toml"}:
            print(output, end="")
        else:
            print_result(output, args.json)
        return

    if args.command in {"backup", "restore"}:
        settings = load_settings(args.config)
        try:
            if args.command == "backup":
                output = Path(args.output).expanduser().resolve()
                if settings.database_backend == "sqlite":
                    result = backup_sqlite(settings.database_path, output)
                else:
                    database_url = os.getenv(settings.database_url_env, "").strip()
                    result = backup_postgresql(database_url, output)
            else:
                if not args.confirm:
                    raise SystemExit("restore requires --confirm")
                ensure_service_stopped(settings)
                source = Path(args.input).expanduser().resolve()
                if settings.database_backend == "sqlite":
                    result = restore_sqlite(
                        source,
                        settings.database_path,
                        overwrite=True,
                    )
                else:
                    database_url = os.getenv(settings.database_url_env, "").strip()
                    result = restore_postgresql(database_url, source)
        except BackupError as error:
            raise SystemExit(str(error)) from error
        print_result(result, args.json)
        return

    base = args.url

    def call_api(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return request_json(
            base,
            method,
            path,
            payload,
            request_id=args.request_id,
        )

    if args.command == "project-add":
        result = call_api(
            "POST",
            "/api/v1/projects",
            {"root_path": args.root_path, "name": args.name},
        )
    elif args.command == "project-list":
        result = call_api("GET", "/api/v1/projects")
    elif args.command == "project-update":
        result = call_api(
            "PATCH",
            f"/api/v1/projects/{args.project_id}",
            {
                "name": args.name,
                "settings": (
                    parse_json_object(args.settings_json, "--settings-json")
                    if args.settings_json is not None
                    else None
                ),
            },
        )
    elif args.command == "project-export":
        result = call_api(
            "GET", f"/api/v1/projects/{args.project_id}/export"
        )
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result = {"exported": True, "output": str(output_path)}
    elif args.command == "member-list":
        member_path = f"/api/v1/projects/{args.project_id}/members"
        if args.active_only:
            member_path += "?include_revoked=false"
        result = call_api(
            "GET",
            member_path,
            None,
        )
    elif args.command == "member-create":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/members",
            {
                "member_key": args.member_key,
                "name": args.name,
                "kind": args.kind,
                "role": args.role,
                "status": args.status,
                "metadata": (
                    parse_json_object(args.metadata_json, "--metadata-json")
                    if args.metadata_json is not None
                    else {}
                ),
            },
        )
    elif args.command == "member-update":
        result = call_api(
            "PATCH",
            f"/api/v1/projects/{args.project_id}/members/{args.member_id}",
            {
                "name": args.name,
                "kind": args.kind,
                "role": args.role,
                "status": args.status,
                "metadata": (
                    parse_json_object(args.metadata_json, "--metadata-json")
                    if args.metadata_json is not None
                    else None
                ),
            },
        )
    elif args.command == "member-revoke":
        result = call_api(
            "DELETE",
            f"/api/v1/projects/{args.project_id}/members/{args.member_id}",
        )
    elif args.command == "project-archive":
        result = call_api(
            "DELETE",
            f"/api/v1/projects/{args.project_id}",
        )
    elif args.command == "project-delete":
        if not args.confirm:
            raise SystemExit("project-delete requires --confirm")
        result = call_api(
            "DELETE",
            f"/api/v1/projects/{args.project_id}?permanent=true",
        )
    elif args.command == "room-join":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/agents/join",
            {
                "agent_key": args.agent_key,
                "name": args.name,
                "client": args.client,
                "model": args.model,
                "role": args.role,
                "branch": args.branch,
                "worktree": args.worktree,
            },
        )
    elif args.command == "room-sync":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/sync",
            {
                "after": args.after,
                "session_id": args.session_id,
                "token": args.token,
            },
        )
    elif args.command == "room-leave":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/agents/{args.session_id}/leave",
            {"token": args.token},
        )
    elif args.command == "message-post":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/messages",
            {
                "body": args.body,
                "kind": args.kind,
                "session_id": args.session_id,
                "token": args.token,
                "model_display_name": args.model_display_name,
                "task_id": args.task_id,
                "mentions": args.mention,
                "files": args.file,
                "requires_ack": args.requires_ack,
                "priority": args.priority,
                "channel": args.channel,
            },
        )
    elif args.command == "task-create":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks",
            {
                "title": args.title,
                "description": args.description,
                "acceptance_criteria": args.accept,
                "depends_on": args.depends_on,
                "priority": args.priority,
            },
        )
    elif args.command == "task-list":
        query = f"?{urllib.parse.urlencode({'status': args.status})}" if args.status else ""
        result = call_api("GET", f"/api/v1/projects/{args.project_id}/tasks{query}")
    elif args.command == "task-get":
        result = call_api(
            "GET",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}",
        )
    elif args.command == "task-claim":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}/claim",
            {"session_id": args.session_id, "token": args.token},
        )
    elif args.command == "task-update":
        result = call_api(
            "PATCH",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}",
            {
                "status": args.status,
                "title": args.title,
                "description": args.description,
                "acceptance_criteria": args.accept,
                "depends_on": args.depends_on,
                "priority": args.priority,
                "progress_percent": args.progress,
                "current_step": args.current_step,
                "blocker_reason": args.blocker_reason,
                "next_step": args.next_step,
                "session_id": args.session_id,
                "token": args.token,
            },
        )
    elif args.command == "task-handoff":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}/handoffs",
            {
                "from_session_id": args.from_session_id,
                "token": args.token,
                "to_session_id": args.to_session_id,
                "summary": args.summary,
                "completed_items": args.completed,
                "pending_items": args.pending,
                "files": args.file,
                "risks": args.risk,
                "next_step": args.next_step,
            },
        )
    elif args.command == "task-handoff-ack":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}/handoffs/"
            f"{args.handoff_id}/acknowledge",
            {
                "session_id": args.session_id,
                "token": args.token,
                "response": args.response,
                "note": args.note,
            },
        )
    elif args.command == "lease-acquire":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/leases",
            {
                "session_id": args.session_id,
                "token": args.token,
                "path_pattern": args.path_pattern,
                "mode": args.mode,
                "task_id": args.task_id,
                "ttl_seconds": args.ttl,
                "reason": args.reason,
            },
        )
    elif args.command == "lease-release":
        query = urllib.parse.urlencode({"session_id": args.session_id, "token": args.token})
        result = call_api(
            "DELETE",
            f"/api/v1/projects/{args.project_id}/leases/{args.lease_id}?{query}",
        )
    elif args.command == "lease-check":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/leases/check",
            {
                "paths": args.path,
                "session_id": args.session_id,
                "token": args.token,
            },
        )
    elif args.command == "work-report":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}/reports",
            {
                "session_id": args.session_id,
                "token": args.token,
                "summary": args.summary,
                "files": args.file,
                "tests": parse_test_evidence(args.test),
                "commit_hash": args.commit,
                "no_code_change_reason": args.no_code_change_reason,
            },
        )
    elif args.command == "review-submit":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}/reviews",
            {
                "reviewer_session_id": args.session_id,
                "token": args.token,
                "verdict": args.verdict,
                "criteria": [{"criterion": value, "status": "passed"} for value in args.criterion],
                "notes": args.notes,
            },
        )
    elif args.command == "integration-submit":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/tasks/{args.task_id}/integrations",
            {
                "integrator_session_id": args.session_id,
                "token": args.token,
                "result": args.result,
                "summary": args.summary,
                "files": args.file,
                "tests": parse_test_evidence(args.test),
                "commit_hash": args.commit,
            },
        )
    elif args.command == "knowledge-submit":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/knowledge/assets",
            {
                "session_id": args.session_id,
                "token": args.token,
                "title": args.title,
                "body": args.body,
                "kind": args.kind,
                "summary": args.summary,
                "tags": args.tag,
                "source_type": args.source_type,
                "source_task_id": args.source_task_id,
                "source_report_id": args.source_report_id,
                "source_review_id": args.source_review_id,
                "source_integration_id": args.source_integration_id,
                "source_event_ids": args.source_event,
                "asset_id": args.asset_id,
            },
        )
    elif args.command == "knowledge-list":
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in {
                    "status": args.status,
                    "kind": args.kind,
                    "source_task_id": args.source_task_id,
                }.items()
                if value
            }
        )
        suffix = f"?{query}" if query else ""
        result = call_api(
            "GET", f"/api/v1/projects/{args.project_id}/knowledge/assets{suffix}"
        )
    elif args.command == "knowledge-get":
        query = (
            f"?{urllib.parse.urlencode({'version_id': args.version_id})}"
            if args.version_id
            else ""
        )
        result = call_api(
            "GET",
            f"/api/v1/projects/{args.project_id}/knowledge/assets/"
            f"{args.asset_id}{query}",
        )
    elif args.command == "knowledge-review":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/knowledge/assets/"
            f"{args.asset_id}/reviews",
            {
                "reviewer_session_id": args.session_id,
                "token": args.token,
                "verdict": args.verdict,
                "criteria": parse_knowledge_criteria(args.criterion),
                "notes": args.notes,
            },
        )
    elif args.command == "knowledge-supersede":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/knowledge/assets/"
            f"{args.asset_id}/supersede",
            {"session_id": args.session_id, "token": args.token, "reason": args.reason},
        )
    elif args.command == "knowledge-archive":
        result = call_api(
            "POST",
            f"/api/v1/projects/{args.project_id}/knowledge/assets/"
            f"{args.asset_id}/archive",
            {"session_id": args.session_id, "token": args.token, "reason": args.reason},
        )
    else:
        parser.error("Unsupported command")
        return
    print_result(result, args.json)


if __name__ == "__main__":
    main(sys.argv[1:])
