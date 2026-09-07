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


def _make_task(service, project_id: str, title: str):
    return service.create_task(
        project_id,
        title=title,
        acceptance_criteria=["Cancellation keeps history"],
    )["task"]


def test_cancel_cleans_up_leases_assignments_and_handoffs(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join_agent(service, project_id, "worker-a")
    other = _join_agent(service, project_id, "worker-b")
    task = _make_task(service, project_id, "Cancellable task")

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

    service.update_task(project_id, task["id"], status="cancelled")

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
    task = _make_task(service, project_id, "Terminal cancel")

    service.update_task(project_id, task["id"], status="cancelled")
    with pytest.raises(DomainError):
        service.claim_task(
            project_id, task["id"], agent["agent"]["id"], agent["token"]
        )

    before = service.query_audit(project_id, task_id=task["id"])["events"]
    cancel_count_before = sum(
        1 for event in before if event["event_type"] == "task.cancelled"
    )
    service.update_task(project_id, task["id"], status="cancelled")
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
