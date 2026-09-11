"""#119: presence/lease/reclaim decoupled from transport reaping."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from agentchatroom import mcp_server
from agentchatroom.api import create_app
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService
from agentchatroom.database import Database


@pytest.fixture()
def quiet_service(settings):
    """A service with a short heartbeat window for fast presence lapses."""
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def _expire_heartbeat(service, project_id: str, session_id: str) -> None:
    from agentchatroom.services import utc_now
    from datetime import timedelta

    stale = (
        utc_now() - timedelta(seconds=service.settings.heartbeat_timeout_seconds + 1)
    ).isoformat().replace("+00:00", "Z")
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
            (stale, session_id),
        )


def test_silence_is_display_only_and_keeps_leases_tasks_submission(
    quiet_service,
):
    """AC1+2：静默超窗仅显示离线；租约保持、所有权不变、提交照常。"""
    service = quiet_service
    project = service.create_project(root_path=str(service.settings.data_dir), name="Silent")
    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=str(service.settings.data_dir),
    )
    worker = service.join_room(
        project["id"],
        name="Worker",
        client="codex",
        model="unknown",
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"], title="Long quiet work", acceptance_criteria=["no noise"]
    )["task"]
    service.claim_task(project["id"], task["id"], worker["agent"]["id"], worker["token"])
    lease = service.acquire_lease(
        project["id"],
        session_id=worker["agent"]["id"],
        token=worker["token"],
        path_pattern="src/quiet/**",
    )["lease"]

    other = service.join_room(
        project["id"], name="Other", client="qoder", model="unknown"
    )

    time.sleep(service.settings.heartbeat_timeout_seconds + 0.3)

    # Display-only: the agent shows offline after the window.
    agents = {
        item["id"]: item
        for item in service.snapshot(project["id"])["agents"]
    }
    assert agents[worker["agent"]["id"]]["status"] == "offline"
    # The lease stays enforced for other identities despite the silence.
    with pytest.raises(DomainError) as excinfo:
        service.acquire_lease(
            project["id"],
            session_id=other["agent"]["id"],
            token=other["token"],
            path_pattern="src/quiet/file.py",
        )
    assert excinfo.value.code == "lease_conflict"
    # Task ownership is unchanged.
    stored = next(
        item
        for item in service.snapshot(project["id"])["tasks"]
        if item["id"] == task["id"]
    )
    assert stored["owner_session_id"] == worker["agent"]["id"]
    # The silent session can still submit messages and reports.
    posted = service.post_message(
        project["id"],
        body="#119 silent submission",
        session_id=worker["agent"]["id"],
        token=worker["token"],
        model_display_name="test",
    )
    assert posted["event_id"]
    report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=worker["agent"]["id"],
        token=worker["token"],
        summary="Quietly done",
        files=["src/quiet.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    assert report["report_id"]


def test_reclaim_uses_transport_state_not_the_presence_window(quiet_service):
    """AC3：transport 成墓碑立即允许同身份 reclaim；活跃 transport 拒绝；异身份拒绝。"""
    service = quiet_service
    project = service.create_project(root_path=str(service.settings.data_dir), name="Reclaim")
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    first = service.join_room(
        project["id"],
        name="OpenCode",
        client="opencode",
        model="unknown",
        member_id=member["id"],
    )
    task = service.create_task(
        project["id"], title="Crash recovery", acceptance_criteria=["reclaim fast"]
    )["task"]
    service.claim_task(project["id"], task["id"], first["agent"]["id"], first["token"])

    successor = service.join_room(
        project["id"],
        name="OpenCode successor",
        client="opencode",
        model="unknown",
        member_id=member["id"],
    )
    other = service.join_room(
        project["id"], name="Other", client="qoder", model="unknown"
    )

    # Owner heartbeat is fresh AND the transport reports alive: deny.
    service.transport_liveness_check = lambda pid, sid: (
        "alive" if sid == first["agent"]["id"] else "unknown"
    )
    with pytest.raises(DomainError) as alive_denied:
        service.claim_task(
            project["id"],
            task["id"],
            successor["agent"]["id"],
            successor["token"],
            reclaim=True,
        )
    assert alive_denied.value.code == "task_owner_session_connected"

    # Same fresh heartbeat, but the transport is now a tombstone: the same
    # identity reclaims immediately instead of waiting out the window.
    service.transport_liveness_check = lambda pid, sid: (
        "gone" if sid == first["agent"]["id"] else "unknown"
    )
    reclaimed = service.claim_task(
        project["id"],
        task["id"],
        successor["agent"]["id"],
        successor["token"],
        reclaim=True,
    )
    assert reclaimed["task"]["owner_session_id"] == successor["agent"]["id"]

    # A different identity is always denied, transport state aside.
    other_task = service.create_task(
        project["id"], title="Other identity probe", acceptance_criteria=["deny"]
    )["task"]
    service.claim_task(
        project["id"], other_task["id"], successor["agent"]["id"], successor["token"]
    )
    service.transport_liveness_check = lambda pid, sid: "gone"
    with pytest.raises(DomainError) as foreign_denied:
        service.claim_task(
            project["id"],
            other_task["id"],
            other["agent"]["id"],
            other["token"],
            reclaim=True,
        )
    assert foreign_denied.value.code == "task_reclaim_forbidden"


def test_api_wires_transport_state_into_the_service(settings, project_dir, tmp_path):
    """api 装配：服务的 transport 状态查询指向 MCP 墓碑/绑定表。"""
    settings = replace(settings, mcp_http_session_idle_timeout_seconds=1800)
    app = create_app(settings)
    assert app.state.service.transport_liveness_check is (
        mcp_server.session_transport_state
    )
    # Unknown for sessions without an HTTP transport binding.
    project = app.state.service.create_project(root_path=str(project_dir), name="Wiring")
    joined = app.state.service.join_room(
        project["id"], name="Local", client="codex", model="unknown"
    )
    state = app.state.service.transport_liveness_check(
        project["id"], joined["agent"]["id"]
    )
    assert state == "unknown"
