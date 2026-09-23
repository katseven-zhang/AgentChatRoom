from __future__ import annotations

import logging

from agentchatroom.presence import LocalPresenceManager


def test_same_identity_sessions_are_kept_alive_independently(service, project):
    first = service.join_room(
        project["id"],
        agent_key="trae-main",
        name="Trae",
        client="trae",
        model="unknown",
    )
    second = service.join_room(
        project["id"],
        agent_key="trae-main",
        name="Trae",
        client="trae",
        model="unknown",
    )
    manager = LocalPresenceManager(service, enabled=True, interval_seconds=60)

    manager.register(
        project["id"],
        first["agent"]["id"],
        first["token"],
        agent_key="trae-main",
    )
    manager.register(
        project["id"],
        second["agent"]["id"],
        second["token"],
        agent_key="trae-main",
    )

    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}
    assert agents[first["agent"]["id"]]["status"] == "online"
    assert agents[second["agent"]["id"]]["status"] == "online"

    manager.stop()
    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}
    assert agents[first["agent"]["id"]]["status"] == "offline"
    assert agents[second["agent"]["id"]]["status"] == "offline"


def test_recovered_and_new_same_identity_sessions_remain_independent(service, project):
    first = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )
    second = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )
    manager = LocalPresenceManager(service, enabled=True, interval_seconds=60)

    assert manager.ensure_registered(
        project["id"], first["agent"]["id"], first["token"]
    )
    manager.register(
        project["id"],
        second["agent"]["id"],
        second["token"],
        agent_key="codex-main",
    )

    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}
    assert agents[first["agent"]["id"]]["status"] == "online"
    assert agents[second["agent"]["id"]]["status"] == "online"

    manager.stop()


def test_transport_cleanup_stops_keepalive_without_closing_the_room_session(
    service, project
):
    """#117: 传输回收只停止保活；Room Session 保持可提交，显式离开才关闭它。"""
    first = service.join_room(
        project["id"], name="Codex", client="codex", model="unknown"
    )
    second = service.join_room(
        project["id"], name="Codex", client="codex", model="unknown"
    )
    live = {"transport-a", "transport-b"}
    manager = LocalPresenceManager(
        service,
        enabled=True,
        interval_seconds=60,
        transport_check=lambda key: key in live,
    )
    manager.register(
        project["id"], first["agent"]["id"], first["token"],
        agent_key=first["agent"]["agent_key"], transport_key="transport-a",
    )
    manager.register(
        project["id"], second["agent"]["id"], second["token"],
        agent_key=second["agent"]["agent_key"], transport_key="transport-b",
    )

    live.remove("transport-a")
    manager.heartbeat_once()
    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}

    # Reaping a transport no longer closes the Room Session: the client that
    # comes back on the same credential is adopted and must still submit.
    assert agents[first["agent"]["id"]]["status"] == "online"
    assert agents[first["agent"]["id"]]["left_at"] is None
    assert agents[second["agent"]["id"]]["status"] == "online"
    posted = service.post_message(
        project["id"],
        body="#117 keepalive stopped but the session stays open",
        session_id=first["agent"]["id"],
        token=first["token"],
        model_display_name="test",
    )
    assert posted["event_id"]

    # An explicit stop (session_leave / server shutdown) remains the only path
    # that closes sessions, and it only closes the ones still kept alive.
    manager.stop()
    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}
    assert agents[first["agent"]["id"]]["left_at"] is None
    assert agents[second["agent"]["id"]]["status"] == "offline"


def test_heartbeat_is_connection_liveness_not_manual_activity(service, project):
    joined = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )

    heartbeat = service.heartbeat(
        project["id"],
        joined["agent"]["id"],
        joined["token"],
    )
    identity = service.snapshot(project["id"])["agent_identities"][0]

    assert heartbeat["status"] == "online"
    assert identity["connection_status"] == "connected"
    assert identity["activity_status"] is None
    assert identity["status"] == "online"


def test_explicit_leave_racing_with_heartbeat_does_not_log_failure(
    service, project, caplog,
):
    joined = service.join_room(
        project["id"], name="Test Agent", client="test", model="unknown",
    )
    session_id = joined["agent"]["id"]
    manager = LocalPresenceManager(service, enabled=True, interval_seconds=60)
    manager.register(
        project["id"], session_id, joined["token"],
        agent_key=joined["agent"]["agent_key"],
    )

    # The heartbeat may have copied the registration just before the explicit
    # leave removed it. A closed session is expected, not an operator error.
    service.leave_session(project["id"], session_id, joined["token"])
    with caplog.at_level(logging.WARNING, logger="agentchatroom.presence"):
        manager.heartbeat_once()

    assert session_id not in manager._sessions
    assert not any(
        "Presence heartbeat failed" in record.getMessage()
        for record in caplog.records
    )
