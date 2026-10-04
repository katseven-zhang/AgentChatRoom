"""Task #231: an integration verdict survives a field edit.

``task_state_for_legacy_status`` returned ``integration_status='pending'`` for
every non-terminal legacy status, and ``update_task`` re-derived the three axes
on each call - so appending a ``next_step`` to a task whose integration had
*failed* silently wiped the failure from the task's current state, leaving it to
be rediscovered by whoever came next.
"""

from __future__ import annotations

import pytest

from agentchatroom.contracts import task_state_for_legacy_status
from agentchatroom.errors import DomainError


@pytest.mark.parametrize(
    ("status", "previous_integration", "expected"),
    [
        ("claimed", "failed", "failed"),
        ("in_progress", "failed", "failed"),
        ("blocked", "failed", "failed"),
        ("verified", "failed", "failed"),
        ("awaiting_review", "failed", "failed"),
        ("claimed", "pending", "pending"),
        ("claimed", "approved", "pending"),
    ],
)
def test_failed_integration_is_the_only_verdict_a_field_edit_preserves(
    status, previous_integration, expected
):
    _, _, integration = task_state_for_legacy_status(
        status, previous_integration=previous_integration
    )
    assert integration == expected


def test_done_and_cancelled_carry_their_own_integration_verdict():
    assert task_state_for_legacy_status("done", previous_integration="failed")[2] == "done"
    assert (
        task_state_for_legacy_status("cancelled", previous_integration="failed")[2]
        == "pending"
    )


def test_existing_verification_retention_does_not_regress():
    execution, verification, integration = task_state_for_legacy_status(
        "claimed",
        previous_verification="changes_requested",
        previous_integration="pending",
    )
    assert (execution, verification, integration) == ("claimed", "changes_requested", "pending")

    _, kept, _ = task_state_for_legacy_status(
        "in_progress", previous_verification="approved", previous_integration="pending"
    )
    assert kept == "approved"

    assert task_state_for_legacy_status("done") == ("completed", "approved", "done")
    assert task_state_for_legacy_status("awaiting_review") == (
        "completed",
        "pending",
        "pending",
    )


def _park_ready_for_integration(service, project_id: str, task_id: str, owner_id: str):
    """Put the task where an approved work report would leave it.

    The report/review machinery is covered elsewhere; this isolates the
    integration-verdict rule.
    """
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            UPDATE tasks
            SET owner_session_id = ?, status = 'verified',
                execution_status = 'completed', verification_status = 'approved',
                integration_status = 'pending'
            WHERE id = ? AND project_id = ?
            """,
            (owner_id, task_id, project_id),
        )


def test_integration_failure_survives_a_later_field_edit(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = service.join_room(
        project_id,
        software_key="worker-a",
        name="Worker A",
        client="pytest",
        model="unknown",
    )
    task = service.create_task(
        project_id,
        title="Integration failed",
        acceptance_criteria=["A failed integration stays visible"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    _park_ready_for_integration(service, project_id, task["id"], owner["agent"]["id"])

    service.submit_integration(
        project_id,
        task["id"],
        result="failed",
        summary="merge conflict, integration aborted",
        tests=[{"command": "pytest -q", "exit_code": 1}],
        integrator_session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    failed = service.get_task(project_id, task["id"])
    assert failed["integration_status"] == "failed"
    assert failed["state_view"]["phase"] == "integration_failed"

    service.update_task(
        project_id,
        task["id"],
        next_step="resolve the conflict and retry integration",
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    after = service.get_task(project_id, task["id"])
    assert after["integration_status"] == "failed"
    assert after["state_view"]["phase"] == "integration_failed"
    assert after["next_step"] == "resolve the conflict and retry integration"

    # A retry replaces the verdict: the failure is not a permanent lock.
    service.submit_integration(
        project_id,
        task["id"],
        result="done",
        summary="rebased and merged",
        tests=[{"command": "pytest -q", "exit_code": 0}],
        integrator_session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    retried = service.get_task(project_id, task["id"])
    assert retried["integration_status"] == "done"
    assert retried["status"] == "done"


def test_integration_done_still_refuses_a_second_integration(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = service.join_room(
        project_id,
        software_key="worker-a",
        name="Worker A",
        client="pytest",
        model="unknown",
    )
    task = service.create_task(
        project_id,
        title="Integrated once",
        acceptance_criteria=["A done integration is final"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    _park_ready_for_integration(service, project_id, task["id"], owner["agent"]["id"])
    service.submit_integration(
        project_id,
        task["id"],
        result="done",
        summary="merged",
        tests=[{"command": "pytest -q", "exit_code": 0}],
        integrator_session_id=owner["agent"]["id"],
        token=owner["token"],
    )

    with pytest.raises(DomainError) as refused:
        service.submit_integration(
            project_id,
            task["id"],
            result="done",
            summary="again",
            tests=[{"command": "pytest -q", "exit_code": 0}],
            integrator_session_id=owner["agent"]["id"],
            token=owner["token"],
        )
    assert refused.value.code == "task_already_integrated"
