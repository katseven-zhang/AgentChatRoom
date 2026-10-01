"""Tasks #246 and #247: one file must have one lease scope.

The scope stored for a lease is the only thing the conflict scan and the
pre-commit check compare, so an unnormalised spelling silently duplicated the
exclusive scope, and a wildcard at the head of a pattern used to be read as
"overlaps everything".
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import normalize_pattern, patterns_overlap


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("././src", "src"),
        ("src/./a.py", "src/a.py"),
        ("src\\a.py", "src/a.py"),
        ("src//", "src"),
        ("./src/./nested/./a.py", "src/nested/a.py"),
        ("/src/a.py", "src/a.py"),
        ("src/*.py", "src/*.py"),
    ],
)
def test_normalize_pattern_folds_every_spelling(spelling, expected):
    assert normalize_pattern(spelling) == expected


@pytest.mark.parametrize("illegal", ["", ".", "./", "../", "../src/a.py", "src/../../x"])
def test_normalize_pattern_still_rejects_escapes(illegal):
    with pytest.raises(DomainError) as error:
        normalize_pattern(illegal)
    assert error.value.code == "invalid_path_pattern"


@pytest.mark.parametrize(
    ("left", "right", "overlap"),
    [
        # A leading wildcard is a scope, not a claim on the whole project.
        ("**/*.py", "src/app.md", False),
        ("*.md", "src/app.py", False),
        ("src/*.py", "docs/*.py", False),
        ("*.py", "*.md", False),
        # Real overlaps still conflict.
        ("src/**", "src/a.py", True),
        ("src/*.py", "src/app.py", True),
        ("**/*.py", "src/a.py", True),
        ("src", "src/a.py", True),
        ("src/a.py", "src/a.py", True),
        # Equivalent spellings of one file are one scope (#246).
        ("./src/a.py", "src/a.py", True),
        ("src/./a.py", "././src/a.py", True),
    ],
)
def test_patterns_overlap(left, right, overlap):
    assert patterns_overlap(left, right) is overlap
    assert patterns_overlap(right, left) is overlap


def test_equivalent_spellings_reuse_the_lease_instead_of_duplicating_it(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = service.join_room(
        project_id,
        software_key="worker-a",
        name="Worker A",
        client="pytest",
        model="unknown",
    )
    service.claim_task(
        project_id,
        service.create_task(
            project_id,
            title="Lease scope",
            acceptance_criteria=["One scope per file"],
            actor_session_id=owner["agent"]["id"],
            token=owner["token"],
        )["task"]["id"],
        owner["agent"]["id"],
        owner["token"],
    )
    task_id = service.get_task(
        project_id,
        service.list_tasks(project_id)[0]["id"],
    )["id"]

    first = service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/app.py",
        task_id=task_id,
    )
    again = service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="./src/./app.py",
        task_id=task_id,
    )
    assert again["already_held"] is True
    assert again["lease"]["id"] == first["lease"]["id"]
    scopes = [
        lease["path_pattern"]
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    ]
    assert scopes == ["src/app.py"]


def test_a_second_agent_cannot_take_the_same_scope_through_a_new_spelling(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    holder = service.join_room(
        project_id,
        software_key="worker-a",
        name="Worker A",
        client="pytest",
        model="unknown",
    )
    intruder = service.join_room(
        project_id,
        software_key="worker-b",
        name="Worker B",
        client="pytest",
        model="unknown",
    )
    service.acquire_lease(
        project_id,
        session_id=holder["agent"]["id"],
        token=holder["token"],
        path_pattern="src/app.py",
    )

    with pytest.raises(DomainError) as conflict:
        service.acquire_lease(
            project_id,
            session_id=intruder["agent"]["id"],
            token=intruder["token"],
            path_pattern="./src/./app.py",
        )
    assert conflict.value.code == "lease_conflict"


def test_unrelated_leading_wildcard_scope_does_not_block_another_agent(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    writer = service.join_room(
        project_id,
        software_key="worker-a",
        name="Worker A",
        client="pytest",
        model="unknown",
    )
    documenter = service.join_room(
        project_id,
        software_key="worker-b",
        name="Worker B",
        client="pytest",
        model="unknown",
    )
    service.acquire_lease(
        project_id,
        session_id=documenter["agent"]["id"],
        token=documenter["token"],
        path_pattern="**/*.md",
    )

    held = service.acquire_lease(
        project_id,
        session_id=writer["agent"]["id"],
        token=writer["token"],
        path_pattern="src/app.py",
    )
    assert held["lease"]["path_pattern"] == "src/app.py"
    checked = service.check_leases(
        project_id,
        paths=["src/app.py"],
        session_id=writer["agent"]["id"],
        token=writer["token"],
    )
    assert not checked["conflicts"]
