"""Task #218: joining never replaces a Session, so stop pretending it might.

`join_room` kept a hardcoded empty ``replacement`` dict, an event that could only
fire when that dict was non-empty, and a `replaced` key in every join response.
"""

from __future__ import annotations

from agentchatroom import gui_event_log


def _joins(service, project, tmp_path):
    root = str(tmp_path / "identity")
    (tmp_path / "identity").mkdir()
    host = service.register_workspace(
        project["id"], host_key="h", host_name="H", local_path=root
    )
    common = dict(
        name="Runtime Check",
        client="codex",
        model="unknown",
        role="reviewer",
        worktree=root,
        host_id=host["host"]["id"],
        workspace_id=host["workspace"]["id"],
    )
    first = service.join_room(
        project["id"], agent_key="runtime-check-main", **common
    )
    second = service.join_room(
        project["id"], agent_key="runtime-check-main", **common
    )
    return first, second


def test_join_response_no_longer_carries_the_inert_replacement(tmp_path, service, project):
    first, second = _joins(service, project, tmp_path)

    assert "replaced" not in first
    assert "replaced" not in second
    assert first["agent"]["id"] != second["agent"]["id"]


def test_no_session_replaced_event_is_ever_emitted(tmp_path, service, project):
    _joins(service, project, tmp_path)

    events = service.list_events(project["id"], after=0)["events"]
    assert [e["event_type"] for e in events].count("agent.session_replaced") == 0
    assert sum(1 for e in events if e["event_type"] == "agent.joined") == 2


def test_second_conversation_reports_the_identity_as_returning(tmp_path, service, project):
    _first, second = _joins(service, project, tmp_path)

    events = service.list_events(project["id"], after=0)["events"]
    joined = [
        event["payload"] for event in events if event["event_type"] == "agent.joined"
    ]

    assert [payload["reconnected"] for payload in joined] == [False, True]
    assert joined[0]["member_id"] == joined[1]["member_id"]


def test_parallel_join_keeps_the_first_session_alive(tmp_path, service, project):
    first, second = _joins(service, project, tmp_path)

    agents = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}

    assert first["agent"]["id"] in agents
    assert second["agent"]["id"] in agents
    assert agents[first["agent"]["id"]]["status"] != "offline"
    assert agents[first["agent"]["id"]]["left_at"] is None


def test_gui_reads_the_reconnect_hint_from_the_event_payload():
    event = {
        "event_type": "agent.joined",
        "project_seq": 7,
        "payload": {"name": "Reviewer", "reconnected": True},
    }

    line = gui_event_log.format_room_event(event, "Room")

    assert "重新接入" in line
    assert gui_event_log.format_room_event(
        {**event, "payload": {"name": "Reviewer", "reconnected": False}}, "Room"
    ).count("重新接入") == 0
