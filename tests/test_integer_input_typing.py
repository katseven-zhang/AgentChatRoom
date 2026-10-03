"""Task #351, second pass: every integer-typed input must refuse booleans.

``exit_code: false`` and ``priority: true`` were already refused through
``is_strict_int``. This pass closes the remaining integer entry points that
still used ``int(x or 0)`` or a bare range check, where a JSON boolean was
silently read as 1:

- paging: ``after`` / ``before`` / ``limit`` / ``cursor`` -> 1
- leases: ``ttl_seconds`` -> the 1-second minimum, the opposite of intent
- Agent tokens: ``expires_in_seconds`` and ``extend_by_seconds`` -> 1 second

A lease that silently becomes one second long is the worst of these: the Agent
believes it holds a file for half an hour while another writer can take it
immediately.
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


# --- paging -----------------------------------------------------------------


@pytest.mark.parametrize("field", ["after", "before", "limit"])
def test_a_boolean_audit_paging_parameter_is_refused(service, project_dir, field):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]

    with pytest.raises(DomainError) as refused:
        service.query_audit(project_id, **{field: True})
    assert refused.value.code == f"invalid_{field}"
    assert refused.value.status_code == 422


@pytest.mark.parametrize("field", ["after", "before", "cursor", "limit"])
def test_a_boolean_history_paging_parameter_is_refused(service, project_dir, field):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "history-reader")
    task = _task(service, project_id, agent, "A task with history")

    with pytest.raises(DomainError) as refused:
        service.list_task_history(project_id, task["id"], **{field: True})
    assert refused.value.code == f"invalid_{field}"


def test_a_real_integer_still_pages(service, project_dir):
    """The guard must not break the ordinary case next to it."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "pager")
    _task(service, project_id, agent, "something to page over")

    page = service.query_audit(project_id, after=0, limit=10)
    assert page["events"]


# --- leases -----------------------------------------------------------------


def test_a_boolean_lease_ttl_is_refused_rather_than_becoming_one_second(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "lessee")

    with pytest.raises(DomainError) as refused:
        service.acquire_lease(
            project_id,
            session_id=agent["agent"]["id"],
            token=agent["token"],
            path_pattern="src/agentchatroom/services.py",
            ttl_seconds=True,
        )
    assert refused.value.code == "invalid_ttl_seconds"

    # And the real thing still works.
    lease = service.acquire_lease(
        project_id,
        session_id=agent["agent"]["id"],
        token=agent["token"],
        path_pattern="src/agentchatroom/services.py",
        ttl_seconds=600,
    )
    assert lease["lease"]["ttl_seconds"] == 600


# --- Agent tokens -----------------------------------------------------------


def test_a_boolean_token_ttl_is_refused(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]

    with pytest.raises(DomainError) as refused:
        service.issue_agent_token(project_id, name="wrong-type", expires_in_seconds=True)
    assert refused.value.code == "invalid_expires_in_seconds"


def test_a_boolean_token_extension_is_refused(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    _join(service, project_id, "token-holder")
    issued = service.issue_agent_token(project_id, name="extendable")
    credential_id = issued["credential"]["id"]

    with pytest.raises(DomainError) as refused:
        service.extend_agent_token(
            project_id, credential_id, extend_by_seconds=True
        )
    assert refused.value.code == "invalid_extend_by_seconds"


# --- the shared predicate ---------------------------------------------------


def test_a_missing_value_still_falls_back_to_the_default():
    """The default exists for a missing value, never to paper over a wrong type."""
    from agentchatroom.services import require_int_or_default

    assert require_int_or_default(None, "after", 0) == 0
    assert require_int_or_default(0, "after", 200) == 0
    assert require_int_or_default(7, "after", 200) == 7
    for wrong in (True, False, "3", 1.5):
        with pytest.raises(DomainError):
            require_int_or_default(wrong, "after", 0)
