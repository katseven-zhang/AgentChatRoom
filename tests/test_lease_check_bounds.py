"""Task #274: list inputs on the lease and handoff surfaces are bounded too.

``POST /leases/check`` normalised every supplied path and then scanned the active
leases while holding the write connection, and ``task_handoff`` wrote four
caller-supplied lists into an append-only event, with no length cap anywhere -
unlike every sibling input, which is capped by ``list_input_max_count``.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _claimed_task(service, project, executor):
    task = service.create_task(
        project["id"],
        title="Bounded lease and handoff input",
        acceptance_criteria=["Oversized lists are rejected"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    service.claim_task(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    return task


def _event_total(service, project_id):
    return len(service.list_events(project_id, after=0)["events"])


def test_lease_check_rejects_more_paths_than_the_bound(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    events_before = _event_total(service, project["id"])

    with pytest.raises(DomainError) as error:
        service.check_leases(
            project["id"],
            paths=[f"src/module_{index}.py" for index in range(101)],
        )

    assert error.value.code == "list_input_too_long"
    assert error.value.status_code == 422
    assert error.value.details["field"] == "paths"
    assert _event_total(service, project["id"]) == events_before


def test_lease_check_still_accepts_a_list_at_the_bound(service, project, joined_agents):
    result = service.check_leases(
        project["id"],
        paths=[f"src/module_{index}.py" for index in range(100)],
    )
    assert result["conflicts"] == []


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("completed_items", {"completed_items": [f"item {i}" for i in range(101)]}),
        ("pending_items", {"pending_items": [f"item {i}" for i in range(101)]}),
        ("risks", {"risks": [f"risk {i}" for i in range(101)]}),
        (
            "files",
            {"files": [f"src/module_{i}.py" for i in range(101)]},
        ),
    ],
)
def test_handoff_rejects_oversized_lists_without_a_write(
    service, project, joined_agents, field, payload
):
    executor, reviewer = joined_agents
    task = _claimed_task(service, project, executor)
    events_before = _event_total(service, project["id"])

    with pytest.raises(DomainError) as error:
        service.handoff_task(
            project["id"],
            task["id"],
            to_session_id=reviewer["agent"]["id"],
            from_session_id=executor["agent"]["id"],
            token=executor["token"],
            summary="Handing over the bounded path",
            next_step="Continue the review",
            **payload,
        )

    assert error.value.code == "list_input_too_long"
    assert error.value.details["field"] == field
    assert _event_total(service, project["id"]) == events_before
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM task_handoffs"
        ).fetchone()["total"] == 0


def test_handoff_still_accepts_lists_at_the_bound(service, project, joined_agents):
    executor, reviewer = joined_agents
    task = _claimed_task(service, project, executor)

    handed_off = service.handoff_task(
        project["id"],
        task["id"],
        to_session_id=reviewer["agent"]["id"],
        from_session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Handing over the bounded path",
        next_step="Continue the review",
        completed_items=[f"item {i}" for i in range(100)],
        files=[f"src/module_{i}.py" for i in range(100)],
    )

    assert handed_off["handoff"]["completed_items"]
