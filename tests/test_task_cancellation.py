"""Task #105: user-facing task cancellation with resource safety.

Cancellation goes through the shared domain service (update_task with the
versioned status model). The terminal transition must release active
leases, cancel pending assignments and handoffs, keep the append-only
audit trail, and stay idempotent on repeated requests.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import TASK_TRANSITIONS


def _join_agent(service, project_id: str, key: str):
    return service.join_room(
        project_id,
        software_key=key,
        name=key.title(),
        client="pytest",
        model="unknown",
    )


def _active_lease_count(service, project_id: str, task_id: str) -> int:
    return sum(
        1
        for lease in service.list_leases(project_id)
        if lease["task_id"] == task_id and lease.get("released_at") is None
    )


def _make_task(service, project_id: str, title: str, actor):
    return service.create_task(
        project_id,
        title=title,
        acceptance_criteria=["Cancellation keeps history"],
        actor_session_id=actor["agent"]["id"],
        token=actor["token"],
    )["task"]


def test_cancel_cleans_up_leases_assignments_and_handoffs(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join_agent(service, project_id, "worker-a")
    other = _join_agent(service, project_id, "worker-b")
    task = _make_task(service, project_id, "Cancellable task", owner)

    service.claim_task(
        project_id, task["id"], owner["agent"]["id"], owner["token"]
    )
    service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/task-105.py",
        task_id=task["id"],
    )
    service.assign_task(
        project_id,
        task["id"],
        assigned_by_session_id=owner["agent"]["id"],
        token=owner["token"],
        assigned_to_member_id=other["agent"]["member_id"],
    )
    service.handoff_task(
        project_id,
        task["id"],
        to_session_id=other["agent"]["id"],
        summary="handing over",
        next_step="verify",
        from_session_id=owner["agent"]["id"],
        token=owner["token"],
    )

    service.update_task(project_id, task["id"], status="cancelled",
        management_authorized=True,
    )

    stored = service.get_task(project_id, task["id"])
    assert stored["execution_status"] == "cancelled"
    assert stored["owner_session_id"] is None
    assert _active_lease_count(service, project_id, task["id"]) == 0
    assignments = stored.get("assignments") or []
    assert all(item["status"] != "pending" for item in assignments)
    handoffs = stored.get("handoffs") or []
    assert all(item["status"] != "pending" for item in handoffs)
    audit_types = [
        event["event_type"]
        for event in service.query_audit(project_id, task_id=task["id"])["events"]
    ]
    assert audit_types.count("task.cancelled") == 1


def test_cancelled_task_cannot_be_claimed_or_recancelled_with_new_event(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join_agent(service, project_id, "worker-a")
    task = _make_task(service, project_id, "Terminal cancel", agent)

    service.update_task(project_id, task["id"], status="cancelled",
        management_authorized=True,
    )
    with pytest.raises(DomainError):
        service.claim_task(
            project_id, task["id"], agent["agent"]["id"], agent["token"]
        )

    before = service.query_audit(project_id, task_id=task["id"])["events"]
    cancel_count_before = sum(
        1 for event in before if event["event_type"] == "task.cancelled"
    )
    service.update_task(project_id, task["id"], status="cancelled",
        management_authorized=True,
    )
    after = service.query_audit(project_id, task_id=task["id"])["events"]
    cancel_count_after = sum(
        1 for event in after if event["event_type"] == "task.cancelled"
    )
    assert cancel_count_before == cancel_count_after == 1
    assert service.get_task(project_id, task["id"])["execution_status"] == "cancelled"


def test_done_task_rejects_cancellation_per_transitions():
    assert TASK_TRANSITIONS["done"] == {"done"}
    assert "cancelled" in TASK_TRANSITIONS["todo"]
    assert "cancelled" in TASK_TRANSITIONS["claimed"]
    assert "cancelled" in TASK_TRANSITIONS["in_progress"]
    assert "cancelled" in TASK_TRANSITIONS["blocked"]
    assert "cancelled" in TASK_TRANSITIONS["awaiting_review"]


CANCELLABLE_EXECUTION_STATES = ("claimed", "in_progress", "blocked")
REVIEW_STATES = ("awaiting_review", "verified")


def _force_state(service, project_id: str, task_id: str, state: str) -> None:
    """Park a task in a state the review machinery owns.

    Used only to set up the cancellation edge under test; the guards that
    normally produce these states are covered in test_services.py.
    """
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE tasks SET execution_status = ?, status = ? WHERE id = ? AND project_id = ?",
            (state, state, task_id, project_id),
        )


@pytest.mark.parametrize("state", CANCELLABLE_EXECUTION_STATES + REVIEW_STATES)
def test_every_edge_into_cancelled_emits_task_cancelled_and_cleans_up(
    service, project_dir, state
):
    """#232: the terminal event is chosen by the target state, not by the first
    incidental branch the transition also matches. blocked -> cancelled used to
    be recorded as task.unblocked, so no lease was released and the pending
    assignment/handoff stayed actionable."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join_agent(service, project_id, "worker-a")
    other = _join_agent(service, project_id, "worker-b")
    task = _make_task(service, project_id, f"Cancel from {state}", owner)

    service.claim_task(project_id, task["id"], owner["agent"]["id"], owner["token"])
    if state != "claimed":
        _force_state(service, project_id, task["id"], state)
    service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern=f"src/cancel-{state}.py",
        task_id=task["id"],
    )
    service.assign_task(
        project_id,
        task["id"],
        assigned_by_session_id=owner["agent"]["id"],
        token=owner["token"],
        assigned_to_member_id=other["agent"]["member_id"],
    )

    service.update_task(
        project_id, task["id"], status="cancelled", management_authorized=True
    )

    audit_types = [
        event["event_type"]
        for event in service.query_audit(project_id, task_id=task["id"])["events"]
    ]
    assert audit_types.count("task.cancelled") == 1, (state, audit_types)
    assert "task.unblocked" not in audit_types, (state, audit_types)

    stored = service.get_task(project_id, task["id"])
    assert stored["execution_status"] == "cancelled"
    assert _active_lease_count(service, project_id, task["id"]) == 0
    assert all(
        item["status"] != "pending" for item in (stored.get("assignments") or [])
    )


def test_plain_session_cannot_cancel_an_unowned_task(service, project_dir):
    """#234: an unowned task used to pass the ownership check for any Session,
    so an unrelated Agent could cancel it and release a lease another Agent had
    bound to that task."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join_agent(service, project_id, "worker-a")
    holder = _join_agent(service, project_id, "worker-b")
    intruder = _join_agent(service, project_id, "worker-c")
    task = _make_task(service, project_id, "Backlog without owner", author)
    assert task["owner_session_id"] is None

    service.acquire_lease(
        project_id,
        session_id=holder["agent"]["id"],
        token=holder["token"],
        path_pattern="src/lease-234.py",
        task_id=task["id"],
    )

    with pytest.raises(DomainError) as refused:
        service.update_task(
            project_id,
            task["id"],
            status="cancelled",
            session_id=intruder["agent"]["id"],
            token=intruder["token"],
        )
    assert refused.value.code == "management_auth_required"
    assert refused.value.details["required_action"]
    assert service.get_task(project_id, task["id"])["execution_status"] != "cancelled"
    assert _active_lease_count(service, project_id, task["id"]) == 1


def test_unowned_field_edit_is_still_allowed(service, project_dir):
    """#43 must not regress: only the terminal move is gated, not field edits on
    a task nobody owns yet."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join_agent(service, project_id, "worker-a")
    editor = _join_agent(service, project_id, "worker-b")
    task = _make_task(service, project_id, "Refine before claiming", author)

    service.update_task(
        project_id,
        task["id"],
        next_step="clarify the acceptance criteria",
        session_id=editor["agent"]["id"],
        token=editor["token"],
    )
    assert service.get_task(project_id, task["id"])["next_step"]


def test_owner_may_cancel_and_a_session_only_releases_its_own_leases(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join_agent(service, project_id, "worker-a")
    other = _join_agent(service, project_id, "worker-b")
    task = _make_task(service, project_id, "Owned by worker-a", owner)
    service.claim_task(project_id, task["id"], owner["agent"]["id"], owner["token"])
    service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/owner-234.py",
        task_id=task["id"],
    )
    foreign = service.acquire_lease(
        project_id,
        session_id=other["agent"]["id"],
        token=other["token"],
        path_pattern="src/other-host-scope.md",
    )

    service.update_task(
        project_id,
        task["id"],
        status="cancelled",
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    assert _active_lease_count(service, project_id, task["id"]) == 0
    assert foreign["lease"]["id"] in {
        lease["id"] for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    }


def test_cancelled_is_terminal_for_owner_and_management_alike(service, project_dir):
    """#346: cancelled is a terminal state; the silent cancelled -> todo hop is
    gone for every caller, and the transition table no longer offers the edge."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join_agent(service, project_id, "worker-a")
    task = _make_task(service, project_id, "Terminal", owner)
    service.claim_task(project_id, task["id"], owner["agent"]["id"], owner["token"])
    service.update_task(
        project_id,
        task["id"],
        status="cancelled",
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )

    with pytest.raises(DomainError) as by_owner:
        service.update_task(
            project_id,
            task["id"],
            status="todo",
            session_id=owner["agent"]["id"],
            token=owner["token"],
        )
    assert by_owner.value.code == "task_is_terminal"

    with pytest.raises(DomainError) as by_management:
        service.update_task(
            project_id, task["id"], status="todo", management_authorized=True
        )
    assert by_management.value.code == "task_is_terminal"
    assert service.get_task(project_id, task["id"])["execution_status"] == "cancelled"
    assert "todo" not in TASK_TRANSITIONS["cancelled"]
