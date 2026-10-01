"""Task #233: accepting an assignment is not a licence to reclaim someone's task.

The assignment path used a weaker rule than ``task_claim``: any Session could take
an ``in_progress`` + ``changes_requested`` task as soon as the owner's
``left_at`` was set, with no same-identity check, no heartbeat/transport evidence,
and no lease transfer - so the new owner was then blocked by the departed owner's
exclusive leases on its own task.
"""

from __future__ import annotations

import json

import pytest

from agentchatroom.errors import DomainError
from tests.conftest import join_room_with_workspace


def _join(service, project, *, key, client, name=None, role="executor"):
    return join_room_with_workspace(
        service,
        project,
        agent_key=key,
        software_key=key,
        name=name or key,
        client=client,
        model="test-model",
        role=role,
    )


def _in_rework_task(service, project, owner):
    task = service.create_task(
        project["id"],
        title="Assignment must not steal ownership",
        acceptance_criteria=["Reclaim follows the task_claim contract"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    service.claim_task(
        project["id"],
        task["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            UPDATE tasks
            SET status = 'in_progress', execution_status = 'in_progress',
                verification_status = 'changes_requested'
            WHERE id = ? AND project_id = ?
            """,
            (task["id"], project["id"]),
        )
    return task


def _disconnect(service, session_id):
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET left_at = ?, token_expires_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", "2000-01-01T00:00:00Z", session_id),
        )


def test_a_different_identity_cannot_take_the_task_through_an_assignment(
    service, project
):
    owner = _join(service, project, key="owner-identity", client="codex-cli")
    stranger = _join(service, project, key="stranger-identity", client="other-cli")
    task = _in_rework_task(service, project, owner)
    assignment = service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=stranger["agent"]["id"],
        management_authorized=True,
    )["assignment"]
    _disconnect(service, owner["agent"]["id"])

    with pytest.raises(DomainError) as error:
        service.acknowledge_task_assignment(
            project["id"],
            task["id"],
            assignment["id"],
            session_id=stranger["agent"]["id"],
            token=stranger["token"],
            response="accepted",
        )

    assert error.value.code == "task_reclaim_forbidden"
    reloaded = service.get_task(project["id"], task["id"])
    assert reloaded["owner_session_id"] == owner["agent"]["id"]


def test_a_live_owner_cannot_be_preempted_by_an_assignment_even_same_identity(
    service, project
):
    owner = _join(service, project, key="owner-identity", client="codex-cli")
    sibling = _join(service, project, key="owner-identity", client="codex-cli")
    task = _in_rework_task(service, project, owner)
    assignment = service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=sibling["agent"]["id"],
        management_authorized=True,
    )["assignment"]

    with pytest.raises(DomainError) as error:
        service.acknowledge_task_assignment(
            project["id"],
            task["id"],
            assignment["id"],
            session_id=sibling["agent"]["id"],
            token=sibling["token"],
            response="accepted",
        )

    assert error.value.code == "task_owner_session_connected"
    assert error.value.details["required_action"] == (
        "task_claim(takeover=true)_by_owner_identity"
    )


@pytest.mark.parametrize("execution_status", ["claimed", "blocked"])
def test_assignment_reclaims_only_the_rework_state(
    service, project, execution_status
):
    owner = _join(service, project, key="owner-identity", client="codex-cli")
    sibling = _join(service, project, key="owner-identity", client="codex-cli")
    task = _in_rework_task(service, project, owner)
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE tasks SET execution_status = ? WHERE id = ?",
            (execution_status, task["id"]),
        )
    assignment = service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=sibling["agent"]["id"],
        management_authorized=True,
    )["assignment"]
    _disconnect(service, owner["agent"]["id"])

    with pytest.raises(DomainError) as error:
        service.acknowledge_task_assignment(
            project["id"],
            task["id"],
            assignment["id"],
            session_id=sibling["agent"]["id"],
            token=sibling["token"],
            response="accepted",
        )

    assert error.value.code == "task_already_claimed"


def test_a_same_identity_reclaim_moves_the_departed_owner_leases(
    service, project
):
    owner = _join(service, project, key="owner-identity", client="codex-cli")
    sibling = _join(service, project, key="owner-identity", client="codex-cli")
    task = _in_rework_task(service, project, owner)
    lease = service.acquire_lease(
        project["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/owner_only.py",
        mode="exclusive",
        task_id=task["id"],
        reason="rework in flight",
    )["lease"]
    assignment = service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=sibling["agent"]["id"],
        management_authorized=True,
    )["assignment"]
    _disconnect(service, owner["agent"]["id"])

    acknowledged = service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assignment["id"],
        session_id=sibling["agent"]["id"],
        token=sibling["token"],
        response="accepted",
    )

    assert acknowledged["task"]["owner_session_id"] == sibling["agent"]["id"]
    with service.database.connect() as connection:
        stored = connection.execute(
            "SELECT session_id FROM file_leases WHERE id = ?", (lease["id"],)
        ).fetchone()
    assert stored["session_id"] == sibling["agent"]["id"]
    with service.database.connect() as connection:
        event = connection.execute(
            """
            SELECT payload_json FROM events
            WHERE event_type = 'task.assignment_acknowledged'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    payload = json.loads(event["payload_json"])
    assert payload["reclaimed_from_session_id"] == owner["agent"]["id"]
    assert payload["transferred_lease_ids"] == [lease["id"]]

    # The new owner can immediately work its own file range: the departed
    # owner's exclusive lease came with the task instead of blocking it.
    try:
        service.acquire_lease(
            project["id"],
            session_id=sibling["agent"]["id"],
            token=sibling["token"],
            path_pattern="src/owner_only.py",
            mode="exclusive",
            task_id=task["id"],
            reason="continue the rework",
        )
    except DomainError as error:
        assert error.code != "lease_conflict", error.as_dict()
