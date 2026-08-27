"""Verify the PostgreSQL profile through a real local HTTP server process.

This uses the test-only ``pgserver`` helper and a temporary database. It is
stronger than a TestClient-only check because the application is started as a
real Uvicorn process and accessed over HTTP, while still avoiding production
data, Agent Tokens, and external hosts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# When this file is executed directly (the documented use), Python puts the
# ``scripts`` directory on ``sys.path`` rather than the repository root. Add
# the root explicitly so the shared disposable-PostgreSQL helpers can be
# imported without requiring an installation-only entry point.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify AgentChatRoom PostgreSQL through a real HTTP server process."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help=(
            "Optional temporary root directory; defaults to "
            ".agentchatroom/verification/postgres-http-acceptance"
        ),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def start_app(env: dict[str, str], port: int, log_path: Path) -> subprocess.Popen[bytes]:
    log = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "agentchatroom", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    process._agentchatroom_log = log  # type: ignore[attr-defined]
    return process


def stop_app(process: subprocess.Popen[bytes] | None) -> None:
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


def wait_for_ready(
    client: Any, process: subprocess.Popen[bytes], timeout: float
) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"AgentChatRoom exited early with code {process.returncode}")
        try:
            response = client.get("/health/ready")
            if response.status_code == 200:
                return
            last_error = RuntimeError(f"health status {response.status_code}")
        except httpx.HTTPError as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"AgentChatRoom did not become ready: {last_error}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        import httpx
        from pgserver._commands import POSTGRES_BIN_PATH, initdb

        from scripts.verify_postgresql import (
            available_port,
            create_database,
            drop_database,
            start_postgres,
            stop_postgres,
            wait_for_postgres,
        )
    except ModuleNotFoundError as error:
        raise SystemExit(
            "PostgreSQL HTTP verification requires optional dependencies. "
            "Install the project with .[dev,postgresql] and the pgserver test "
            "package."
        ) from error

    root = (
        args.data_dir
        or ROOT
        / ".agentchatroom"
        / "verification"
        / "postgres-http-acceptance"
    ).resolve()
    run_dir = root / f"run-{uuid.uuid4().hex[:10]}"
    pgdata = run_dir / "pgdata"
    runtime = run_dir / "runtime"
    pg_process = None
    app_process = None
    admin_url = ""
    database_url = ""
    database_name = f"agentchatroom_http_{uuid.uuid4().hex[:10]}"
    admin_token = "postgres-http-admin-test-token"
    old_path = os.environ.get("PATH", "")
    try:
        pgdata.mkdir(parents=True, exist_ok=True)
        runtime.mkdir(parents=True, exist_ok=True)
        os.environ["PATH"] = str(POSTGRES_BIN_PATH) + os.pathsep + old_path
        initdb(
            ["--locale=C", "--encoding=UTF8", "--no-sync", "-U", "postgres", "-A", "trust"],
            pgdata=pgdata,
        )
        pg_port = available_port()
        admin_url = f"postgresql://postgres@127.0.0.1:{pg_port}/postgres"
        pg_process = start_postgres(pgdata, pg_port, run_dir / "postgres.log")
        wait_for_postgres(admin_url, pg_process)
        database_url = create_database(admin_url, database_name)

        app_port = available_port()
        env = os.environ.copy()
        env.update(
            {
                "AGENTCHATROOM_DATA_DIR": str(runtime),
                "AGENTCHATROOM_HOST": "127.0.0.1",
                "AGENTCHATROOM_PORT": str(app_port),
                "AGENTCHATROOM_DATABASE_BACKEND": "postgresql",
                "AGENTCHATROOM_DATABASE_URL_ENV": "AGENTCHATROOM_TEST_DATABASE_URL",
                "AGENTCHATROOM_TEST_DATABASE_URL": database_url,
                "AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED": "true",
                "AGENTCHATROOM_ADMIN_TOKEN": admin_token,
                "AGENTCHATROOM_MANAGEMENT_TOKEN_ENV": "AGENTCHATROOM_ADMIN_TOKEN",
                "AGENTCHATROOM_MCP_HTTP_AUTH_REQUIRED": "true",
            }
        )
        base_url = f"http://127.0.0.1:{app_port}"
        app_process = start_app(env, app_port, run_dir / "agentchatroom.log")
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            wait_for_ready(client, app_process, args.timeout)
            login = client.post("/api/v1/auth/login", json={"token": admin_token})
            login.raise_for_status()
            created = client.post(
                "/api/v1/projects",
                json={"root_path": str(runtime), "name": "PostgreSQL HTTP verification"},
            )
            created.raise_for_status()
            project = created.json()
            joined = client.post(
                f"/api/v1/projects/{project['id']}/agents/join",
                json={
                    "agent_key": "postgresql-http-verifier-main",
                    "name": "PostgreSQL HTTP Agent",
                    "client": "postgresql-http-verifier",
                    "model": "verification-model",
                    "role": "executor",
                },
            )
            joined.raise_for_status()
            before = client.get(f"/api/v1/projects/{project['id']}/snapshot")
            before.raise_for_status()
            before_cursor = before.json()["cursor"]

            stop_app(app_process)
            app_process = None
            app_process = start_app(env, app_port, run_dir / "agentchatroom-restart.log")
            wait_for_ready(client, app_process, args.timeout)
            auth_status = client.get("/api/v1/auth/status")
            auth_status.raise_for_status()
            recovered = client.get(f"/api/v1/projects/{project['id']}/snapshot")
            recovered.raise_for_status()
            after_cursor = recovered.json()["cursor"]
            if before_cursor != after_cursor:
                raise RuntimeError("HTTP PostgreSQL restart changed the event cursor")
            if not auth_status.json().get("authenticated"):
                raise RuntimeError("HTTP PostgreSQL restart did not recover the management cookie")
            print(
                json.dumps(
                    {
                        "backend": "postgresql",
                        "transport": "real_http",
                        "health_ready": True,
                        "management_session_restart_recovered": True,
                        "event_cursor_before_restart": before_cursor,
                        "event_cursor_after_restart": after_cursor,
                        "project_recovered": True,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        stop_app(app_process)
        if admin_url and database_name:
            try:
                drop_database(admin_url, database_name)
            except Exception:
                pass
        stop_postgres(pg_process)
        os.environ["PATH"] = old_path
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
