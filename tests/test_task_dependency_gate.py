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
