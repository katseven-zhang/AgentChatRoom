"""Task #217: evidence `files` are paths, not lease patterns.

Message, handoff, work report, and integration lists all went through the lease
pattern normalizer, so they answered with the lease error code and accepted
absolute and glob-shaped claims.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import normalize_evidence_path, normalize_pattern

REJECTED = [
    ("src/*.py", "*?[]"),
    ("src/a[1].py", "*?[]"),
    ("src/?.py", "*?[]"),
    ("/etc/passwd", "absolute"),
    ("~/secrets/id_rsa", "absolute"),
]


def test_plain_paths_fold_without_lease_semantics():
    assert normalize_evidence_path("./src//a.py") == "src/a.py"
    assert normalize_evidence_path("src\\nested\\b.py") == "src/nested/b.py"
    assert normalize_evidence_path("  src/a.py  ") == "src/a.py"


@pytest.mark.parametrize("value", ["", "   ", ".", "..", "src/../etc/x", "/etc/passwd", "~/x", r"C:\<checkout>\x.py", "//server/share/x"])
def test_paths_that_cannot_be_project_evidence_are_rejected(value):
    with pytest.raises(DomainError) as captured:
        normalize_evidence_path(value)

    error = captured.value
    assert error.code == "invalid_changed_file"
    assert error.status_code == 422
    assert error.details["required_action"]


@pytest.mark.parametrize("value,reason", REJECTED)
def test_glob_shapes_are_not_evidence(value, reason):
    with pytest.raises(DomainError) as captured:
        normalize_evidence_path(value)

    assert captured.value.code == "invalid_changed_file"
    assert "concrete path" in captured.value.message or "relative" in captured.value.message


def test_the_lease_pattern_normalizer_still_accepts_globs():
    assert normalize_pattern("src/**/*.py") == "src/**/*.py"
    assert normalize_pattern("././src") == "src"


def test_post_message_rejects_a_glob_with_its_own_code(service, project, joined_agents):
    owner, _ = joined_agents

    with pytest.raises(DomainError) as captured:
        service.post_message(
            project["id"],
            body="evidence",
            session_id=owner["agent"]["id"],
            token=owner["token"],
            files=["src/*.py"],
            model_display_name="test-model",
        )

    assert captured.value.code == "invalid_changed_file"
    assert captured.value.details["field"] == "files"


def test_post_message_stores_folded_concrete_files(service, project, joined_agents):
    owner, _ = joined_agents

    posted = service.post_message(
        project["id"],
        body="evidence",
        session_id=owner["agent"]["id"],
        token=owner["token"],
        files=["./src//a.py", "src\\b.py", "src/a.py"],
        model_display_name="test-model",
    )
    events = service.list_events(project["id"], after=0)["events"]
    message = next(event for event in events if event["project_seq"] == posted["event_id"])

    assert message["payload"]["files"] == ["src/a.py", "src/b.py"]


def test_lease_surface_still_uses_the_glob_vocabulary(service, project, joined_agents):
    owner, _ = joined_agents

    lease = service.acquire_lease(
        project["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/**/*.py",
    )

    assert lease["lease"]["path_pattern"] == "src/**/*.py"
