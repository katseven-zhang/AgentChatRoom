"""Task #248: review and integration evidence is bounded like every other input.

Reviews and integrations land in the append-only event log, so an unbounded
``criteria``/``files`` list is permanent, and a criterion with a blank name or a
status outside passed/failed could be filed as evidence that never actually
assesses anything.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _awaiting_review_task(service, project, joined_agents):
    executor, _reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Bound review inputs",
        acceptance_criteria=["Criteria are assessed"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    service.claim_task(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    service.update_task(
        project["id"],
        task["id"],
        status="in_progress",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Implemented the bounded path",
        files=["src/auth/login.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    assert report["task_status"] == "awaiting_review"
    return task


def _event_total(service, project_id):
    return len(service.list_events(project_id, after=0)["events"])


def _review(service, project, task, reviewer, criteria, verdict="changes_requested"):
    return service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict=verdict,
        criteria=criteria,
        notes="Evidence shape check",
    )


def test_review_criteria_over_the_bound_are_rejected_without_a_write(
    service, project, joined_agents
):
    _executor, reviewer = joined_agents
    task = _awaiting_review_task(service, project, joined_agents)
    events_before = _event_total(service, project["id"])

    with pytest.raises(DomainError) as error:
        _review(
            service,
            project,
            task,
            reviewer,
            [
                {"criterion": f"criterion {index}", "status": "failed"}
                for index in range(101)
            ],
        )

    assert error.value.code == "list_input_too_long"
    assert error.value.status_code == 422
    assert _event_total(service, project["id"]) == events_before
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM reviews"
        ).fetchone()["total"] == 0


@pytest.mark.parametrize(
    "criteria",
    [
        [{"criterion": "", "status": "failed"}],
        [{"criterion": "Criterion one", "status": "skipped"}],
        [{"criterion": "Criterion one"}],
        ["not an object"],
    ],
)
def test_review_criterion_shape_is_enforced(
    service, project, joined_agents, criteria
):
    _executor, reviewer = joined_agents
    task = _awaiting_review_task(service, project, joined_agents)
    events_before = _event_total(service, project["id"])

    with pytest.raises(DomainError) as error:
        _review(service, project, task, reviewer, criteria)

    assert error.value.code == "invalid_review_criteria"
    assert error.value.status_code == 422
    assert _event_total(service, project["id"]) == events_before


def test_integration_files_over_the_bound_are_rejected_without_a_write(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    task = _awaiting_review_task(service, project, joined_agents)
    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Criteria are assessed", "status": "passed"}],
        notes="Independent approval",
    )
    events_before = _event_total(service, project["id"])

    with pytest.raises(DomainError) as error:
        service.submit_integration(
            project["id"],
            task["id"],
            result="done",
            summary="Merged the bounded path",
            tests=[{"command": "pytest", "exit_code": 0}],
            files=[f"src/module_{index}.py" for index in range(101)],
            integrator_session_id=reviewer["agent"]["id"],
            token=reviewer["token"],
        )

    assert error.value.code == "list_input_too_long"
    assert _event_total(service, project["id"]) == events_before
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM task_integrations"
        ).fetchone()["total"] == 0
    reloaded = service.get_task(project["id"], task["id"])
    assert reloaded["integration_status"] == "pending"
