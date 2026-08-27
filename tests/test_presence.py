from __future__ import annotations

from agentchatroom.presence import LocalPresenceManager


def test_local_presence_keeps_status_without_advancing_room_cursor(service, project):
    joined = service.join_room(
        project["id"],
        agent_key="presence-main",
        name="Presence Agent",
        client="generic",
        model="unknown",
    )
    with service.database.connect() as connection:
        cursor = service.latest_cursor(connection, project["id"])

    manager = LocalPresenceManager(service, enabled=True, interval_seconds=1)
    manager.register(
        project["id"],
        joined["agent"]["id"],
        joined["token"],
        status="working",
    )
    manager.heartbeat_once()

    snapshot = service.snapshot(project["id"])
    agent = next(item for item in snapshot["agents"] if item["id"] == joined["agent"]["id"])
    assert agent["status"] == "working"
    with service.database.connect() as connection:
        assert service.latest_cursor(connection, project["id"]) == cursor


def test_ensure_registered_recovers_presence_after_process_restart(service, project):
    joined = service.join_room(
        project["id"],
        agent_key="presence-recovery",
        name="Recovery Agent",
        client="generic",
        model="unknown",
    )
    session_id = joined["agent"]["id"]
    token = joined["token"]

    # First process registers presence, then "restarts" (registry lost).
    first = LocalPresenceManager(service, enabled=True, interval_seconds=1)
    first.register(project["id"], session_id, token, status="idle")
    first.heartbeat_once()
    restarted = LocalPresenceManager(service, enabled=True, interval_seconds=1)

    # After restart, a successful authenticated room_sync proves liveness.
    assert restarted.ensure_registered(project["id"], session_id, token) is True
    # Idempotent: repeated calls do not duplicate registration.
    assert restarted.ensure_registered(project["id"], session_id, token) is False
    restarted.heartbeat_once()

    snapshot = service.snapshot(project["id"])
    agent = next(item for item in snapshot["agents"] if item["id"] == session_id)
    assert agent["status"] != "offline"


def test_ensure_registered_rejects_invalid_session_via_heartbeat(service, project):
    joined = service.join_room(
        project["id"],
        agent_key="presence-invalid",
        name="Invalid Agent",
        client="generic",
        model="unknown",
    )
    manager = LocalPresenceManager(service, enabled=True, interval_seconds=1)
    # Bogus token registers eagerly but self-heals on the next heartbeat.
    assert manager.ensure_registered(project["id"], joined["agent"]["id"], "bad-token") is True
    manager.heartbeat_once()
    manager.heartbeat_once()
    # The session was unregistered after the first failed heartbeat.
    with manager._lock:
        assert joined["agent"]["id"] not in manager._sessions
