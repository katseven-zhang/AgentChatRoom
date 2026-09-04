from __future__ import annotations

from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.mcp_server import task_history
from agentchatroom.task_history import history_detail_kind, project_history_item
from tests.conftest import join_room_with_workspace


def test_history_unknown_event_type_degrades_without_secrets():
    item = project_history_item(
        {
            "id": 9,
            "event_type": "future.unknown_event",
            "created_at": "2026-09-04T00:00:00Z",
            "task_id": "task_x",
            "task_number": 1,
            "channel": "system",
            "payload": {"token": "secret-token-value", "note": "ok"},
        },
        actor={"name": "unknown", "client": "unknown", "role": "unknown"},
        related={},
        acknowledgements=[],
    )
    assert item["detail_kind"] == "unknown"
    assert item["event_id"] == 9
    assert "secret-token-value" not in str(item)
    assert "token" not in item["payload"]


def test_task_history_projects_review_message_and_report_evidence(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Traceable history",
        acceptance_criteria=["Evidence is visible", "Decision body is visible"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.update_task(
        project["id"],
        task["id"],
        status="in_progress",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    decision = service.post_message(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        body="Must keep the original decision text.",
        model_display_name="test-model",
        kind="decision",
        channel="task",
        task_id=task["id"],
        requires_ack=True,
        priority=1,
    )
    service.acknowledge_event(
        project["id"],
        decision["event_id"],
        session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Ready for review",
        files=["src/history.py"],
        tests=[
            {"command": "pytest tests/test_task_history.py", "exit_code": 0},
            {"command": "failing", "exit_code": 1, "notes": "boom"},
        ],
    )
    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="changes_requested",
        criteria=[
            {
                "criterion": "Evidence is visible",
                "status": "failed",
                "evidence": "Need the decision body",
            },
            {
                "criterion": "Decision body is visible",
                "status": "passed",
                "evidence": "ok",
            },
        ],
        notes="Please show the original text.",
    )

    page = service.list_task_history(project["id"], task["id"], limit=50)
    types = [item["event_type"] for item in page["items"]]
    assert "message.decision" in types
    assert "review.submitted" in types
    assert "work.reported" in types
    decision_item = next(
        item for item in page["items"] if item["event_type"] == "message.decision"
    )
    assert decision_item["model_display_name"] == "test-model"
    assert any(
        "Must keep the original decision text." in str(section.get("items"))
        for section in decision_item["evidence_sections"]
    )
    assert decision_item["requires_ack"] is True
    assert decision_item["acknowledgements"]
    assert decision_item["actor"]["name"] == "Builder"
    review_item = next(
        item for item in page["items"] if item["event_type"] == "review.submitted"
    )
    assert review_item["verdict"] == "changes_requested"
    criteria = next(
        section
        for section in review_item["evidence_sections"]
        if section["kind"] == "criteria"
    )
    assert any(item["status"] == "failed" for item in criteria["items"])
    report_item = next(
        item for item in page["items"] if item["event_type"] == "work.reported"
    )
    tests = next(
        section
        for section in report_item["evidence_sections"]
        if section["kind"] == "tests"
    )
    assert any(item["exit_code"] == 1 for item in tests["items"])
    assert "token" not in str(page)

    first = service.list_task_history(project["id"], task["id"], limit=2)
    assert len(first["items"]) == 2
    older = service.list_task_history(
        project["id"], task["id"], before=first["items"][0]["event_id"], limit=20
    )
    newer = service.list_task_history(
        project["id"], task["id"], after=first["items"][-1]["event_id"], limit=20
    )
    assert all(
        item["event_id"] < first["items"][0]["event_id"] for item in older["items"]
    )
    assert all(
        item["event_id"] > first["items"][-1]["event_id"] for item in newer["items"]
    )


def test_task_history_keeps_revoked_member_name_and_missing_relations(service, project):
    joined = join_room_with_workspace(
        service,
        project,
        name="Soon Revoked",
        client="codex",
        model="test-model",
    )
    task = service.create_task(
        project["id"],
        title="Keep identity",
        acceptance_criteria=["Name survives revoke"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )
    member_id = joined["agent"]["member_id"]
    service.revoke_project_member(project["id"], member_id)
    page = service.list_task_history(project["id"], task["id"], limit=20)
    claimed = next(item for item in page["items"] if item["event_type"] == "task.claimed")
    assert claimed["actor"]["name"] == "Soon Revoked"
    assert claimed["actor"]["member_status"] == "revoked"

    orphan = project_history_item(
        {
            "id": 99,
            "event_type": "review.submitted",
            "created_at": "2026-09-04T00:00:00Z",
            "task_id": task["id"],
            "payload": {"review_id": "review_missing", "verdict": "approved", "notes": "gone"},
        },
        actor={"name": "unknown"},
        related={},
        acknowledgements=[],
    )
    assert orphan["verdict"] == "approved"
    assert orphan["related_ids"]["review_id"] == "review_missing"


def test_task_history_rest_mcp_and_detail_kind_alignment(
    monkeypatch, settings, service, project, joined_agents
):
    from agentchatroom import mcp_server

    assert history_detail_kind("review.submitted") == "review"
    executor, _reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Adapter history",
        acceptance_criteria=["Shared contract"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    domain = service.list_task_history(project["id"], task["id"], limit=20)
    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/history?limit=20"
        )
        assert response.status_code == 200
        rest = response.json()
    monkeypatch.setattr(mcp_server, "service", service)
    mcp = task_history(project_id=project["id"], task_id=task["id"], limit=20)
    assert rest["total"] == domain["total"]
    assert mcp["ok"] is True
    assert [item["event_id"] for item in rest["items"]] == [
        item["event_id"] for item in domain["items"]
    ]
    assert [item["event_id"] for item in mcp["result"]["items"]] == [
        item["event_id"] for item in domain["items"]
    ]
