"""Task #261: cancelling a task has to audit each invalidated row.

The release path emitted ``task.assignment_cancelled`` / ``task.handoff_cancelled``
per row, while cancellation only rewrote the rows and summarised them inside the
``task.cancelled`` payload - so the event-sourced timelines showed an assignment
disappearing on release but not on cancellation, and the individual invalidations
were not reconstructable.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import join_room_with_workspace


def _join(service, project, *, key, client, role="executor"):
    return join_room_with_workspace(
        service,
        project,
        agent_key=key,
        software_key=key,
        name=key,
        client=client,
        model="test-model",
        role=role,
    )


def _three(service, project):
    return (
        _join(service, project, key="cancel-actor", client="actor-cli"),
        _join(service, project, key="cancel-target", client="target-cli"),
        _join(service, project, key="cancel-reviewer", client="review-cli", role="reviewer"),
    )


def _pending_assignment_and_handoff(service, project, actor, target, reviewer):
    task = service.create_task(
        project["id"],
        title="Cancel with pending rows",
        acceptance_criteria=["Each invalidation is audited"],
        actor_session_id=actor["agent"]["id"],
        token=actor["token"],
    )["task"]
    service.claim_task(
        project["id"],
        task["id"],
        session_id=actor["agent"]["id"],
        token=actor["token"],
    )
    service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=target["agent"]["id"],
        note="pending assignment",
        management_authorized=True,
    )
    service.handoff_task(
        project["id"],
        task["id"],
        to_session_id=reviewer["agent"]["id"],
        from_session_id=actor["agent"]["id"],
        token=actor["token"],
        summary="pending handoff",
        next_step="review the work",
    )
    return task


def _cancel_events(service, project_id):
    with service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_type, payload_json FROM events
            WHERE project_id = ?
              AND event_type IN ('task.assignment_cancelled', 'task.handoff_cancelled')
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
    return [(row["event_type"], row["payload_json"]) for row in rows]


def _event_log(service, project_id):
    with service.database.connect() as connection:
        return [
            (row["id"], row["event_type"], row["payload_json"])
            for row in connection.execute(
                "SELECT id, event_type, payload_json FROM events WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]


@pytest.mark.parametrize("by", ["cancellation", "release"])
def test_both_paths_audit_every_invalidated_row(service, project, by):
    actor, target, reviewer = _three(service, project)
    task = _pending_assignment_and_handoff(
        service, project, actor, target, reviewer
    )

    if by == "cancellation":
        service.update_task(
            project["id"],
            task["id"],
            status="cancelled",
            session_id=actor["agent"]["id"],
            token=actor["token"],
        )
    else:
        service.release_task(
            project["id"],
            task["id"],
            reason_code="user_requested",
            reason="back to the pool",
            session_id=actor["agent"]["id"],
            token=actor["token"],
        )

    events = _cancel_events(service, project["id"])
    types = sorted(event_type for event_type, _ in events)
    assert types == ["task.assignment_cancelled", "task.handoff_cancelled"]
    if by == "cancellation":
        payloads = [json.loads(payload) for _, payload in events]
        assert {payload["by"] for payload in payloads} == {"task_cancellation"}
        assert all(payload["task_number"] == task["task_number"] for payload in payloads)


def test_cancellation_cannot_rewrite_existing_history(service, project):
    actor, target, reviewer = _three(service, project)
    task = _pending_assignment_and_handoff(
        service, project, actor, target, reviewer
    )
    before = _event_log(service, project["id"])

    service.update_task(
        project["id"],
        task["id"],
        status="cancelled",
        session_id=actor["agent"]["id"],
        token=actor["token"],
    )

    after = _event_log(service, project["id"])
    assert after[: len(before)] == before
    assert len(after) > len(before)


def test_management_cancellation_of_an_unowned_task_still_audits_rows(
    service, project
):
    actor, target, reviewer = _three(service, project)
    task = _pending_assignment_and_handoff(
        service, project, actor, target, reviewer
    )
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE tasks SET owner_session_id = NULL WHERE id = ?", (task["id"],)
        )

    service.update_task(
        project["id"],
        task["id"],
        status="cancelled",
        management_authorized=True,
    )

    payloads = [
        json.loads(payload)
        for _, payload in _cancel_events(service, project["id"])
    ]
    assert len(payloads) == 2
    assert {payload["by"] for payload in payloads} == {"task_cancellation"}
    assert all(payload["previous_target_session_id"] for payload in payloads)
