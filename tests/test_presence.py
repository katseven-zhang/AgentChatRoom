from __future__ import annotations

from agentchatroom.presence import LocalPresenceManager


def test_new_same_identity_session_supersedes_process_owned_session(service, project):
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
    assert agents[first["agent"]["id"]]["status"] == "offline"
    assert agents[second["agent"]["id"]]["status"] == "online"

    manager.stop()
    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}
    assert agents[second["agent"]["id"]]["status"] == "offline"


def test_recovered_session_restores_identity_before_new_join(service, project):
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
    assert agents[first["agent"]["id"]]["status"] == "offline"
    assert agents[second["agent"]["id"]]["status"] == "online"

    manager.stop()


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
