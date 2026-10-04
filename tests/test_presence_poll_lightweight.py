"""Task #264: a presence poll must not rebuild the whole Room projection.

The GUI's ten-second watcher called `snapshot()` per project just to read four
Session fields, paying for event pages, tasks with relations, reports, reviews,
acknowledgements, leases, knowledge assets, and a per-Session unread count.
"""

from __future__ import annotations

import threading
from queue import Queue

import pytest

from agentchatroom.gui_event_log import RoomEventTail

ANCIENT = "2000-01-01T00:00:00+00:00"


def _age_session(service, session_id):
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
            (ANCIENT, session_id),
        )
        connection.execute(
            "UPDATE events SET created_at = ? WHERE actor_session_id = ?",
            (ANCIENT, session_id),
        )


def test_presence_query_returns_only_the_presence_fields(service, project, joined_agents):
    owner, reviewer = joined_agents

    presence = service.list_session_presence(project["id"])

    assert {item["id"] for item in presence} == {owner["agent"]["id"], reviewer["agent"]["id"]}
    for item in presence:
        assert set(item) == {"id", "name", "status", "left_at"}


def test_presence_query_agrees_with_the_snapshot_projection(service, project, joined_agents):
    owner, _reviewer = joined_agents
    _age_session(service, owner["agent"]["id"])

    presence = {
        item["id"]: item for item in service.list_session_presence(project["id"])
    }
    snapshot = {
        agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]
    }

    for session_id, item in presence.items():
        assert item["status"] == snapshot[session_id]["status"]
        assert item["name"] == snapshot[session_id]["name"]
        assert item["left_at"] == snapshot[session_id]["left_at"]


def test_presence_poll_never_touches_the_snapshot_projection(
    settings, service, project, joined_agents
):
    owner, _reviewer = joined_agents
    sink: Queue = Queue()
    tail = RoomEventTail(settings, sink, threading.Event(), service=service)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("presence polling rebuilt the Room projection")

    service.snapshot = forbidden  # type: ignore[method-assign]

    tail.poll_presence_once()

    assert tail.presence[project["id"]][owner["agent"]["id"]] == "online"
    assert sink.empty()


def test_the_offline_reminder_still_fires_without_a_snapshot(
    settings, service, project, joined_agents
):
    owner, _reviewer = joined_agents
    sink: Queue = Queue()
    tail = RoomEventTail(settings, sink, threading.Event(), service=service)

    tail.poll_presence_once()
    _age_session(service, owner["agent"]["id"])
    assert tail.presence[project["id"]][owner["agent"]["id"]] == "online"

    service.snapshot = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
        AssertionError("presence polling rebuilt the Room projection")
    )
    tail.poll_presence_once()
    lines = []
    while not sink.empty():
        lines.append(sink.get()[1])

    assert any("心跳/活动超时" in line for line in lines), lines
    assert tail.presence[project["id"]][owner["agent"]["id"]] == "offline"


def test_a_deliberate_leave_does_not_produce_a_timeout_reminder(
    settings, service, project, joined_agents
):
    owner, _reviewer = joined_agents
    sink: Queue = Queue()
    tail = RoomEventTail(settings, sink, threading.Event(), service=service)

    tail.poll_presence_once()
    service.leave_session(project["id"], owner["agent"]["id"], owner["token"])
    _age_session(service, owner["agent"]["id"])

    tail.poll_presence_once()
    lines = []
    while not sink.empty():
        lines.append(sink.get()[1])

    assert not any("心跳/活动超时" in line for line in lines), lines
    assert tail.presence[project["id"]][owner["agent"]["id"]] == "offline"
