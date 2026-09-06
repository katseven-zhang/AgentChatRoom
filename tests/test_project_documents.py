from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def test_document_versions_are_immutable_and_audited(service, project):
    first = service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="工程规范", content="v1", actor="management",
    )
    second = service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="工程规范", content="v2", actor="management",
    )
    assert second["document"]["version"] == 2
    assert len(second["document"]["history"]) == 2

    old = service.get_project_document(project["id"], "standards", version=1)
    assert old["document"]["content"] == "v1"

    created = service.query_audit(project["id"], event_type="document.created")["events"]
    updated = service.query_audit(project["id"], event_type="document.updated")["events"]
    assert created and updated

    archived = service.archive_project_document(project["id"], "standards", actor="management")
    assert archived["document"]["archived_at"]
    listed = service.list_project_documents(project_id=project["id"])["documents"]
    assert listed == []
    with_archived = service.list_project_documents(project_id=project["id"], include_archived=True)
    assert len(with_archived["documents"]) == 1

    with pytest.raises(DomainError) as bad_kind:
        service.upsert_project_document(
            project["id"], doc_key="x", kind="other", title="t", content="c"
        )
    assert bad_kind.value.code == "invalid_project_document_kind"


def test_task_claim_injects_binding_documents_and_stamps_receipt(service, project, joined_agents):
    executor, _reviewer = joined_agents
    service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding",
        title="工程规范", content="先取租约，再改文件", actor="management",
    )
    service.upsert_project_document(
        project["id"], doc_key="design", kind="reference",
        title="设计说明", content="参考用", actor="management",
    )
    task = service.create_task(project["id"], title="T", acceptance_criteria=["c1"])["task"]
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


def test_inject_limit_degrades_large_documents(service, project):
    service.upsert_project_document(
        project["id"], doc_key="small", kind="binding", title="S", content="tiny", actor="management",
    )
    service.upsert_project_document(
        project["id"], doc_key="large", kind="binding", title="L", content="x" * 5000, actor="management",
    )
    import dataclasses
    from agentchatroom.config import Settings

    object.__setattr__(service.settings, "project_doc_inject_max_chars", 200)
    result = service.injectable_project_documents(project["id"])
    by_key = {doc["doc_key"]: doc for doc in result["documents"]}
    assert by_key["small"]["content"] == "tiny"
    assert by_key["large"]["content"] is None
    assert "project_document_get" in by_key["large"]["hint"]


def test_document_rest_surfaces(settings, project_dir):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir), "name": "Docs"}
        ).json()
        created = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            json={"doc_key": "standards", "kind": "binding", "title": "规范", "content": "内容"},
        )
        assert created.status_code == 200
        listing = client.get(f"/api/v1/projects/{project['id']}/documents")
        assert listing.status_code == 200
        assert listing.json()["documents"][0]["doc_key"] == "standards"
        detail = client.get(f"/api/v1/projects/{project['id']}/documents/standards")
        assert detail.status_code == 200
        assert detail.json()["document"]["content"] == "内容"
        board = client.get(f"/api/v1/projects/{project['id']}/tasks")
        assert board.status_code == 200


def test_mcp_project_document_upsert_success_and_history(service, project, joined_agents):
    executor, _ = joined_agents
    from agentchatroom.mcp_server import (
        _bound_service_provider,
        project_document_get,
        project_document_list,
        project_document_upsert,
    )

    token = _bound_service_provider.set(lambda: service)
    try:
        res1 = project_document_upsert(
            project_id=project["id"],
            doc_key="guidelines",
            kind="binding",
            title="开发规范",
            content="首要规则：先写测试",
        )
        assert res1["ok"] is True
        doc1 = res1["result"]["document"]
        assert doc1["version"] == 1
        assert doc1["doc_key"] == "guidelines"
        assert doc1["created_by"] == "agent"

        # Update to version 2
        res2 = project_document_upsert(
            project_id=project["id"],
            doc_key="guidelines",
            kind="binding",
            title="开发规范",
            content="首要规则：先写测试，再写实现",
        )
        assert res2["ok"] is True
        doc2 = res2["result"]["document"]
        assert doc2["version"] == 2
        assert len(doc2["history"]) == 2

        # Check project_document_get
        get_latest = project_document_get(project_id=project["id"], doc_key="guidelines")
        assert get_latest["ok"] is True
        assert get_latest["result"]["document"]["content"] == "首要规则：先写测试，再写实现"

        get_v1 = project_document_get(project_id=project["id"], doc_key="guidelines", version=1)
        assert get_v1["ok"] is True
        assert get_v1["result"]["document"]["content"] == "首要规则：先写测试"

        # Check audit events
        created_events = service.query_audit(project["id"], event_type="document.created")["events"]
        updated_events = service.query_audit(project["id"], event_type="document.updated")["events"]
        assert any(e["payload"]["doc_key"] == "guidelines" and e["payload"]["actor"] == "agent" for e in created_events)
        assert any(e["payload"]["doc_key"] == "guidelines" and e["payload"]["actor"] == "agent" for e in updated_events)

        # Check room_sync incremental visibility
        sync_res = service.room_sync(
            project["id"],
            session_id=executor["agent"]["id"],
            token=executor["token"],
            after=0,
        )
        event_types = [e["event_type"] for e in sync_res["events"]]
        assert "document.created" in event_types
        assert "document.updated" in event_types
    finally:
        _bound_service_provider.reset(token)


def test_mcp_project_document_upsert_validation_failures(service, project):
    from agentchatroom.mcp_server import _bound_service_provider, project_document_upsert

    token = _bound_service_provider.set(lambda: service)
    try:
        # Invalid kind
        res_kind = project_document_upsert(
            project_id=project["id"], doc_key="test-doc", kind="bad_kind", title="T", content="C"
        )
        assert res_kind["ok"] is False
        assert res_kind["error"]["code"] == "invalid_project_document_kind"

        # Invalid doc_key (path traversal)
        res_key1 = project_document_upsert(
            project_id=project["id"], doc_key="../evil", kind="binding", title="T", content="C"
        )
        assert res_key1["ok"] is False
        assert res_key1["error"]["code"] == "invalid_project_document_key"

        # Invalid doc_key (uppercase / invalid characters)
        res_key2 = project_document_upsert(
            project_id=project["id"], doc_key="Bad_Doc!", kind="binding", title="T", content="C"
        )
        assert res_key2["ok"] is False
        assert res_key2["error"]["code"] == "invalid_project_document_key"

        # Empty title
        res_title = project_document_upsert(
            project_id=project["id"], doc_key="test-doc", kind="binding", title="   ", content="C"
        )
        assert res_title["ok"] is False
        assert res_title["error"]["code"] == "invalid_project_document"

        # Empty content
        res_content = project_document_upsert(
            project_id=project["id"], doc_key="test-doc", kind="binding", title="T", content="   "
        )
        assert res_content["ok"] is False
        assert res_content["error"]["code"] == "invalid_project_document"
    finally:
        _bound_service_provider.reset(token)


def test_mcp_project_document_upsert_remote_token_authorization(service, project, monkeypatch):
    from mcp.server.auth.provider import AccessToken
    from agentchatroom.mcp_server import _bound_service_provider, project_document_upsert

    token = _bound_service_provider.set(lambda: service)
    try:
        # 1. Token without document:write permission
        fake_token_no_write = AccessToken(
            token="tok1",
            client_id="client1",
            scopes=["room:read", "message:write"],
            expires_at=9999999999,
            subject="agent1",
            claims={"project_id": project["id"]},
        )
        monkeypatch.setattr("agentchatroom.mcp_server.get_access_token", lambda: fake_token_no_write)
        res_forbidden = project_document_upsert(
            project_id=project["id"], doc_key="valid-key", kind="binding", title="T", content="C"
        )
        assert res_forbidden["ok"] is False
        assert res_forbidden["error"]["code"] == "agent_token_permission_forbidden"

        # 2. Token for a different project
        fake_token_wrong_project = AccessToken(
            token="tok2",
            client_id="client2",
            scopes=["document:write"],
            expires_at=9999999999,
            subject="agent2",
            claims={"project_id": "other_project_id"},
        )
        monkeypatch.setattr("agentchatroom.mcp_server.get_access_token", lambda: fake_token_wrong_project)
        res_wrong_proj = project_document_upsert(
            project_id=project["id"], doc_key="valid-key", kind="binding", title="T", content="C"
        )
        assert res_wrong_proj["ok"] is False
        assert res_wrong_proj["error"]["code"] == "agent_token_project_forbidden"

        # 3. Token with document:write permission for correct project
        fake_token_ok = AccessToken(
            token="tok3",
            client_id="client3",
            scopes=["document:write"],
            expires_at=9999999999,
            subject="agent3",
            claims={"project_id": project["id"]},
        )
        monkeypatch.setattr("agentchatroom.mcp_server.get_access_token", lambda: fake_token_ok)
        res_ok = project_document_upsert(
            project_id=project["id"], doc_key="valid-key", kind="binding", title="T", content="C"
        )
        assert res_ok["ok"] is True
    finally:
        _bound_service_provider.reset(token)
