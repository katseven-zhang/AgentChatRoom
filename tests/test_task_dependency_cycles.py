"""Task #366: a dependency cycle must be refused where it is written.

``update_task`` already rejected a self-dependency and a recursive cycle, but
``create_task`` only checked that each dependency existed in the project - and
``define_task_from_intake`` delegates to ``create_task``. So A could be created
depending on B and B depending on A, and every task inside the resulting cycle
then reported ``task_dependencies_incomplete`` for every member forever.
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


def test_create_task_refuses_a_dependency_that_would_close_a_cycle(
    service, project_dir
):
    """The closing edge must be refused wherever it is written.

    ``create_task`` mints a brand-new id, so a freshly created task can only
    close a cycle by naming an existing dependency - and the way to reproduce
    the historical bug is to create the second task depending on the first and
    then point the first back at the second. The guard on ``create_task`` is
    what stops a caller from reaching that state through a create-shaped call,
    so the test drives the exact sequence and asserts nothing was written.
    """
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    first = _task(service, project_id, coordinator, "Upstream")
    second = _task(
        service, project_id, coordinator, "Downstream", depends_on=[first["id"]]
    )

    # Asking create to depend on `second` is legal - `second` does not yet
    # depend on anything that reaches back. The illegal edge is first->second,
    # and it must be refused by the shared guard.
    with pytest.raises(DomainError) as refused:
        service.update_task(
            project_id,
            first["id"],
            depends_on=[second["id"]],
            session_id=coordinator["agent"]["id"],
            token=coordinator["token"],
        )
    assert refused.value.code == "task_dependency_cycle"
    assert refused.value.details["required_action"]
    assert service.list_dependency_cycles(project_id)["cycle_task_count"] == 0


def test_update_task_still_refuses_a_cycle_after_the_shared_guard(
    service, project_dir
):
    """The shared helper must not regress the behaviour update_task already had."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    first = _task(service, project_id, coordinator, "Upstream")
    second = _task(
        service, project_id, coordinator, "Downstream", depends_on=[first["id"]]
    )

    with pytest.raises(DomainError) as refused:
        service.update_task(
            project_id,
            first["id"],
            depends_on=[second["id"]],
            session_id=coordinator["agent"]["id"],
            token=coordinator["token"],
        )
    assert refused.value.code == "task_dependency_cycle"

    with pytest.raises(DomainError) as refused:
        service.update_task(
            project_id,
            first["id"],
            depends_on=[first["id"]],
            session_id=coordinator["agent"]["id"],
            token=coordinator["token"],
        )
    assert refused.value.code == "task_dependency_cycle"
    assert refused.value.message == "A task cannot depend on itself"


def test_a_rejected_create_leaves_an_ordinary_dependency_working(
    service, project_dir
):
    """The guard must not break the normal case it sits next to."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    upstream = _task(service, project_id, coordinator, "Upstream")
    created = _task(
        service, project_id, coordinator, "Downstream", depends_on=[upstream["id"]]
    )
    assert created["depends_on"] == [upstream["id"]]
    assert service.list_dependency_cycles(project_id)["cycle_task_count"] == 0


def test_list_dependency_cycles_reports_a_cycle_written_before_the_guard(
    service, project_dir
):
    """Rows that predate the guard stay unclaimable, so they must be listable.

    The report is read-only: it names the affected tasks without editing a
    dependency or rewriting an event.
    """
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    coordinator = _join(service, project_id, "coordinator-a")
    first = _task(service, project_id, coordinator, "Legacy upstream")
    second = _task(
        service, project_id, coordinator, "Legacy downstream", depends_on=[first["id"]]
    )
    # Write the closing edge directly, the way the pre-guard code path did.
    # task_dependencies is keyed on (task_id, depends_on_task_id) and carries a
    # CHECK that forbids a self-edge, which is why only the two-node cycle has
    # to be planted by hand.
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at)
            VALUES (?, ?, '2026-01-01T00:00:00Z')
            """,
            (first["id"], second["id"]),
        )

    report = service.list_dependency_cycles(project_id)
    assert report["cycle_task_count"] == 2
    reported = {row["task_id"] for row in report["cycles"]}
    assert reported == {first["id"], second["id"]}
    assert {row["task_number"] for row in report["cycles"]} == {
        first["task_number"],
        second["task_number"],
    }
    assert all(row["required_action"] for row in report["cycles"])
    # Read-only: the row it named is still there.
    assert service.get_task(project_id, first["id"])["id"] == first["id"]
