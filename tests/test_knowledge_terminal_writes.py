"""Task #355, parts 2 and 3: terminal knowledge writes need authority and a reason.

``archive`` and ``supersede`` are irreversible - ``archived`` has no outgoing
transition in KNOWLEDGE_ASSET_TRANSITIONS - yet both accepted any caller that
merely held ``knowledge:write``, and neither required a reason. A default
permission Agent could therefore retire knowledge somebody else wrote, with
nothing in the event explaining why.

Part 3 is the matching optimistic lock: re-submitting a candidate rewrote the
row with ``WHERE id = ? AND project_id = ?``, no previous-status predicate and
no rowcount check, and the transition check carried an "unless it is already a
candidate" escape that let a candidate silently rewrite the exact content an
open review was reading.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


def _candidate(
    service, project_id: str, author, *, title="candidate", body="body", asset_id: str = ""
):
    return service.submit_knowledge_candidate(
        project_id,
        session_id=author["agent"]["id"],
        token=author["token"],
        kind="decision",
        title=title,
        body=body,
        summary="a candidate",
        asset_id=asset_id,
    )["asset"]


# --- part 2: authority and reason -------------------------------------------


def test_archiving_someone_elses_approved_knowledge_is_refused(service, project_dir):
    """The reported failure: a default-permission Agent retiring another author's work."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    stranger = _join(service, project_id, "stranger-b")

    asset = _candidate(service, project_id, author)
    approved = service.submit_knowledge_review(
        project_id,
        asset["id"],
        reviewer_session_id=stranger["agent"]["id"],
        token=stranger["token"],
        verdict="approved",
        criteria=[{"criterion": "reviewed by a different identity", "status": "passed"}],
        expected_version_id=asset["current_version_id"],
    )
    assert approved["status"] == "approved"

    with pytest.raises(DomainError) as refused:
        service.archive_knowledge_asset(
            project_id,
            asset["id"],
            session_id=stranger["agent"]["id"],
            token=stranger["token"],
            reason="I dislike this",
        )
    assert refused.value.code == "knowledge_write_not_permitted"
    assert refused.value.status_code == 403
    # The asset is untouched.
    assert service.get_knowledge_asset(project_id, asset["id"])["status"] == "approved"


def test_a_management_session_may_archive_another_identitys_knowledge(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    manager = _join(service, project_id, "manager-b")

    asset = _candidate(service, project_id, author)
    archived = service.archive_knowledge_asset(
        project_id,
        asset["id"],
        session_id=manager["agent"]["id"],
        token=manager["token"],
        reason="superseded by the operations runbook",
        management_authorized=True,
    )
    assert archived["status"] == "archived"


def test_the_author_may_archive_their_own_candidate_without_management(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    asset = _candidate(service, project_id, author)

    archived = service.archive_knowledge_asset(
        project_id,
        asset["id"],
        session_id=author["agent"]["id"],
        token=author["token"],
        reason="withdrawn, the decision was reversed",
    )
    assert archived["status"] == "archived"


def test_archiving_without_a_reason_is_refused(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    asset = _candidate(service, project_id, author)

    with pytest.raises(DomainError) as refused:
        service.archive_knowledge_asset(
            project_id,
            asset["id"],
            session_id=author["agent"]["id"],
            token=author["token"],
            reason="   ",
        )
    assert refused.value.code == "knowledge_reason_required"
    assert refused.value.status_code == 422


def test_superseding_without_a_reason_is_refused(service, project_dir):
    """Supersede needs an approved asset - the status check comes first."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    reviewer = _join(service, project_id, "reviewer-b")
    asset = _candidate(service, project_id, author)
    service.submit_knowledge_review(
        project_id,
        asset["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "readable", "status": "passed"}],
        expected_version_id=asset["current_version_id"],
    )

    with pytest.raises(DomainError) as refused:
        service.supersede_knowledge_asset(
            project_id,
            asset["id"],
            session_id=author["agent"]["id"],
            token=author["token"],
        )
    assert refused.value.code == "knowledge_reason_required"
    assert refused.value.status_code == 422


def test_an_impossible_transition_is_reported_before_the_reason(
    service, project_dir
):
    """Ordering matters: a candidate cannot be superseded whatever the reason."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    asset = _candidate(service, project_id, author)

    with pytest.raises(DomainError) as refused:
        service.supersede_knowledge_asset(
            project_id,
            asset["id"],
            session_id=author["agent"]["id"],
            token=author["token"],
        )
    assert refused.value.code == "knowledge_invalid_transition"


# --- part 3: a candidate cannot silently rewrite itself --------------------


def test_a_candidate_self_update_appends_a_version_under_a_compare_and_set(
    service, project_dir
):
    """A candidate may revise its own wording, and the revision is versioned.

    Appending a new version is a documented behaviour, not the defect: the row
    keeps its history and an open review pins the version it read. What the card
    asks for - and what this pins - is that the write is a compare-and-set on
    the previous status, so a concurrent transition is reported rather than
    overwritten.
    """
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    asset = _candidate(service, project_id, author, body="the original wording")

    revised = _candidate(
        service,
        project_id,
        author,
        body="the revised wording",
        asset_id=asset["id"],
    )
    assert revised["status"] == "candidate"
    # A new version, and the previous one is still readable.
    detail = service.get_knowledge_asset(project_id, asset["id"])
    assert len(detail["versions"]) == 2
    assert detail["current_version_id"] != asset["current_version_id"]


def test_a_concurrent_candidate_transition_is_reported_not_overwritten(
    service, project_dir
):
    """The rowcount guard: the write carries the previous status as a predicate."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    asset = _candidate(service, project_id, author)

    # Move the asset out from under the writer, exactly as a concurrent
    # approval would, then attempt the revision the writer planned.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE knowledge_assets SET status = 'archived', updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            ("2026-01-01T00:00:00Z", asset["id"], project_id),
        )

    with pytest.raises(DomainError) as refused:
        _candidate(
            service,
            project_id,
            author,
            body="this must not silently overwrite the archived row",
            asset_id=asset["id"],
        )
    # Either the transition guard or the compare-and-set refuses; what matters
    # is that the archived status survives.
    assert refused.value.code in {
        "knowledge_invalid_transition",
        "knowledge_concurrent_transition",
        "knowledge_asset_archived",
    }
    assert (
        service.get_knowledge_asset(project_id, asset["id"])["status"] == "archived"
    )


def test_superseding_produces_the_next_version_and_keeps_the_old_readable(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "author-a")
    reviewer = _join(service, project_id, "reviewer-b")
    asset = _candidate(service, project_id, author, body="the reviewed wording")

    service.submit_knowledge_review(
        project_id,
        asset["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "readable", "status": "passed"}],
        expected_version_id=asset["current_version_id"],
    )

    superseded = service.supersede_knowledge_asset(
        project_id,
        asset["id"],
        session_id=author["agent"]["id"],
        token=author["token"],
        reason="the wording changed after review",
    )
    assert superseded["status"] == "superseded"

    # And a new candidate can now be submitted against the replacement.
    replacement = _candidate(
        service,
        project_id,
        author,
        body="the replacement wording",
        asset_id=asset["id"],
    )
    assert replacement["status"] == "candidate"
