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


def _session(service, project_id: str, key: str, client: str = "pytest"):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client=client, model="unknown"
    )


def test_reacquiring_a_scope_with_a_stronger_mode_upgrades_it(service, project_dir):
    """#220: a readonly lease re-requested as exclusive used to return the old
    mode, so the caller believed it held exclusive protection it did not have."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _session(service, project_id, "worker-a")
    service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/up.py",
        mode="readonly",
    )

    upgraded = service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/up.py",
        mode="exclusive",
    )
    assert upgraded["already_held"] is True
    assert upgraded["mode_changed"] is True
    assert upgraded["lease"]["mode"] == "exclusive"

    other = _session(service, project_id, "worker-b")
    with pytest.raises(DomainError) as conflict:
        service.acquire_lease(
            project_id,
            session_id=other["agent"]["id"],
            token=other["token"],
            path_pattern="src/up.py",
            mode="exclusive",
        )
    assert conflict.value.code == "lease_conflict"
    events = service.query_audit(project_id)["events"]
    changed = next(
        event for event in events if event["event_type"] == "lease.mode_changed"
    )
    assert changed["payload"]["from_mode"] == "readonly"
    assert changed["payload"]["to_mode"] == "exclusive"


def test_weakening_an_active_mode_requires_an_explicit_release(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _session(service, project_id, "worker-a")
    service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/down.py",
        mode="exclusive",
    )

    with pytest.raises(DomainError) as refused:
        service.acquire_lease(
            project_id,
            session_id=owner["agent"]["id"],
            token=owner["token"],
            path_pattern="src/down.py",
            mode="readonly",
        )
    assert refused.value.code == "lease_mode_downgrade_requires_release"
    assert refused.value.details["required_action"] == "release_lease_then_reacquire"
    held = [
        lease
        for lease in service.list_leases(project_id)
        if lease["path_pattern"] == "src/down.py" and lease.get("released_at") is None
    ]
    assert [lease["mode"] for lease in held] == ["exclusive"]


def test_a_rejected_acquire_does_not_release_the_predecessor_lease(
    service, project_dir
):
    """#235: the same-identity takeover ran before the conflict decision, so a
    rejected request still tore down its own identity's coverage."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    first = _session(service, project_id, "worker-a")
    successor = _session(service, project_id, "worker-a", client="pytest")
    blocker = _session(service, project_id, "worker-c")

    stale = service.acquire_lease(
        project_id,
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/a.py",
        mode="exclusive",
    )
    service.acquire_lease(
        project_id,
        session_id=blocker["agent"]["id"],
        token=blocker["token"],
        path_pattern="src/b.py",
        mode="exclusive",
    )

    with pytest.raises(DomainError) as conflict:
        service.acquire_lease(
            project_id,
            session_id=successor["agent"]["id"],
            token=successor["token"],
            path_pattern="src/*.py",
            mode="exclusive",
            # takeover=true so the request gets past its own identity's live
            # predecessor and all the way to the foreign-conflict decision. The
            # bug under test is about what a *rejected* request does to the
            # predecessor's lease, so the rejection has to be the conflict one.
            # The live-predecessor gate has its own test in test_services.py.
            takeover=True,
        )
    assert conflict.value.code == "lease_conflict"
    assert {item["path_pattern"] for item in conflict.value.details["conflicts"]} == {
        "src/b.py"
    }
    still_held = [
        lease["id"]
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    ]
    assert stale["lease"]["id"] in still_held
    released_events = [
        event
        for event in service.query_audit(project_id)["events"]
        if event["event_type"] == "lease.released"
    ]
    assert released_events == []


def test_a_narrow_takeover_keeps_the_broader_lease_of_its_own_identity(
    service, project_dir
):
    """#235: the takeover used to fire on mere overlap, so acquiring a single
    file quietly retired the same identity's directory-wide lease."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    first = _session(service, project_id, "worker-a")
    successor = _session(service, project_id, "worker-a")
    broad = service.acquire_lease(
        project_id,
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/**",
        mode="exclusive",
    )

    result = service.acquire_lease(
        project_id,
        session_id=successor["agent"]["id"],
        token=successor["token"],
        path_pattern="src/a.py",
        mode="exclusive",
    )
    assert result["lease"]["path_pattern"] == "src/a.py"
    active = {
        lease["path_pattern"]
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    }
    assert {"src/**", "src/a.py"} <= active
    assert broad["lease"]["id"] in [
        lease["id"]
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    ]


def test_a_replacing_takeover_does_retire_the_covered_lease(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    first = _session(service, project_id, "worker-a")
    successor = _session(service, project_id, "worker-a")
    narrow = service.acquire_lease(
        project_id,
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/a.py",
        mode="exclusive",
    )

    service.acquire_lease(
        project_id,
        session_id=successor["agent"]["id"],
        token=successor["token"],
        path_pattern="src/**",
        mode="exclusive",
        # A broader scope from the same identity replaces the narrower lease.
        # The predecessor is still live, so the successor says so explicitly;
        # what this test pins is the scope comparison, not the consent flag.
        takeover=True,
    )
    active = {
        lease["id"]
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    }
    assert narrow["lease"]["id"] not in active
    takeover = [
        event
        for event in service.query_audit(project_id)["events"]
        if event["event_type"] == "lease.released"
        and event["payload"].get("reason") == "same_member_takeover_explicit"
    ]
    assert [event["payload"]["lease_id"] for event in takeover] == [narrow["lease"]["id"]]


def test_check_leases_treats_ones_own_identity_as_takeable(service, project_dir):
    """#245: check_leases reported a conflict against the caller's own software
    identity, while acquire_lease accepts the same situation as a takeover."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    first = _session(service, project_id, "worker-a")
    successor = _session(service, project_id, "worker-a", client="pytest")
    stranger = _session(service, project_id, "worker-b")
    service.acquire_lease(
        project_id,
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/adopt.py",
        mode="exclusive",
    )

    own = service.check_leases(
        project_id,
        paths=["src/adopt.py"],
        session_id=successor["agent"]["id"],
        token=successor["token"],
    )
    assert not own["conflicts"]

    foreign = service.check_leases(
        project_id,
        paths=["src/adopt.py"],
        session_id=stranger["agent"]["id"],
        token=stranger["token"],
    )
    assert foreign["conflicts"]
