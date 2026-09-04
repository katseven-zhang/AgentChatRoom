from __future__ import annotations

import logging

import pytest

from agentchatroom.contracts import (
    TASK_EXECUTION_STATUSES,
    TASK_INTEGRATION_STATUSES,
    TASK_VERIFICATION_STATUSES,
    TASK_VIEW_ATTENTION_PHASES,
    TASK_VIEW_GROUPS,
    TASK_VIEW_SCHEMA_VERSION,
    TASK_VIEW_UNCLASSIFIED_PHASE,
    task_contract,
    task_phase,
    task_view,
    task_view_contract,
)
from agentchatroom.services import _UNCLASSIFIED_TASK_VIEW_WARNED


EXPECTED_ALL_COMBINATIONS = {
    # (execution, verification, integration): phase
    # P1-P4: open execution faces
    ("todo", "not_required", "pending"): "todo",
    ("claimed", "not_required", "pending"): "claimed",
    ("in_progress", "not_required", "pending"): "in_progress",
    ("blocked", "not_required", "pending"): "blocked",
    # P5: submitted work report, awaiting independent review
    ("completed", "pending", "pending"): "awaiting_review",
    # P6: review returned; execution reopened or the task is a #43-style
    # released-after-review task back in the pool
    ("todo", "changes_requested", "pending"): "changes_requested",
    ("claimed", "changes_requested", "pending"): "changes_requested",
    ("in_progress", "changes_requested", "pending"): "changes_requested",
    ("blocked", "changes_requested", "pending"): "changes_requested",
    # P7 / P11: verified, waiting for integration (with or without review)
    ("completed", "approved", "pending"): "pending_integration",
    ("completed", "not_required", "pending"): "pending_integration",
    # P8: integration failed
    ("completed", "approved", "failed"): "integration_failed",
    ("completed", "not_required", "failed"): "integration_failed",
    # P9: the only phase that may be called finished
    ("completed", "approved", "done"): "done",
    ("completed", "not_required", "done"): "done",
    # P10: terminal guard — cancelled wins over stale verification residue
    ("cancelled", "not_required", "pending"): "cancelled",
    ("cancelled", "pending", "pending"): "cancelled",
    ("cancelled", "changes_requested", "pending"): "cancelled",
    ("cancelled", "approved", "pending"): "cancelled",
}


EXPECTED_UNCLASSIFIED_COMBINATIONS = {
    # legacy residue after cancelled -> todo reopening keeps approved
    ("todo", "approved", "pending"): "unclassified",
    # a verification can only be pending after a work report on a completed task
    ("in_progress", "pending", "pending"): "unclassified",
    ("claimed", "pending", "pending"): "unclassified",
    # a review verdict cannot stay changes_requested once execution completed
    ("completed", "changes_requested", "pending"): "unclassified",
    # blocked tasks cannot carry a verification result
    ("blocked", "approved", "pending"): "unclassified",
    # integration only runs after execution completed
    ("in_progress", "not_required", "done"): "unclassified",
    ("todo", "not_required", "failed"): "unclassified",
    ("cancelled", "approved", "done"): "unclassified",
    ("cancelled", "not_required", "failed"): "unclassified",
    ("claimed", "changes_requested", "done"): "unclassified",
}


def _all_triples():
    for execution in sorted(TASK_EXECUTION_STATUSES):
        for verification in sorted(TASK_VERIFICATION_STATUSES):
            for integration in sorted(TASK_INTEGRATION_STATUSES):
                yield execution, verification, integration


def test_task_view_projection_covers_every_triple_exactly_once():
    expected = {**EXPECTED_ALL_COMBINATIONS}
    unclassified_seen = 0
    total = 0
    for triple in _all_triples():
        view = task_view(
            execution_status=triple[0],
            verification_status=triple[1],
            integration_status=triple[2],
        )
        total += 1
        if triple in expected:
            assert view["phase"] == expected[triple], f"unexpected phase for {triple}"
        else:
            unclassified_seen += 1
            assert view["phase"] == "unclassified", (
                f"triple {triple} must be unclassified, got {view['phase']}"
            )
        assert view["group"] == TASK_VIEW_GROUPS[view["phase"]]
        assert view["primary_badge"] == view["phase"]
        assert view["schema_version"] == TASK_VIEW_SCHEMA_VERSION
    assert total == 6 * 4 * 3
    # Exactly the unclassified residue set plus every other non-listed triple.
    assert unclassified_seen == total - len(expected)
    for triple in EXPECTED_UNCLASSIFIED_COMBINATIONS:
        assert triple not in expected


def test_task_view_projection_has_eleven_valid_phases_plus_fallback():
    phases = {phase for phase in EXPECTED_ALL_COMBINATIONS.values()}
    assert phases == {
        "todo",
        "claimed",
        "in_progress",
        "blocked",
        "awaiting_review",
        "changes_requested",
        "pending_integration",
        "integration_failed",
        "done",
        "cancelled",
    }
    # P11 shares the pending_integration phase with P7 by design
    assert task_view(
        execution_status="completed",
        verification_status="not_required",
        integration_status="pending",
    )["phase"] == "pending_integration"


@pytest.mark.parametrize(
    ("execution", "verification", "integration", "attention"),
    [
        ("in_progress", "changes_requested", "pending", True),
        ("blocked", "not_required", "pending", True),
        ("completed", "approved", "failed", True),
        ("in_progress", "not_required", "pending", False),
        ("completed", "pending", "pending", False),
        ("completed", "approved", "pending", False),
        ("completed", "approved", "done", False),
        ("todo", "not_required", "pending", False),
        ("cancelled", "changes_requested", "pending", False),
    ],
)
def test_needs_attention_marks_exactly_the_three_anomaly_phases(
    execution, verification, integration, attention
):
    view = task_view(
        execution_status=execution,
        verification_status=verification,
        integration_status=integration,
    )
    assert view["needs_attention"] is attention
    assert (view["phase"] in TASK_VIEW_ATTENTION_PHASES) is attention


def test_attention_inbox_deduplicates_blocked_and_returned_tasks():
    # A task that is blocked AND returned must appear once in the inbox:
    # primary badge is the return, auxiliary badge is the block.
    view = task_view(
        execution_status="blocked",
        verification_status="changes_requested",
        integration_status="pending",
    )
    assert view["phase"] == "changes_requested"
    assert view["auxiliary_badges"] == ["blocked"]
    assert view["needs_attention"] is True

    inbox_phases = TASK_VIEW_ATTENTION_PHASES
    sample_tasks = [
        ("in_progress", "changes_requested", "pending"),
        ("blocked", "not_required", "pending"),
        ("completed", "approved", "failed"),
        ("blocked", "changes_requested", "pending"),  # blocked AND returned
        ("todo", "not_required", "pending"),  # not an anomaly
    ]
    inbox_ids = {
        index
        for index, (execution, verification, integration) in enumerate(sample_tasks)
        if task_view(
            execution_status=execution,
            verification_status=verification,
            integration_status=integration,
        )["needs_attention"]
    }
    assert len(inbox_ids) == 4  # four anomalous tasks, each counted once
    assert all(
        task_view(
            execution_status=sample_tasks[index][0],
            verification_status=sample_tasks[index][1],
            integration_status=sample_tasks[index][2],
        )["phase"] in inbox_phases
        for index in inbox_ids
    )


def test_task_40_scenario_is_visible_as_returned_and_unfinished():
    # Task #40 itself: execution reopened after review changes_requested.
    view = task_view(
        execution_status="in_progress",
        verification_status="changes_requested",
        integration_status="pending",
    )
    assert view["phase"] == "changes_requested"
    assert view["group"] == "active"
    assert view["needs_attention"] is True
    assert view["primary_badge"] == "changes_requested"
    # unfinished = group is neither done nor cancelled
    assert view["group"] not in {"done", "cancelled"}
    # and it must also be reachable from the claimable entry after a release
    released = task_view(
        execution_status="todo",
        verification_status="changes_requested",
        integration_status="pending",
    )
    assert released["phase"] == "changes_requested"
    assert released["needs_attention"] is True


def test_legacy_task_phase_accessor_matches_the_shared_projection():
    samples = {
        **EXPECTED_ALL_COMBINATIONS,
        **EXPECTED_UNCLASSIFIED_COMBINATIONS,
        ("blocked", "approved", "done"): "unclassified",
    }
    for triple, phase in samples.items():
        assert task_phase(
            execution_status=triple[0],
            verification_status=triple[1],
            integration_status=triple[2],
            status="awaiting_review",  # legacy status must not drive projection
        ) == phase


def test_task_view_contract_alias_matches_projection():
    assert task_view_contract(
        execution_status="completed",
        verification_status="approved",
        integration_status="pending",
    ) == task_view(
        execution_status="completed",
        verification_status="approved",
        integration_status="pending",
    )


def test_task_contract_state_phase_delegates_to_the_projection():
    state = task_contract(
        execution_status="completed",
        verification_status="approved",
        integration_status="pending",
        legacy_status="verified",
    )
    assert state["phase"] == "pending_integration"
    assert state["verified"] is True
    assert state["integrated"] is False


def test_unclassified_projection_warns_once_per_task(service, project, caplog):
    created = service.create_task(
        project["id"],
        title="Legacy residue task",
        acceptance_criteria=["Projection surfaces unknown combinations"],
    )["task"]
    # Force an unclassified triple as legacy residue would look after a
    # cancelled -> todo reopening that kept an approved verification value.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE tasks SET verification_status = 'approved' WHERE id = ?",
            (created["id"],),
        )
    _UNCLASSIFIED_TASK_VIEW_WARNED.clear()
    task = service.get_task(project["id"], created["id"])
    assert task["state_view"]["phase"] == TASK_VIEW_UNCLASSIFIED_PHASE
    with caplog.at_level(logging.WARNING, logger="agentchatroom.services"):
        service.get_task(project["id"], created["id"])
        service.get_task(project["id"], created["id"])
    warnings = [
        record
        for record in caplog.records
        if "unclassified state view" in record.getMessage()
    ]
    assert len(warnings) == 1  # warned once per task, not per serialization


def test_list_tasks_filters_by_view_phase_and_attention(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    todo_task = service.create_task(
        project["id"], title="Todo sample", acceptance_criteria=["c"]
    )["task"]
    blocked_task = service.create_task(
        project["id"], title="Blocked sample", acceptance_criteria=["c"]
    )["task"]
    # claim -> in_progress -> blocked through the shared state machine
    executor_session = executor["agent"]["id"]
    service.claim_task(
        project["id"], blocked_task["id"], executor_session, executor["token"]
    )
    service.update_task(
        project["id"],
        blocked_task["id"],
        status="in_progress",
        session_id=executor_session,
        token=executor["token"],
    )
    service.update_task(
        project["id"],
        blocked_task["id"],
        status="blocked",
        blocker_reason="waiting",
        session_id=executor_session,
        token=executor["token"],
    )
    done_task = service.create_task(
        project["id"], title="Done sample", acceptance_criteria=["c"]
    )["task"]

    assert {task["id"] for task in service.list_tasks(project["id"], phase="todo")} == {
        todo_task["id"],
        done_task["id"],
    }
    assert {
        task["id"] for task in service.list_tasks(project["id"], phase="blocked")
    } == {blocked_task["id"]}
    attention = service.list_tasks(project["id"], phase="attention")
    assert {task["id"] for task in attention} == {blocked_task["id"]}
    assert all(task["state_view"]["needs_attention"] for task in attention)
    # unknown phase codes simply match nothing
    assert service.list_tasks(project["id"], phase="no-such-phase") == []
