from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import SSELimiter, create_app
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService, request_payload_for_hash
from agentchatroom.local_mcp import LocalMcpConfigurator, LocalMcpEnvironment
from agentchatroom.integrations import build_mcp_integration
from agentchatroom.config import Settings


def test_authenticate_agent_token_verify_stays_on_read_path(service, project):
    issued = service.issue_agent_token(project["id"], name="reader")
    token = issued["token"]
    started = threading.Event()
    release = threading.Event()

    def hold_writer():
        with service.database.connect(write=True):
            started.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_writer, daemon=True)
    holder.start()
    assert started.wait(timeout=2)
    began = time.monotonic()
    credential = service.authenticate_agent_token(token, touch=True)
    elapsed = time.monotonic() - began
    release.set()
    holder.join(timeout=2)
    assert credential["id"] == issued["credential"]["id"]
    assert elapsed < 0.75

    with pytest.raises(DomainError) as revoked:
        service.authenticate_agent_token("acr.missing." + ("a" * 32), touch=True)
    assert revoked.value.code == "invalid_agent_token"

    service.revoke_agent_token(project["id"], issued["credential"]["id"])
    with pytest.raises(DomainError) as blocked:
        service.authenticate_agent_token(token, touch=True)
    assert blocked.value.code == "agent_token_revoked"
    service.close()


def test_concurrent_create_task_allocates_unique_task_numbers(service, project):
    def create_one(index: int):
        return service.create_task(
            project["id"],
            title=f"Concurrent task {index}",
            acceptance_criteria=["Unique number"],
        )["task"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = list(pool.map(create_one, range(20)))
    numbers = sorted(task["task_number"] for task in tasks)
    assert numbers == list(range(1, 21))
    assert len({task["id"] for task in tasks}) == 20


def test_list_tasks_does_not_use_per_task_relation_queries(
    service, project, joined_agents, monkeypatch
):
    executor, _reviewer = joined_agents
    first = service.create_task(
        project["id"],
        title="First",
        acceptance_criteria=["One"],
    )["task"]
    service.create_task(
        project["id"],
        title="Second",
        acceptance_criteria=["Two"],
        depends_on=[first["id"]],
    )
    calls = {"count": 0}
    original = AgentChatRoomService._task_with_dependencies

    def wrapped(self, connection, row):
        calls["count"] += 1
        return original(self, connection, row)

    monkeypatch.setattr(AgentChatRoomService, "_task_with_dependencies", wrapped)
    listed = service.list_tasks(project["id"])
    members = service.list_project_members(project["id"])
    snapshot = service.snapshot(project["id"])
    assert calls["count"] == 0
    assert len(listed) == 2
    assert listed[1]["depends_on"] == [first["id"]]
    assert members
    assert snapshot["members"]
    assert executor["agent"]["id"] in {agent["id"] for agent in snapshot["agents"]}


def test_work_report_rejects_unregistered_and_untrusted_worktrees(
    service, project
):
    unregistered = service.join_room(
        project["id"],
        name="Stranger",
        client="codex",
        model="test-model",
    )
    task = service.create_task(
        project["id"],
        title="Workspace gate",
        acceptance_criteria=["Registered workspace required"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], unregistered["agent"]["id"], unregistered["token"]
    )
    with pytest.raises(DomainError) as missing:
        service.submit_work_report(
            project["id"],
            task["id"],
            session_id=unregistered["agent"]["id"],
            token=unregistered["token"],
            summary="Attempt without workspace",
            files=["src/a.py"],
            tests=[{"command": "pytest", "exit_code": 0}],
        )
    assert missing.value.code == "workspace_not_registered"
    assert "未登记工作区" in missing.value.message

    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )
    trusted = service.join_room(
        project["id"],
        name="Trusted",
        client="trae",
        model="test-model",
        worktree=str(Path(project["root_path"]) / "nested"),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    Path(project["root_path"], "nested").mkdir(exist_ok=True)
    other = service.create_task(
        project["id"],
        title="Untrusted worktree",
        acceptance_criteria=["Path must stay inside workspace"],
    )["task"]
    service.claim_task(
        project["id"], other["id"], trusted["agent"]["id"], trusted["token"]
    )
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET worktree = ? WHERE id = ?",
            (str(Path(project["root_path"]).parent / "elsewhere"), trusted["agent"]["id"]),
        )
    with pytest.raises(DomainError) as untrusted:
        service.submit_work_report(
            project["id"],
            other["id"],
            session_id=trusted["agent"]["id"],
            token=trusted["token"],
            summary="Forged path",
            files=["src/a.py"],
            tests=[{"command": "pytest", "exit_code": 0}],
        )
    assert untrusted.value.code == "untrusted_worktree"

    restored = service.join_room(
        project["id"],
        name="Hasher",
        client="workbuddy",
        model="test-model",
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    hashed = service.create_task(
        project["id"],
        title="Commit gate",
        acceptance_criteria=["Commit must parse"],
    )["task"]
    service.claim_task(
        project["id"], hashed["id"], restored["agent"]["id"], restored["token"]
    )
    with pytest.raises(DomainError) as forged:
        service.submit_work_report(
            project["id"],
            hashed["id"],
            session_id=restored["agent"]["id"],
            token=restored["token"],
            summary="Fake commit",
            files=["src/a.py"],
            tests=[{"command": "pytest", "exit_code": 0}],
            commit_hash="abc123",
        )
    assert forged.value.code == "invalid_commit_hash"
    assert forged.value.details["commit_hash"] == "unknown"
    audit = service.query_audit(
        project["id"],
        task_id=hashed["id"],
        event_type="work_report.commit_unverified",
    )
    assert audit["events"]
    assert audit["events"][0]["payload"]["commit_hash"] == "unknown"
    assert audit["events"][0]["payload"]["reported_commit_hash"] == "abc123"


def test_next_task_number_insert_uses_on_conflict_without_aborting_transaction():
    class UniqueError(Exception):
        pass

    class Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class FakeConnection:
        def __init__(self):
            self.seq = None
            self.aborted = False
            self.sql: list[str] = []

        def execute(self, sql, params=()):
            self.sql.append(sql)
            if self.aborted:
                raise RuntimeError("current transaction is aborted")
            if "SELECT next_value" in sql:
                if self.seq is None:
                    return Result(None)
                return Result({"next_value": self.seq})
            if "MAX(task_number)" in sql:
                return Result({"task_number": 0})
            if "INSERT INTO task_number_sequences" in sql:
                if "ON CONFLICT" not in sql.upper():
                    self.aborted = True
                    raise UniqueError("duplicate key")
                if self.seq is None:
                    self.seq = int(params[1])
                return Result(None)
            if "UPDATE task_number_sequences" in sql:
                self.seq = int(params[0])
                return Result(None)
            return Result(None)

    service = AgentChatRoomService.__new__(AgentChatRoomService)
    service.database = type("Database", (), {"backend": "postgresql"})()
    connection = FakeConnection()
    allocated = AgentChatRoomService._next_task_number(
        service, connection, "project_example"
    )
    assert allocated == 1
    assert any("ON CONFLICT" in sql for sql in connection.sql)
    assert connection.aborted is False


def test_sse_client_ip_ignores_spoofed_forwarded_for_from_untrusted_peers():
    from types import SimpleNamespace

    from agentchatroom.api import request_client_ip, trusted_proxy_peer

    assert trusted_proxy_peer("203.0.113.10", "127.0.0.1") is False
    assert trusted_proxy_peer("127.0.0.1", "127.0.0.1") is True
    assert trusted_proxy_peer("10.1.2.3", "10.0.0.0/8") is True
    untrusted = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"x-forwarded-for": "198.51.100.7"},
    )
    assert request_client_ip(
        untrusted,
        trusted_proxy_headers=True,
        trusted_proxy_ips="127.0.0.1",
    ) == "203.0.113.10"
    trusted = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "198.51.100.7"},
    )
    assert request_client_ip(
        trusted,
        trusted_proxy_headers=True,
        trusted_proxy_ips="127.0.0.1",
    ) == "198.51.100.7"


def test_list_members_and_tasks_scale_for_100_members_200_tasks(service, project):
    for index in range(100):
        service.create_project_member(
            project["id"],
            member_key=f"scale-agent-{index}",
            name=f"Scale Agent {index}",
            kind="agent",
        )
    for index in range(200):
        created = service.create_task(
            project["id"],
            title=f"Scale task {index}",
            acceptance_criteria=["Unique number"],
        )["task"]
        assert created["phase"] == "todo"
    started = time.perf_counter()
    members = service.list_project_members(project["id"])
    tasks = service.list_tasks(project["id"])
    elapsed = time.perf_counter() - started
    assert len(members) >= 100
    assert len(tasks) == 200
    assert {task["phase"] for task in tasks} == {"todo"}
    assert elapsed < 2.5


def test_sensitive_request_fields_hash_authorization_metadata():
    hashed = request_payload_for_hash(
        {"Authorization": "Bearer secret-token", "note": "ok"},
        field_name="metadata",
    )
    assert hashed["Authorization"]["sha256"]
    assert "secret-token" not in str(hashed)
    assert hashed["note"] == "ok"


def test_event_stream_requires_credentials_and_enforces_limits(settings, project_dir):
    limited = replace(settings, max_sse_clients_per_project=2, sse_per_ip_limit=8)
    with TestClient(create_app(limited)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "SSE Project"},
        ).json()
        anonymous = client.get(
            f"/api/v1/projects/{project['id']}/events/stream",
            headers={"Accept": "application/json"},
        )
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "unauthorized_sse"
        joined = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={"name": "Streamer", "client": "codex", "model": "test-model"},
        ).json()
        missing_token = client.get(
            f"/api/v1/projects/{project['id']}/events/stream",
            headers={
                "Accept": "application/json",
                "X-Agentchatroom-Session-Id": joined["agent"]["id"],
            },
        )
        assert missing_token.status_code == 401

    limiter = SSELimiter(max_per_project=64, max_per_ip=16)
    for index in range(64):
        limiter.acquire("project_limit", f"10.0.0.{index % 16}")
    with pytest.raises(DomainError) as exceeded:
        limiter.acquire("project_limit", "10.0.0.1")
    assert exceeded.value.code == "sse_limit_exceeded"
    assert exceeded.value.status_code == 429
    limiter.release("project_limit", "10.0.0.1")
    limiter.acquire("project_limit", "10.0.0.1")
    crowded = SSELimiter(max_per_project=200, max_per_ip=8)
    for index in range(8):
        crowded.acquire("project_ip", "203.0.113.10")
    with pytest.raises(DomainError) as ip_exceeded:
        crowded.acquire("project_ip", "203.0.113.10")
    assert ip_exceeded.value.details["scope"] == "ip"
    two_hundred = SSELimiter(max_per_project=199, max_per_ip=300)
    for index in range(199):
        two_hundred.acquire("busy-project", f"198.51.100.{index % 250}")
    with pytest.raises(DomainError) as project_exceeded:
        two_hundred.acquire("busy-project", "198.51.100.1")
    assert project_exceeded.value.details["scope"] == "project"


def test_local_mcp_backup_retries_collision_and_uses_private_mode(tmp_path):
    config_path = tmp_path / "home" / ".workbuddy" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    original = b'{"mcpServers": {"existing": {"command": "keep-me"}}}\n'
    config_path.write_bytes(original)
    taken = config_path.with_name(
        "mcp.json.agentchatroom-backup-20260830-123456-000000"
    )
    taken.write_bytes(b"occupied")
    configurator = LocalMcpConfigurator(
        LocalMcpEnvironment(
            home_dir=tmp_path / "home",
            appdata_dir=tmp_path / "appdata",
        ),
        now=lambda: datetime(2026, 8, 30, 12, 34, 56, tzinfo=timezone.utc),
    )
    profile = build_mcp_integration(
        Settings(data_dir=tmp_path / "runtime"),
        python_executable=tmp_path / "venv" / "python.exe",
        project={"name": "Example", "root_path": str(tmp_path / "project")},
    )["profiles"]["workbuddy"]
    (tmp_path / "project").mkdir(exist_ok=True)
    plan = configurator.plan(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
    )
    result = configurator.apply(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
        expected_current_sha256=plan["current_sha256"],
    )
    backup = Path(result["backup_path"])
    assert backup.exists()
    assert backup != taken
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
