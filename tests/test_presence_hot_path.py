from __future__ import annotations

from contextvars import ContextVar

from agentchatroom import mcp_server
from agentchatroom.bootstrap import RuntimeBinding
from agentchatroom.presence import LocalPresenceManager


class CountingRoomService:
    """Wrap the real service and count the two Room reads presence may perform.

    ``snapshot`` rebuilds the whole board; ``session_agent_key`` reads the one
    Session row. The regression is that a hot call must not use either.
    """

    def __init__(self, room_service):
        self.room_service = room_service
        self.board_reads = 0
        self.key_lookups = 0

    def __getattr__(self, name):
        return getattr(self.room_service, name)

    def snapshot(self, *args, **kwargs):
        self.board_reads += 1
        return self.room_service.snapshot(*args, **kwargs)

    def session_agent_key(self, *args, **kwargs):
        self.key_lookups += 1
        return self.room_service.session_agent_key(*args, **kwargs)


class RecordingManager:
    """Stand in for the presence manager and record what the MCP layer passes."""

    def __init__(self):
        self.registrations = []

    def ensure_registered(
        self, project_id, session_id, token, *, agent_key="", transport_key=""
    ):
        self.registrations.append(
            {"session_id": session_id, "agent_key": agent_key}
        )
        return True


def _join(service, project):
    return service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )


def _bind_session(mcp_module, monkeypatch, project, session_id, *, agent_key):
    monkeypatch.setattr(mcp_module, "_session_bindings", {})
    monkeypatch.setattr(
        mcp_module, "_runtime_binding", ContextVar("test-binding", default=None)
    )
    mcp_module.set_runtime_binding(
        RuntimeBinding(
            project_id=project["id"],
            session_id=session_id,
            token="token-under-test",
            cursor=0,
            software_key=agent_key,
            agent_key=agent_key,
            conversation_synced=True,
        )
    )


def test_registered_session_is_rejected_before_any_room_read(service, project):
    joined = _join(service, project)
    counting = CountingRoomService(service)
    manager = LocalPresenceManager(counting, enabled=True, interval_seconds=60)
    manager.register(
        project["id"],
        joined["agent"]["id"],
        joined["token"],
        agent_key=joined["agent"]["agent_key"],
    )

    for _ in range(5):
        assert (
            manager.ensure_registered(
                project["id"], joined["agent"]["id"], joined["token"]
            )
            is False
        )

    assert (counting.board_reads, counting.key_lookups) == (0, 0)


def test_cold_registration_resolves_identity_from_the_session_row(service, project):
    joined = _join(service, project)
    counting = CountingRoomService(service)
    manager = LocalPresenceManager(counting, enabled=True, interval_seconds=60)

    assert (
        manager.ensure_registered(
            project["id"], joined["agent"]["id"], joined["token"]
        )
        is True
    )

    assert counting.board_reads == 0
    assert counting.key_lookups == 1
    assert manager._sessions[joined["agent"]["id"]].agent_key == joined["agent"]["agent_key"]


def test_cold_registration_with_a_known_identity_touches_no_database(service, project):
    joined = _join(service, project)
    counting = CountingRoomService(service)
    manager = LocalPresenceManager(counting, enabled=True, interval_seconds=60)

    assert (
        manager.ensure_registered(
            project["id"],
            joined["agent"]["id"],
            joined["token"],
            agent_key=joined["agent"]["agent_key"],
        )
        is True
    )

    assert (counting.board_reads, counting.key_lookups) == (0, 0)


def test_unknown_session_is_not_registered_without_a_board_read(service, project):
    counting = CountingRoomService(service)
    manager = LocalPresenceManager(counting, enabled=True, interval_seconds=60)

    assert (
        manager.ensure_registered(project["id"], "agent-missing", "token-missing")
        is False
    )

    assert counting.board_reads == 0


def test_hot_mcp_calls_hand_presence_the_bound_identity(service, project, monkeypatch):
    joined = _join(service, project)
    manager = RecordingManager()
    monkeypatch.setattr(mcp_server, "presence_manager", manager)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "_authorize_remote", lambda *args, **kwargs: None)
    _bind_session(
        mcp_server,
        monkeypatch,
        project,
        joined["agent"]["id"],
        agent_key=joined["agent"]["agent_key"],
    )

    assert mcp_server.room_sync(
        project["id"], session_id=joined["agent"]["id"], token=joined["token"]
    )["ok"] is True
    assert mcp_server.session_heartbeat(
        project["id"], joined["agent"]["id"], joined["token"]
    )["ok"] is True
    assert mcp_server.message_post(
        project["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        body="#341 presence must not rebuild the board",
        model_display_name="unknown",
    )["ok"] is True

    assert [item["agent_key"] for item in manager.registrations] == [
        joined["agent"]["agent_key"]
    ] * 3


def test_presence_falls_back_when_the_binding_is_another_session(
    service, project, monkeypatch
):
    joined = _join(service, project)
    other = _join(service, project)
    manager = RecordingManager()
    monkeypatch.setattr(mcp_server, "presence_manager", manager)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)
    monkeypatch.setattr(mcp_server, "_authorize_remote", lambda *args, **kwargs: None)
    _bind_session(
        mcp_server,
        monkeypatch,
        project,
        other["agent"]["id"],
        agent_key=other["agent"]["agent_key"],
    )

    assert mcp_server.session_heartbeat(
        project["id"], joined["agent"]["id"], joined["token"]
    )["ok"] is True

    assert manager.registrations == [{"session_id": joined["agent"]["id"], "agent_key": ""}]


def test_explicit_leave_unregisters_the_manager_bound_to_the_transport(
    service, project, monkeypatch
):
    """#283: HTTP 形态下保活注册表是按 transport 绑定的，全局那个根本不存在。"""
    joined = _join(service, project)
    bound = LocalPresenceManager(service, enabled=True, interval_seconds=60)
    bound.register(
        project["id"],
        joined["agent"]["id"],
        joined["token"],
        agent_key=joined["agent"]["agent_key"],
    )
    monkeypatch.setattr(mcp_server, "presence_manager", None)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)
    presence_token = mcp_server._bound_presence_manager.set(bound)
    try:
        assert mcp_server.session_leave(
            project["id"], joined["agent"]["id"], joined["token"]
        )["ok"] is True
    finally:
        mcp_server._bound_presence_manager.reset(presence_token)

    assert joined["agent"]["id"] not in bound._sessions


def test_explicit_leave_unregisters_the_stdio_manager(service, project, monkeypatch):
    joined = _join(service, project)
    manager = LocalPresenceManager(service, enabled=True, interval_seconds=60)
    manager.register(
        project["id"],
        joined["agent"]["id"],
        joined["token"],
        agent_key=joined["agent"]["agent_key"],
    )
    monkeypatch.setattr(mcp_server, "presence_manager", manager)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    assert mcp_server.session_leave(
        project["id"], joined["agent"]["id"], joined["token"]
    )["ok"] is True

    assert joined["agent"]["id"] not in manager._sessions


def test_repeated_heartbeat_keeps_the_session_alive_without_room_reads(
    service, project, monkeypatch
):
    joined = _join(service, project)
    counting = CountingRoomService(service)
    manager = LocalPresenceManager(counting, enabled=True, interval_seconds=60)
    monkeypatch.setattr(mcp_server, "presence_manager", manager)
    monkeypatch.setattr(mcp_server, "get_service", lambda: counting)
    monkeypatch.setattr(mcp_server, "_authorize_remote", lambda *args, **kwargs: None)
    _bind_session(
        mcp_server,
        monkeypatch,
        project,
        joined["agent"]["id"],
        agent_key=joined["agent"]["agent_key"],
    )

    for _ in range(10):
        assert mcp_server.session_heartbeat(
            project["id"], joined["agent"]["id"], joined["token"]
        )["ok"] is True

    assert (counting.board_reads, counting.key_lookups) == (0, 0)
    manager.heartbeat_once()
    agents = {
        agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]
    }
    assert agents[joined["agent"]["id"]]["status"] == "online"
