"""Task #266: task history must say which side actually holds events.

The page flags used to be `bool(items) and <count proxy>`, so a page walked to
the oldest event answered "nothing newer" while twelve newer events existed, and
an empty page fell back to cursor 0, which no client can page from.
"""

from __future__ import annotations

from agentchatroom import services as services_module

def _seed(service, project, session_id, token, tmp_path):
    other = service.create_task(
        project["id"],
        actor_session_id=session_id,
        token=token,
        title="Other",
        acceptance_criteria=["x"],
    )["task"]
    target = service.create_task(
        project["id"],
        actor_session_id=session_id,
        token=token,
        title="Target",
        acceptance_criteria=["x"],
    )["task"]

    def insert(task_id: str, count: int, tag: str) -> None:
        with service.database.connect(write=True) as connection:
            base = int(
                connection.execute(
                    "SELECT COALESCE(MAX(project_seq), 0) AS m FROM events"
                    " WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()["m"]
            )
            for index in range(count):
                connection.execute(
                    "INSERT INTO events (project_id, event_type, actor_session_id,"
                    " task_id, payload_json, project_seq, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project["id"],
                        f"probe.{tag}",
                        session_id,
                        task_id,
                        "{}",
                        base + 1 + index,
                        f"2026-01-01T00:00:{index:02d}+00:00",
                    ),
                )

    # Another task's events occupy the lower sequence numbers, so the target
    # task's oldest event is well above the project's first cursor position.
    insert(other["id"], 8, "other")
    insert(target["id"], 12, "target")
    return target

def _walk_older(service, project_id, task_id, page, limit=5, guard=12, event_type=""):
    visited = [event["event_id"] for event in page["items"]]
    steps = 0
    while page["has_more_before"] and steps < guard:
        page = service.list_task_history(
            project_id,
            task_id,
            before=page["next_before"],
            limit=limit,
            event_type=event_type,
        )
        steps += 1
        assert page["items"], "has_more_before promised a page and delivered nothing"
        visited += [event["event_id"] for event in page["items"]]
    return page, visited

def test_backward_walk_reaches_every_event_once(service, project, joined_agents, tmp_path):
    owner, _ = joined_agents
    target = _seed(service, project, owner["agent"]["id"], owner["token"], tmp_path)

    first = service.list_task_history(project["id"], target["id"], limit=5)
    oldest_page, visited = _walk_older(service, project["id"], target["id"], first)

    assert first["total"] == 13
    assert len(visited) == first["total"] == len(set(visited))
    assert not oldest_page["has_more_before"]

def test_oldest_page_still_offers_the_newer_direction(service, project, joined_agents, tmp_path):
    owner, _ = joined_agents
    target = _seed(service, project, owner["agent"]["id"], owner["token"], tmp_path)

    first = service.list_task_history(project["id"], target["id"], limit=5)
    oldest_page, _visited = _walk_older(service, project["id"], target["id"], first)

    assert oldest_page["items"], "the walk must end on a page that has rows"
    assert oldest_page["has_more_after"] is True

def test_empty_page_past_the_oldest_event_is_honest_and_navigable(
    service, project, joined_agents, tmp_path
):
    owner, _ = joined_agents
    target = _seed(service, project, owner["agent"]["id"], owner["token"], tmp_path)

    first = service.list_task_history(project["id"], target["id"], limit=5)
    oldest_page, _visited = _walk_older(service, project["id"], target["id"], first)
    beyond = service.list_task_history(
        project["id"], target["id"], before=oldest_page["next_before"], limit=5
    )
    forward = service.list_task_history(
        project["id"], target["id"], after=beyond["next_after"], limit=5
    )

    assert beyond["items"] == []
    assert beyond["has_more_before"] is False
    assert beyond["has_more_after"] is True
    assert beyond["next_after"] == oldest_page["next_before"] > 0
    assert forward["items"], "the cursor handed back must be usable"

def test_page_past_the_newest_event_stops_offering_newer(service, project, joined_agents, tmp_path):
    owner, _ = joined_agents
    target = _seed(service, project, owner["agent"]["id"], owner["token"], tmp_path)

    newest = service.list_task_history(project["id"], target["id"], limit=200)
    beyond = service.list_task_history(
        project["id"], target["id"], after=newest["next_after"], limit=5
    )

    assert beyond["items"] == []
    assert beyond["has_more_after"] is False
    assert beyond["has_more_before"] is True

def test_flags_respect_the_event_type_filter(service, project, joined_agents, tmp_path):
    owner, _ = joined_agents
    target = _seed(service, project, owner["agent"]["id"], owner["token"], tmp_path)

    only_tagged = service.list_task_history(
        project["id"], target["id"], limit=200, event_type="probe.target"
    )
    page = service.list_task_history(
        project["id"], target["id"], limit=5, event_type="probe.target"
    )
    page, visited = _walk_older(
        service, project["id"], target["id"], page, event_type="probe.target"
    )

    assert only_tagged["total"] == 12
    assert len(visited) == 12
    assert not page["has_more_before"]
    assert all(event["event_type"] == "probe.target" for event in page["items"])
