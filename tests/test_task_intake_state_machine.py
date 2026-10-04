"""Task #239: intake status changes are a declared state machine, not ad-hoc SQL.

``reassign_task_intake`` used to write ``status = 'pending'`` with no previous
status predicate and no rowcount check, so a concurrent acknowledge could be
silently overwritten, and ``_transition_task_intake`` - the shared guard the
contract exists to provide - had no callers at all. ``defining``, the claim state
``define_task_from_intake`` needs to break a define race, was not declared.
"""

from __future__ import annotations

import json

import pytest

from agentchatroom.contracts import TASK_INTAKE_STATUSES, TASK_INTAKE_TRANSITIONS
from agentchatroom.errors import DomainError


def _join(service, project, *, agent_key, name, client):
    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )
    return service.join_room(
        project["id"],
        agent_key=agent_key,
        name=name,
        client=client,
        software_key=agent_key,
        model="test-model",
        role="executor",
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )


def _three_identities(service, project):
    members = {}
    for key in ("creator", "target", "other"):
        members[key] = _join(
            service,
            project,
            agent_key=f"intake-{key}",
            name=key.title(),
            client=f"{key}-cli",
        )
    return members


def _submit(service, project, members):
    return service.submit_task_intake(
        project["id"],
        raw_description="Move through the declared intake states",
        target_member_id=members["target"]["agent"]["member_id"],
        created_by_session_id=members["creator"]["agent"]["id"],
        token=members["creator"]["token"],
    )["intake"]


def _intake_event_payloads(service, project_id):
    with service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_type, payload_json FROM events
            WHERE project_id = ? AND event_type LIKE 'task.intake%'
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
    import json

    return [
        (row["event_type"], json.loads(row["payload_json"])) for row in rows
    ]


def test_defining_is_a_declared_transaction_local_claim_state():
    assert "defining" in TASK_INTAKE_STATUSES
    assert TASK_INTAKE_TRANSITIONS["accepted"] >= {"defining", "pending"}
    assert TASK_INTAKE_TRANSITIONS["defining"] == {"defining", "defined"}
    assert "pending" not in TASK_INTAKE_TRANSITIONS["defined"]
    assert "pending" not in TASK_INTAKE_TRANSITIONS["cancelled"]


def test_reassign_of_an_accepted_intake_is_declared_and_audited(
    service, project
):
    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=members["target"]["agent"]["id"],
        token=members["target"]["token"],
        response="accepted",
    )

    reassigned = service.reassign_task_intake(
        project["id"],
        intake["id"],
        target_member_id=members["other"]["agent"]["member_id"],
        note="handing the intake to another identity",
        management_authorized=True,
    )["intake"]

    assert reassigned["status"] == "pending"
    assert reassigned["target_member_id"] == members["other"]["agent"]["member_id"]
    events = _intake_event_payloads(service, project["id"])
    reassign_events = [
        payload for name, payload in events if name == "task.intake_reassigned"
    ]
    assert reassign_events[-1]["previous_status"] == "accepted"
    assert reassign_events[-1]["status"] == "pending"


@pytest.mark.parametrize("seed_status", ["defined", "cancelled", "defining"])
def test_reassign_refuses_states_the_contract_does_not_allow(
    service, project, seed_status
):
    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE task_intakes SET status = ? WHERE id = ?",
            (seed_status, intake["id"]),
        )

    with pytest.raises(DomainError) as error:
        service.reassign_task_intake(
            project["id"],
            intake["id"],
            target_member_id=members["other"]["agent"]["member_id"],
            management_authorized=True,
        )

    assert error.value.code in {
        "task_intake_already_defined",
        "task_intake_cancelled",
        "invalid_task_intake_transition",
    }
    assert service.get_task_intake(project["id"], intake["id"])["status"] == (
        seed_status
    )


def test_reassign_requires_the_row_to_still_be_the_one_it_read(
    service, project, monkeypatch
):
    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    stale = dict(intake)
    assert stale["status"] == "pending"

    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE task_intakes SET status = 'accepted' WHERE id = ?",
            (intake["id"],),
        )

    def _always_stale(connection, project_id, intake_id):
        return stale

    monkeypatch.setattr(service, "_require_task_intake", _always_stale)

    with pytest.raises(DomainError) as error:
        service.reassign_task_intake(
            project["id"],
            intake["id"],
            target_member_id=members["other"]["agent"]["member_id"],
            management_authorized=True,
        )

    assert error.value.code == "task_modified_concurrently"
    with service.database.connect() as connection:
        stored = connection.execute(
            "SELECT status FROM task_intakes WHERE id = ?", (intake["id"],)
        ).fetchone()["status"]
    assert stored == "accepted"


def test_a_failed_define_does_not_leave_the_intake_defining(
    service, project
):
    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=members["target"]["agent"]["id"],
        token=members["target"]["token"],
        response="accepted",
    )

    with pytest.raises(DomainError):
        service.define_task_from_intake(
            project["id"],
            intake["id"],
            session_id=members["target"]["agent"]["id"],
            token=members["target"]["token"],
            title="",
        )

    assert service.get_task_intake(project["id"], intake["id"])["status"] == (
        "accepted"
    )


def test_define_still_completes_the_declared_walk(service, project):
    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=members["target"]["agent"]["id"],
        token=members["target"]["token"],
        response="accepted",
    )

    defined = service.define_task_from_intake(
        project["id"],
        intake["id"],
        session_id=members["target"]["agent"]["id"],
        token=members["target"]["token"],
        title="Formal task from intake",
        acceptance_criteria=["Intake reaches defined"],
    )

    assert defined["intake"]["status"] == "defined"
    assert defined["intake"]["formal_task_id"] == defined["task"]["id"]
