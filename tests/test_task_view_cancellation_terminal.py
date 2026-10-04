"""Task #259: cancellation is terminal for the whole projected triple.

`task_view` documented cancelled tasks as terminal but only matched the
``integration == "pending"`` case, so a task cancelled after an integration
attempt projected as ``unclassified`` in the Web timeline.
"""

from __future__ import annotations

import itertools

import pytest

from agentchatroom.contracts import (
    TASK_INTEGRATION_STATUSES,
    TASK_VERIFICATION_STATUSES,
    TASK_VIEW_ALL_PHASES,
    TASK_VIEW_UNCLASSIFIED_PHASE,
    task_view,
)

VERIFICATIONS = sorted(TASK_VERIFICATION_STATUSES)
INTEGRATIONS = sorted(TASK_INTEGRATION_STATUSES)


@pytest.mark.parametrize("verification", VERIFICATIONS)
@pytest.mark.parametrize("integration", INTEGRATIONS)
def test_cancelled_never_falls_through_to_unclassified(verification, integration):
    view = task_view(
        execution_status="cancelled",
        verification_status=verification,
        integration_status=integration,
    )

    assert view["phase"] == "cancelled"
    assert view["group"] == "cancelled"
    assert view["needs_attention"] is False
    assert view["primary_badge"] == "cancelled"


def test_no_cancelled_triple_is_unclassified():
    for verification, integration in itertools.product(VERIFICATIONS, INTEGRATIONS):
        view = task_view(
            execution_status="cancelled",
            verification_status=verification,
            integration_status=integration,
        )
        assert view["phase"] != TASK_VIEW_UNCLASSIFIED_PHASE, (
            verification,
            integration,
        )
    assert TASK_VIEW_UNCLASSIFIED_PHASE in TASK_VIEW_ALL_PHASES


def test_an_unknown_verification_value_cannot_reintroduce_the_masking():
    view = task_view(
        execution_status="cancelled",
        verification_status="approved_by_a_future_release",
        integration_status="failed",
    )

    assert view["phase"] == "cancelled"
    assert view["group"] == "cancelled"


def test_other_illegal_triples_still_report_unclassified():
    view = task_view(
        execution_status="in_progress",
        verification_status="approved",
        integration_status="done",
    )

    assert view["phase"] == "unclassified"


def test_a_cancelled_task_with_integration_residue_reads_as_cancelled(
    service, project, joined_agents
):
    owner, _ = joined_agents
    task = service.create_task(
        project["id"],
        title="Cancelled after a failed integration",
        acceptance_criteria=["phase still reads cancelled"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE tasks SET execution_status = 'cancelled', status = 'cancelled',"
            " verification_status = 'approved', integration_status = 'failed'"
            " WHERE id = ?",
            (task["id"],),
        )

    stored = next(
        item for item in service.snapshot(project["id"])["tasks"]
        if item["id"] == task["id"]
    )

    assert stored["state_view"]["phase"] == "cancelled"
    assert stored["state_view"]["integration_status"] == "failed"
