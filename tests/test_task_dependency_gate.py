"""Task #342: the dependency gate must guard every way into work.

``claim_task`` refused to claim a task whose dependencies were not
verified-and-integrated, but assignment acceptance and handoff acceptance moved
the same task into ``claimed`` without asking, so the gate could be walked around
by the door you chose rather than by the state of the work.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


def _task(service, project_id: str, actor, title, **kwargs):
    return service.create_task(
        project_id,
        title=title,
        acceptance_criteria=[f"{title} is verified"],
        actor_session_id=actor["agent"]["id"],
        token=actor["token"],
        **kwargs,
    )["task"]


def _blocked_setup(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    worker = _join(service, project_id, "worker-b")
    dependency = _task(service, project_id, coordinator, "Upstream dependency")
    blocked = _task(
        service,
        project_id,
        coordinator,
        "Downstream work",
        depends_on=[dependency["id"]],
    )
    return project_id, coordinator, worker, dependency, blocked


def test_assignment_acceptance_cannot_walk_past_the_dependency_gate(
    service, project_dir
):
    project_id, coordinator, worker, dependency, blocked = _blocked_setup(
        service, project_dir
    )
    assignment = service.assign_task(
        project_id,
        blocked["id"],
        assigned_by_session_id=coordinator["agent"]["id"],
        token=coordinator["token"],
        assigned_to_member_id=worker["agent"]["member_id"],
    )["assignment"]

    with pytest.raises(DomainError) as refused:
        service.acknowledge_task_assignment(
            project_id,
            blocked["id"],
            assignment["id"],
            session_id=worker["agent"]["id"],
            token=worker["token"],
            response="accepted",
            note="taking it now",
        )
    assert refused.value.code == "task_dependencies_incomplete"
    assert [
        blocker["id"] for blocker in refused.value.details["blockers"]
    ] == [dependency["id"]]
    stored = service.get_task(project_id, blocked["id"])
    assert stored["owner_session_id"] is None
    assert stored["execution_status"] == "todo"


def test_handoff_acceptance_cannot_walk_past_the_dependency_gate(
    service, project_dir
):
    project_id, coordinator, worker, dependency, blocked = _blocked_setup(
        service, project_dir
    )
    # Own the task first, then discover a dependency: claiming it up front is
    # impossible now, so park the ownership the way a mid-flight dependency edit
    # would leave it, and check the accepting side refuses.
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            UPDATE tasks SET owner_session_id = ?, status = 'claimed',
                execution_status = 'claimed'
            WHERE id = ? AND project_id = ?
            """,
            (coordinator["agent"]["id"], blocked["id"], project_id),
        )
    handoff = service.handoff_task(
        project_id,
        blocked["id"],
        to_session_id=worker["agent"]["id"],
        summary="handing over despite the dependency",
        next_step="continue",
        from_session_id=coordinator["agent"]["id"],
        token=coordinator["token"],
    )["handoff"]

    with pytest.raises(DomainError) as refused:
        service.acknowledge_task_handoff(
            project_id,
            blocked["id"],
            handoff["id"],
            session_id=worker["agent"]["id"],
            token=worker["token"],
            response="accepted",
            note="taking over",
        )
    assert refused.value.code == "task_dependencies_incomplete"
    assert service.get_task(project_id, blocked["id"])["owner_session_id"] == (
        coordinator["agent"]["id"]
    )


def test_claim_still_uses_the_same_gate(service, project_dir):
    project_id, coordinator, worker, dependency, blocked = _blocked_setup(
        service, project_dir
    )

    with pytest.raises(DomainError) as refused:
        service.claim_task(
            project_id, blocked["id"], worker["agent"]["id"], worker["token"]
        )
    assert refused.value.code == "task_dependencies_incomplete"


def _cancel(service, project_id: str, task, actor):
    # An unowned backlog task is cancelled through the management surface, which
    # is the rule #234 introduced.
    service.update_task(
        project_id,
        task["id"],
        status="cancelled",
        management_authorized=True,
    )
    return service.get_task(project_id, task["id"])


def test_a_cancelled_task_cannot_be_wired_in_as_a_dependency(service, project_dir):
    """#222: cancelled is terminal, so such a dependency can never satisfy the
    approved+integrated gate - the new task would be unclaimable forever."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    dead = _task(service, project_id, coordinator, "Cancelled upstream")
    _cancel(service, project_id, dead, coordinator)

    with pytest.raises(DomainError) as refused:
        service.create_task(
            project_id,
            title="Downstream of a dead end",
            acceptance_criteria=["Cannot depend on a cancelled task"],
            depends_on=[dead["id"]],
            actor_session_id=coordinator["agent"]["id"],
            token=coordinator["token"],
        )
    assert refused.value.code == "invalid_dependency_status"
    assert refused.value.details["execution_status"] == "cancelled"
    assert refused.value.details["task_number"] == dead["task_number"]
    assert refused.value.details["required_action"]

    live = _task(service, project_id, coordinator, "Live upstream")
    created = service.create_task(
        project_id,
        title="Downstream of a live task",
        acceptance_criteria=["Ordinary dependencies still work"],
        depends_on=[live["id"]],
        actor_session_id=coordinator["agent"]["id"],
        token=coordinator["token"],
    )["task"]
    assert created["depends_on"] == [live["id"]]


def test_dependency_edits_reject_a_cancelled_target_too(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    upstream = _task(service, project_id, coordinator, "Upstream to cancel")
    downstream = _task(service, project_id, coordinator, "Downstream")
    _cancel(service, project_id, upstream, coordinator)

    with pytest.raises(DomainError) as refused:
        service.update_task(
            project_id,
            downstream["id"],
            depends_on=[upstream["id"]],
            session_id=coordinator["agent"]["id"],
            token=coordinator["token"],
        )
    assert refused.value.code == "invalid_dependency_status"
