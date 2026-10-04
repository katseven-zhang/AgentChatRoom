"""Task #338: a refusal must be auditable, and the id it quotes must exist.

``work.commit_unverified`` used to be emitted inside the transaction that then
raised, so whenever the caller had supplied a ``request_id`` the audit row was
rolled back with the rest of the write - the Room never recorded the unverifiable
self-reported commit, while the response handed back an ``event_id`` no query
could resolve.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError

BOGUS_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _setup(service, project, joined_agents):
    executor, _reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Reports an unverifiable commit",
        acceptance_criteria=["Rejection is audited"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    return project["id"], executor, task


def _report(service, project_id, task, agent, **kwargs):
    return service.submit_work_report(
        project_id,
        task["id"],
        session_id=agent["agent"]["id"],
        token=agent["token"],
        summary="Claiming a commit nobody can resolve",
        files=["src/example.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
        **kwargs,
    )


def _rejections(service, project_id, task_id):
    return [
        event
        for event in service.query_audit(project_id, task_id=task_id)["events"]
        if event["event_type"] == "work.commit_unverified"
    ]


@pytest.mark.parametrize("with_request_id", [True, False])
def test_rejection_is_persisted_and_its_event_id_resolves(
    service, project, joined_agents, with_request_id
):
    """Persistence must not depend on whether the caller sent a request id."""
    project_id, executor, task = _setup(service, project, joined_agents)
    kwargs = {"commit_hash": BOGUS_COMMIT}
    if with_request_id:
        kwargs["request_id"] = "unverified-commit-report"

    with pytest.raises(DomainError) as refused:
        _report(service, project_id, task, executor, **kwargs)
    assert refused.value.code == "invalid_commit_hash"
    event_id = refused.value.details["event_id"]
    assert event_id

    rejections = _rejections(service, project_id, task["id"])
    assert len(rejections) == 1
    assert rejections[0]["id"] == event_id
    assert rejections[0]["payload"]["reported_commit_hash"] == BOGUS_COMMIT
    # Nothing was accepted: no work report exists and verification is untouched.
    # Nothing was accepted: no work report exists and the task is still in the
    # executing state with no verification verdict.
    stored = service.get_task(project_id, task["id"])
    assert (stored.get("reports") or []) == []
    assert stored["verification_status"] == "not_required"
    assert stored["execution_status"] == "claimed"

    # The id the caller was given resolves through the public event listing.
    listed = service.list_events(project_id, after=event_id - 1)["events"]
    assert [event["id"] for event in listed] and event_id in {
        event["id"] for event in listed
    }


def test_each_rejection_leaves_its_own_auditable_event(service, project, joined_agents):
    """A retry after a failed report is a second fact, and both must resolve."""
    project_id, executor, task = _setup(service, project, joined_agents)

    first_ids = []
    for index in range(2):
        with pytest.raises(DomainError) as refused:
            _report(
                service,
                project_id,
                task,
                executor,
                commit_hash=BOGUS_COMMIT,
                request_id=f"repeat-unverified-{index}",
            )
        first_ids.append(refused.value.details["event_id"])

    rejections = _rejections(service, project_id, task["id"])
    assert [event["id"] for event in rejections] == first_ids
    assert len(set(first_ids)) == 2
