"""Run a disposable PostgreSQL server and exercise the production database path.

The ``pgserver`` package is a test-only helper that bundles PostgreSQL binaries
for Windows. It is intentionally not part of AgentChatRoom runtime dependencies
or the documented production deployment.
"""

from __future__ import annotations

import argparse
import os
import socket
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify AgentChatRoom against a disposable PostgreSQL server."
    )
    parser.add_argument("--data-dir", type=Path, help="Optional runtime directory for the temporary app")
    return parser.parse_args()


def create_database(admin_url: str, database_name: str) -> str:
    import psycopg

    connection = psycopg.connect(admin_url, autocommit=True)
    try:
        connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        connection.close()
    return admin_url.rsplit("/", 1)[0] + "/" + database_name


def drop_database(admin_url: str, database_name: str) -> None:
    import psycopg

    connection = psycopg.connect(admin_url, autocommit=True)
    try:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        connection.close()


def count_rows(database_url: str, table: str) -> int:
    import psycopg

    connection = psycopg.connect(database_url)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_postgres(pgdata: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    from pgserver._commands import POSTGRES_BIN_PATH

    log = log_path.open("wb")
    process = subprocess.Popen(
        [
            str(POSTGRES_BIN_PATH / "postgres.exe"),
            "-D",
            str(pgdata),
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    process._agentchatroom_log = log  # type: ignore[attr-defined]
    return process


def wait_for_postgres(database_url: str, process: subprocess.Popen[bytes]) -> None:
    import psycopg

    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"postgres.exe exited early with code {process.returncode}")
        try:
            connection = psycopg.connect(database_url, connect_timeout=1)
            connection.close()
            return
        except psycopg.Error as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"PostgreSQL did not become ready: {last_error}")


def stop_postgres(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    log = getattr(process, "_agentchatroom_log", None)
    if log is not None:
        log.close()


def main() -> None:
    args = parse_args()
    try:
        from fastapi.testclient import TestClient
        from pgserver._commands import POSTGRES_BIN_PATH, initdb

        from agentchatroom.api import create_app
        from agentchatroom.backup import backup_postgresql, restore_postgresql
        from agentchatroom.config import Settings
    except ModuleNotFoundError as error:
        raise SystemExit(
            "PostgreSQL verification requires optional dependencies. Install "
            "the project with .[dev,postgresql] and the pgserver test package."
        ) from error

    root = ROOT / ".agentchatroom" / "verification" / "postgres-acceptance"
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / f"run-{uuid.uuid4().hex[:10]}"
    pgdata = run_dir / "pgdata"
    runtime_dir = args.data_dir or (run_dir / "runtime")
    server_process: subprocess.Popen[bytes] | None = None
    old_path = os.environ.get("PATH", "")
    old_lang = os.environ.get("LANG")
    old_lc_all = os.environ.get("LC_ALL")
    os.environ["PATH"] = str(POSTGRES_BIN_PATH) + os.pathsep + old_path
    os.environ["LANG"] = "C"
    os.environ["LC_ALL"] = "C"
    original_database = ""
    restored_database = ""
    restored_url = ""
    admin_url = ""
    settings = None
    management_token_env = "AGENTCHATROOM_POSTGRES_TEST_ADMIN"
    management_token = "postgres-management-test-token"
    try:
        pgdata.mkdir(parents=True, exist_ok=True)
        # pgserver's default initdb flags inherit the Windows locale and can
        # fail under a restricted token. Pre-initialize with explicit settings;
        # PostgresServer then only starts and supervises the cluster.
        initdb(
            ["--locale=C", "--encoding=UTF8", "--no-sync", "-U", "postgres", "-A", "trust"],
            pgdata=pgdata,
        )
        original_database = f"agentchatroom_{uuid.uuid4().hex[:10]}"
        restored_database = f"agentchatroom_restore_{uuid.uuid4().hex[:10]}"
        port = available_port()
        admin_url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        server_process = start_postgres(pgdata, port, run_dir / "postgres.log")
        wait_for_postgres(admin_url, server_process)
        database_url = create_database(admin_url, original_database)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            data_dir=runtime_dir,
            database_backend="postgresql",
            database_url_env="AGENTCHATROOM_TEST_DATABASE_URL",
            database_pool_min_size=1,
            database_pool_max_size=5,
            mcp_http_auth_required=False,
            management_auth_required=True,
            management_token_env=management_token_env,
        )
        os.environ[settings.database_url_env] = database_url
        os.environ[management_token_env] = management_token
        backup_path = run_dir / "room.dump"
        app = create_app(settings)
        with TestClient(app) as client:
            logged_in = client.post(
                "/api/v1/auth/login", json={"token": management_token}
            )
            logged_in.raise_for_status()
            management_cookie = client.cookies.get(settings.management_cookie_name)
            if not management_cookie:
                raise RuntimeError("PostgreSQL management login did not set a cookie")
            created = client.post(
                "/api/v1/projects",
                json={"root_path": str(runtime_dir), "name": "PostgreSQL verification"},
            )
            created.raise_for_status()
            project = created.json()
            joined = client.post(
                f"/api/v1/projects/{project['id']}/agents/join",
                json={
                    "agent_key": "postgresql-verifier-main",
                    "name": "PostgreSQL Agent",
                    "client": "postgresql-verifier",
                    "model": "verification-model",
                    "role": "executor",
                },
            )
            joined.raise_for_status()
            snapshot = client.get(f"/api/v1/projects/{project['id']}/snapshot")
            snapshot.raise_for_status()
            before_cursor = snapshot.json()["cursor"]
            if before_cursor < 1:
                raise RuntimeError("PostgreSQL center did not persist an event")

        restarted = create_app(settings)
        with TestClient(restarted) as client:
            client.cookies.set(settings.management_cookie_name, management_cookie)
            auth_status = client.get("/api/v1/auth/status")
            auth_status.raise_for_status()
            if not auth_status.json().get("authenticated"):
                raise RuntimeError(
                    "PostgreSQL restart did not recover the management session"
                )
            recovered = client.get(f"/api/v1/projects/{project['id']}/snapshot")
            recovered.raise_for_status()
            if recovered.json()["cursor"] != before_cursor:
                raise RuntimeError("PostgreSQL restart changed the event cursor")

        backup_result = backup_postgresql(database_url, backup_path)
        restored_url = create_database(admin_url, restored_database)
        restore_result = restore_postgresql(restored_url, backup_path)
        source_projects = count_rows(database_url, "projects")
        restored_projects = count_rows(restored_url, "projects")
        source_events = count_rows(database_url, "events")
        restored_events = count_rows(restored_url, "events")
        if (source_projects, source_events) != (restored_projects, restored_events):
            raise RuntimeError("PostgreSQL restore counts do not match the source")
        print(
            {
                "backend": "postgresql",
                "schema_initialized": True,
                "restart_recovered": True,
                "management_session_restart_recovered": True,
                "backup_backend": backup_result["backend"],
                "restore_backend": restore_result["backend"],
                "projects_before_restore": source_projects,
                "projects_after_restore": restored_projects,
                "events_before_restore": source_events,
                "events_after_restore": restored_events,
            }
        )
    finally:
        if admin_url and restored_database and restored_url:
            try:
                drop_database(admin_url, restored_database)
            except Exception:
                pass
        if admin_url and original_database:
            try:
                drop_database(admin_url, original_database)
            except Exception:
                pass
        stop_postgres(server_process)
        if old_path:
            os.environ["PATH"] = old_path
        else:
            os.environ.pop("PATH", None)
        if old_lang is None:
            os.environ.pop("LANG", None)
        else:
            os.environ["LANG"] = old_lang
        if old_lc_all is None:
            os.environ.pop("LC_ALL", None)
        else:
            os.environ["LC_ALL"] = old_lc_all
        if settings is not None:
            os.environ.pop(settings.database_url_env, None)
        os.environ.pop(management_token_env, None)
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
