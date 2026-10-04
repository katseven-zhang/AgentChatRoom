"""#356: the receipt has to exist on all three ways into a task.

spec_receipt records which binding-document versions the work was held to. It
was only stamped by task.claimed, so a task that started through an assignment
or a reclaim reported {} for its whole life - and the history panel showed no
transition at all, because task_history.state_changes() reports nothing for an
event carrying only a new value.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _join(service, project_id, name):
    return service.join_room(
        project_id,
        software_key=name,
        name=name,
        client="pytest",
        model="unknown",
    )


def _binding(service, project_id, manager, *, key="standards", version=1):
    return service.upsert_project_document(
        project_id,
        doc_key=key,
        kind="binding",
        title=f"Standards v{version}",
        content=f"rules {version}",
        session_id=manager["agent"]["id"],
        token=manager["token"],
        management_authorized=True,
    )


def _task(service, project_id, owner, *, title="T", depends_on=None):
    return service.create_task(
        project_id,
        title=title,
        acceptance_criteria=["done"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
        depends_on=list(depends_on or []),
    )["task"]


def _assign(service, project_id, manager, target, task_id):
    return service.assign_task(
        project_id,
        task_id,
        assigned_by_session_id=manager["agent"]["id"],
        token=manager["token"],
        assigned_to_session_id=target["agent"]["id"],
        note="please take it",
    )


def test_a_task_started_by_assignment_acceptance_also_gets_a_receipt(
    service, project, joined_agents
):
    """The gap: accepting an assignment moved the task into claimed with no
    snapshot, so spec_receipt stayed empty forever."""
    manager, executor = joined_agents
    _binding(service, project["id"], manager)
    task = _task(service, project["id"], executor)

    assigned = _assign(service, project["id"], manager, executor, task["id"])
    acknowledged = service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assigned["assignment"]["id"],
        response="accepted",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    assert acknowledged["task"]["status"] == "claimed"

    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="done",
        files=["README.md"],
        tests=[{"command": "pytest", "exit_code": 0}],
        commit_hash="",
        no_code_change_reason="nothing to change",
    )
    listed = service.list_tasks(project["id"])
    receipt = next(
        row["spec_receipt"] for row in listed if row["id"] == task["id"]
    )
    assert receipt, "an assignment-started task still reports {} on the board"


def test_a_task_started_by_reclaim_also_gets_a_receipt(service, project, joined_agents):
    """The third door, same omission."""
    manager, executor = joined_agents
    _binding(service, project["id"], manager)
    task = _task(service, project["id"], executor)

    service.claim_task(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    service.release_task(
        project["id"],
        task["id"],
        reason_code="agent_unavailable",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )

    registered = service.register_workspace(
        project["id"],
        host_key="successor-host",
        host_name="SuccessorHost",
        local_path=project["root_path"],
    )
    successor = service.join_room(
        project["id"],
        software_key="successor",
        name="successor",
        client="pytest",
        model="unknown",
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    service.claim_task(
        project["id"],
        task["id"],
        session_id=successor["agent"]["id"],
        token=successor["token"],
        reclaim=True,
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=successor["agent"]["id"],
        token=successor["token"],
        summary="done",
        files=["README.md"],
        tests=[{"command": "pytest", "exit_code": 0}],
        commit_hash="",
        no_code_change_reason="nothing to change",
    )
    listed = service.list_tasks(project["id"])
    receipt = next(
        row["spec_receipt"] for row in listed if row["id"] == task["id"]
    )
    assert receipt, "a reclaim-started task still reports {} on the board"


def test_a_declined_assignment_does_not_stamp_an_empty_receipt(
    service, project, joined_agents
):
    """A refusal must not overwrite the receipt with an empty one.

    The lookup skips empty snapshots and keeps looking back, so declining
    something after claiming normally leaves the real receipt in place.
    """
    manager, executor = joined_agents
    _binding(service, project["id"], manager)
    task = _task(service, project["id"], executor)
    service.claim_task(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    assigned = _assign(service, project["id"], manager, executor, task["id"])
    service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assigned["assignment"]["id"],
        response="declined",
        note="not mine",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )

    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="done",
        files=["README.md"],
        tests=[{"command": "pytest", "exit_code": 0}],
        commit_hash="",
        no_code_change_reason="nothing to change",
    )
    listed = service.list_tasks(project["id"])
    receipt = next(
        row["spec_receipt"] for row in listed if row["id"] == task["id"]
    )
    assert receipt, "a decline erased the receipt taken at claim"


def test_the_verification_chain_records_transitions_not_just_end_states(
    service, project, joined_agents
):
    """task_history.state_changes() reports nothing without a from_/pair."""
    from agentchatroom.task_history import state_changes

    executor, reviewer = joined_agents
    task = _task(service, project["id"], executor, title="reviewable")
    service.claim_task(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="done",
        files=["README.md"],
        tests=[{"command": "pytest", "exit_code": 0}],
        commit_hash="",
        no_code_change_reason="nothing to change",
    )
    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[
            {
                "criterion": "done",
                "status": "passed",
            }
        ],
    )

    with service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_type, payload_json FROM events
            WHERE project_id = ? AND task_id = ?
              AND event_type IN ('work.reported', 'review.submitted',
                                 'task.integration_completed')
            ORDER BY id
            """,
            (project["id"], task["id"]),
        ).fetchall()

    import json

    kinds = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        kinds[row["event_type"]] = state_changes(payload)

    assert kinds["work.reported"], "work.reported shows no transition"
    assert kinds["review.submitted"], "review.submitted shows no transition"
    fields = {change["field"] for change in kinds["review.submitted"]}
    # verification_status always moves (pending -> approved or
    # changes_requested). execution_status only appears when it actually
    # changed, which state_changes is right to skip - asserting it would pin a
    # value that depends on whether the task was already execution-complete.
    assert "verification_status" in fields
    # And the pairs are real: a before that equals its after is not a change.
    for change in kinds["review.submitted"]:
        assert change["before"] != change["after"]


def test_integration_refuses_a_task_whose_dependency_regressed(
    service, project, joined_agents
):
    """The gate used to guard only entry into work.

    A dependency that is verified and integrated when the dependent task is
    approved, then reopened, leaves the dependent task looking ready on the
    strength of a stale approval.
    """
    from agentchatroom.services import AgentChatRoomService

    manager, executor = joined_agents
    reviewer = _join(service, project["id"], "rev2")
    integrator = _join(service, project["id"], "integrator")

    blocker = _task(service, project["id"], executor, title="blocker")
    dependent = _task(
        service, project["id"], executor, title="dependent", depends_on=[blocker["id"]]
    )

    for task_id, owner in ((blocker["id"], executor), (dependent["id"], executor)):
        service.claim_task(
            project["id"],
            task_id,
            session_id=owner["agent"]["id"],
            token=owner["token"],
        )
        service.submit_work_report(
            project["id"],
            task_id,
            session_id=owner["agent"]["id"],
            token=owner["token"],
            summary="done",
            files=["README.md"],
            tests=[{"command": "pytest", "exit_code": 0}],
            commit_hash="",
            no_code_change_reason="nothing to change",
        )
        service.submit_review(
            project["id"],
            task_id,
            reviewer_session_id=reviewer["agent"]["id"],
            token=reviewer["token"],
            verdict="approved",
            criteria=[{"criterion": "done", "status": "passed"}],
        )
        service.submit_integration(
            project["id"],
            task_id,
            integrator_session_id=integrator["agent"]["id"],
            token=integrator["token"],
            result="done",
            summary="integrated",
            files=["README.md"],
            tests=[{"command": "pytest", "exit_code": 0}],
        )

    # Reopen the blocker: it is no longer approved + integrated.
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            UPDATE tasks SET verification_status = 'not_required',
                             integration_status = 'pending'
            WHERE project_id = ? AND id = ?
            """,
            (project["id"], blocker["id"]),
        )
        # And send the dependent task back to work so integration is reachable.
        connection.execute(
            """
            UPDATE tasks SET status = 'awaiting_review',
                             verification_status = 'approved'
            WHERE project_id = ? AND id = ?
            """,
            (project["id"], dependent["id"]),
        )

    with pytest.raises(DomainError) as refused:
        service.submit_integration(
            project["id"],
            dependent["id"],
            integrator_session_id=integrator["agent"]["id"],
            token=integrator["token"],
            result="done",
            summary="integrated anyway",
            files=["README.md"],
            tests=[{"command": "pytest", "exit_code": 0}],
        )
    assert refused.value.code == "task_dependencies_incomplete"
    assert refused.value.status_code == 409
