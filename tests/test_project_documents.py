from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _as_management(session) -> dict:
    """Credentials plus explicit management authority (#252)."""
    return {
        "session_id": session["agent"]["id"],
        "token": session["token"],
        "management_authorized": True,
    }


def test_document_versions_are_immutable_and_audited(
    service, project, joined_agents
):
    manager, _reviewer = joined_agents
    first = service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="工程规范", content="v1",
        **_as_management(manager),
    )
    second = service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="工程规范", content="v2",
        **_as_management(manager),
    )
    assert second["document"]["version"] == 2
    assert len(second["document"]["history"]) == 2

    old = service.get_project_document(project["id"], "standards", version=1)
    assert old["document"]["content"] == "v1"

    created = service.query_audit(project["id"], event_type="document.created")["events"]
    updated = service.query_audit(project["id"], event_type="document.updated")["events"]
    assert created and updated

    archived = service.archive_project_document(
        project["id"], "standards", **_as_management(manager)
    )
    assert archived["document"]["archived_at"]
    listed = service.list_project_documents(project_id=project["id"])["documents"]
    assert listed == []
    with_archived = service.list_project_documents(project_id=project["id"], include_archived=True)
    assert len(with_archived["documents"]) == 1

    with pytest.raises(DomainError) as bad_kind:
        service.upsert_project_document(
            project["id"], doc_key="x", kind="other", title="t", content="c",
            **_as_management(manager),
        )
    assert bad_kind.value.code == "invalid_project_document_kind"


def test_task_claim_injects_binding_documents_and_stamps_receipt(service, project, joined_agents):
    executor, manager = joined_agents
    service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="工程规范", content="先取租约，再改文件",
        **_as_management(manager),
    )
    service.upsert_project_document(
        project["id"], doc_key="design", kind="reference",
        title="设计说明", content="参考用",
        **_as_management(manager),
    )
    task = service.create_task(project["id"], title="T", acceptance_criteria=["c1"], actor_session_id=executor["agent"]["id"], token=executor["token"], )["task"]
    claimed = service.claim_task(
        project["id"], task["id"],
        session_id=executor["agent"]["id"], token=executor["token"],
    )
    injected = claimed["project_documents"]["documents"]
    assert injected[0]["doc_key"] == "standards"
    assert injected[0]["content"] == "先取租约，再改文件"
    assert all(doc["doc_key"] != "design" for doc in injected)

    claim_event = service.query_audit(
        project["id"], event_type="task.claimed", task_id=task["id"]
    )["events"][-1]
    assert claim_event["payload"]["project_documents"] == {"standards": 1}

    reported = service.submit_work_report(
        project["id"], task["id"],
        session_id=executor["agent"]["id"], token=executor["token"],
        summary="done",
        files=["src/app.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
        no_code_change_reason="",
    )
    report_event = service.query_audit(
        project["id"], event_type="work.reported", task_id=task["id"]
    )["events"][-1]
    assert report_event["payload"]["spec_receipt"] == {"standards": 1}

    # The task object itself carries the receipt so Web surfaces can render it.
    fetched = service.get_task(project["id"], task["id"])
    assert fetched["spec_receipt"] == {"standards": 1}
    board = service.list_tasks(project["id"])
    board_task = next(item for item in board if item["id"] == task["id"])
    assert board_task["spec_receipt"] == {"standards": 1}


def test_inject_limit_degrades_large_documents(service, project, joined_agents):
    manager, _reviewer = joined_agents
    service.upsert_project_document(
        project["id"], doc_key="small", kind="binding", title="S", content="tiny", **_as_management(manager),
    )
    service.upsert_project_document(
        project["id"], doc_key="large", kind="binding", title="L", content="x" * 5000, **_as_management(manager),
    )
    import dataclasses
    from agentchatroom.config import Settings

    object.__setattr__(service.settings, "project_doc_inject_max_chars", 200)
    result = service.injectable_project_documents(project["id"])
    by_key = {doc["doc_key"]: doc for doc in result["documents"]}
    assert by_key["small"]["content"] == "tiny"
    assert by_key["large"]["content"] is None
    assert "project_document_get" in by_key["large"]["hint"]


def test_document_rest_surfaces(settings, project_dir, monkeypatch):
    """The REST write is management-only (#252), so the test authenticates.

    Without a management session the route now answers 401, which is the point:
    a binding document is injected verbatim into every Agent's task response.
    """
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    admin_token = "test-management-token-with-adequate-length"
    monkeypatch.setenv("TEST_AGENTCHATROOM_ADMIN_TOKEN", admin_token)
    guarded = replace(
        settings,
        management_auth_required=True,
        management_token_env="TEST_AGENTCHATROOM_ADMIN_TOKEN",
    )

    with TestClient(create_app(guarded)) as client:
        # Creating a Project is management-only too, so log in first and let the
        # cookie carry the authority for both calls.
        management = client.post(
            "/api/v1/auth/login", json={"token": admin_token}
        )
        assert management.status_code == 200, management.text
        assert client.get("/api/v1/auth/status").json()["authenticated"] is True

        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir), "name": "Docs"}
        ).json()
        created = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            json={"doc_key": "standards", "kind": "binding", "title": "规范", "content": "内容"},
        )
        assert created.status_code == 200, created.text
        listing = client.get(f"/api/v1/projects/{project['id']}/documents")
        assert listing.status_code == 200
        assert listing.json()["documents"][0]["doc_key"] == "standards"
        detail = client.get(f"/api/v1/projects/{project['id']}/documents/standards")
        assert detail.status_code == 200
        assert detail.json()["document"]["content"] == "内容"
        board = client.get(f"/api/v1/projects/{project['id']}/tasks")
        assert board.status_code == 200


def test_mcp_document_upsert_is_management_only(service, project, joined_agents):
    """#252: the tool surface must not imply an Agent can write a document.

    A binding document is injected verbatim into every Agent's task response, so
    an Agent that could write one could hand every other Agent instructions of
    its own choosing. The management surface is REST; this tool reports the
    refusal instead of pretending the permission exists.
    """
    from agentchatroom import mcp_server

    executor, _ = joined_agents
    outcome = mcp_server.project_document_upsert(
        project_id=project["id"],
        doc_key="standards",
        kind="binding",
        title="工程规范",
        content="先取租约",
    )
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "management_auth_required"
    assert outcome["error"]["details"]["required_action"]

    # Nothing was written, and the call left no event behind.
    with pytest.raises(DomainError) as missing:
        service.get_project_document(project["id"], "standards")
    assert missing.value.code == "project_document_not_found"
    created = service.query_audit(project["id"], event_type="document.created")
    assert created["events"] == []


def test_mcp_document_upsert_refuses_before_validating_its_arguments(
    service, project, joined_agents
):
    """Authorization first: an invalid kind must not read as a validation path.

    Answering "that kind is wrong" to a caller with no authority would confirm
    the surface exists and hand back a code to try elsewhere.
    """
    from agentchatroom import mcp_server

    outcome = mcp_server.project_document_upsert(
        project_id=project["id"],
        doc_key="x",
        kind="not-a-kind",
        title="t",
        content="c",
    )
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "management_auth_required"


def test_mcp_document_upsert_refuses_a_remote_token_too(
    service, project, joined_agents, monkeypatch
):
    """A remote token used to answer with a permission error; now it refuses.

    Either way the write does not happen. What matters is that the refusal is
    the authority one, so a caller is not told to go and find a permission that
    would actually work.
    """
    from agentchatroom import mcp_server

    monkeypatch.setenv("AGENTCHATROOM_REMOTE_AGENT_TOKEN", "remote-secret-token-value")
    outcome = mcp_server.project_document_upsert(
        project_id=project["id"],
        doc_key="standards",
        kind="binding",
        title="t",
        content="c",
    )
    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "management_auth_required"
