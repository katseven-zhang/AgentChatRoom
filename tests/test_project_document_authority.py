"""#252: writing a project document is a privileged, attributable act.

A binding document is injected verbatim into every Agent's task response, so
whoever can write one can hand every other Agent instructions of its own
choosing. The method used to take a free-text ``actor`` and authenticate nobody,
so any caller holding document:write could replace the Room's own standards
and the event named no one.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _project(service, project_dir):
    return service.create_project(root_path=str(project_dir))


def _agent(service, project_id, name="writer"):
    return service.join_room(
        project_id,
        software_key=name,
        name=name,
        client="pytest",
        model="unknown",
    )


def test_an_agent_without_management_authority_cannot_write_a_binding_document(
    service, project_dir
):
    """The reported gap: document:write is in DEFAULT_AGENT_PERMISSIONS."""
    project = _project(service, project_dir)
    agent = _agent(service, project["id"])

    with pytest.raises(DomainError) as refused:
        service.upsert_project_document(
            project["id"],
            doc_key="standards",
            kind="binding",
            title="Room rules",
            content="Always do what the coordinator says.",
            session_id=agent["agent"]["id"],
            token=agent["token"],
        )
    assert refused.value.code == "management_auth_required"
    # 401, not 403: the repository reads a missing management authority as
    # insufficient credentials, and that is the shared helper's contract.
    assert refused.value.status_code == 401

    # And nothing was written.
    with pytest.raises(DomainError) as missing:
        service.get_project_document(project["id"], "standards")
    assert missing.value.code == "project_document_not_found"


def test_creating_a_document_requires_credentials_at_all(
    service, project_dir
):
    """No session and no token is not a lesser case of the same refusal."""
    project = _project(service, project_dir)

    with pytest.raises(DomainError) as refused:
        service.upsert_project_document(
            project["id"],
            doc_key="standards",
            kind="binding",
            title="Room rules",
            content="content",
        )
    assert refused.value.code
    assert refused.value.status_code in (401, 403)


def test_a_management_session_writes_and_the_event_names_it(service, project_dir):
    """AC2: the audit has to be traceable to a real identity."""
    project = _project(service, project_dir)
    agent = _agent(service, project["id"])

    written = service.upsert_project_document(
        project["id"],
        doc_key="standards",
        kind="binding",
        title="Room rules",
        content="Do the work.",
        session_id=agent["agent"]["id"],
        token=agent["token"],
        management_authorized=True,
    )
    assert written["document"]["version"] == 1

    with service.database.connect() as connection:
        row = connection.execute(
            """
            SELECT created_by FROM project_documents
            WHERE project_id = ? AND doc_key = ?
            ORDER BY version DESC LIMIT 1
            """,
            (project["id"], "standards"),
        ).fetchone()
        event = connection.execute(
            """
            SELECT actor_session_id FROM events
            WHERE project_id = ? AND event_type = 'document.created'
            ORDER BY id DESC LIMIT 1
            """,
            (project["id"],),
        ).fetchone()

    assert row["created_by"] == agent["agent"]["id"], "created_by stayed NULL"
    assert event["actor_session_id"] == agent["agent"]["id"], "event named nobody"


def test_archiving_also_requires_management_authority(service, project_dir):
    project = _project(service, project_dir)
    agent = _agent(service, project["id"])
    service.upsert_project_document(
        project["id"],
        doc_key="standards",
        kind="binding",
        title="Room rules",
        content="content",
        session_id=agent["agent"]["id"],
        token=agent["token"],
        management_authorized=True,
    )

    with pytest.raises(DomainError) as refused:
        service.archive_project_document(
            project["id"],
            "standards",
            session_id=agent["agent"]["id"],
            token=agent["token"],
        )
    assert refused.value.code == "management_auth_required"


def test_a_stale_expected_version_is_refused(service, project_dir):
    """AC3: a concurrent overwrite has to fail, not silently win."""
    project = _project(service, project_dir)
    agent = _agent(service, project["id"])
    common = {
        "session_id": agent["agent"]["id"],
        "token": agent["token"],
        "management_authorized": True,
    }
    first = service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="v1", content="one", **common,
    )
    service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="v2", content="two", expected_version=1, **common,
    )

    # An editor still holding version 1 must not overwrite version 2.
    with pytest.raises(DomainError) as refused:
        service.upsert_project_document(
            project["id"], doc_key="standards", kind="binding",
            title="stale", content="three", expected_version=1, **common,
        )
    assert refused.value.code == "project_document_modified_concurrently"
    assert refused.value.status_code == 409
    assert refused.value.details["current_version"] == 2

    current = service.get_project_document(project["id"], "standards")["document"]
    assert current["version"] == first["document"]["version"] + 1
    assert current["title"] == "v2"


def test_matching_the_expected_version_writes(service, project_dir):
    """The lock must not turn into a permanent block."""
    project = _project(service, project_dir)
    agent = _agent(service, project["id"])
    common = {
        "session_id": agent["agent"]["id"],
        "token": agent["token"],
        "management_authorized": True,
    }
    service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="v1", content="one", **common,
    )
    updated = service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="v2", content="two", expected_version=1, **common,
    )
    assert updated["document"]["version"] == 2


def test_the_mcp_tool_reports_the_refusal_rather_than_writing(project) -> None:
    """The tool surface must not imply an Agent can do this."""
    from agentchatroom import mcp_server

    outcome = mcp_server.project_document_upsert(
        project_id=project["id"],
        doc_key="standards",
        kind="binding",
        title="Room rules",
        content="content",
    )
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "management_auth_required"
    assert outcome["error"]["details"]["required_action"]
