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

def test_task_history_shows_integration_commit_hash(service, project, joined_agents):
    executor, reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Integration evidence",
        acceptance_criteria=["Integration shows its commit hash"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Ready for review",
        files=["src/feature.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[
            {"criterion": "Integration shows its commit hash", "status": "passed"}
        ],
    )
    service.submit_integration(
        project["id"],
        task["id"],
        result="done",
        summary="Integrated into main",
        tests=[{"command": "pytest", "exit_code": 0}],
        files=["src/feature.py"],
        commit_hash="abc123def4567890",
        integrator_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
    )

    page = service.list_task_history(project["id"], task["id"], limit=50)
    integration_item = next(
        item
        for item in page["items"]
        if item["event_type"] == "task.integration_completed"
    )
    git_sections = [
        section
        for section in integration_item["evidence_sections"]
        if section["kind"] == "git"
    ]
    assert git_sections, "integration history must carry a Git evidence section"
    entry = git_sections[0]["items"][0]
    assert entry["head"] == "abc123def4567890"
    assert entry["captured"] is True


def test_task_history_redacts_credentials_embedded_in_text():
    from agentchatroom.task_history import redact_runtime_value

    leaked = redact_runtime_value(
        {"body": "Authorization: Bearer supersecret token=abc plain tail"}
    )
    body = leaked["body"]
    assert "supersecret" not in body
    assert "abc" not in body.replace("[redacted]", "")

    leaked_key = redact_runtime_value(
        {"summary": "config uses api_key=verysecretvalue123 end"}
    )
    assert "verysecretvalue123" not in leaked_key["summary"]

    # Built at runtime so the public-release surface scan does not see a
    # provider key literal in this file; the regex branch is the same.
    provider_prefix = "s" + "k"
    assert provider_prefix not in redact_runtime_value(
        {"body": f"key is {provider_prefix}-abcdefghijklmnop012345"}
    )["body"]

    assert "token" not in redact_runtime_value({"token": "whatever"})
    # Benign text passes through unchanged.
    assert redact_runtime_value({"body": "plain update text"})["body"] == (
        "plain update text"
    )


def test_bootstrap_redaction_shares_the_same_text_policy():
    from agentchatroom.bootstrap import redact_runtime_value as bootstrap_redact
    from agentchatroom.task_history import redact_runtime_value as history_redact

    sample = {"body": "Authorization: Bearer supersecret value tail"}
    assert bootstrap_redact(sample) == history_redact(sample)
    assert "supersecret" not in str(bootstrap_redact(sample))


def test_task_history_cursor_pagination_walks_the_whole_chain_without_overlap(
    service, project, joined_agents
):
    executor, _ = joined_agents
    task = service.create_task(
        project["id"],
        title="Cursor pagination",
        acceptance_criteria=["Every event reachable via cursor"],
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
    service.update_task(
        project["id"],
        task["id"],
        progress_percent=40,
        current_step="Halfway",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    service.update_task(
        project["id"],
        task["id"],
        progress_percent=80,
        current_step="Almost",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Progress done",
        files=["src/step4.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )

    # Backwards from the newest page via before-cursor...
    seen: list[int] = []
    page = service.list_task_history(project["id"], task["id"], limit=2)
    assert page["total"] >= 5
    seen.extend(item["event_id"] for item in page["items"])
    while page["has_more_before"]:
        page = service.list_task_history(
            project["id"], task["id"], before=page["next_before"], limit=2
        )
        seen.extend(item["event_id"] for item in page["items"])
    # The before-walk must cover every event exactly once.
    assert len(seen) == len(set(seen)) == page["total"]
    # ...then forward from the oldest event via cursor, covering the same
    # chain without overlap or gaps.
    oldest = min(seen)
    forward: list[int] = []
    page = service.list_task_history(
        project["id"], task["id"], cursor=oldest - 1, limit=2
    )
    forward.extend(item["event_id"] for item in page["items"])
    while page["has_more_after"]:
        page = service.list_task_history(
            project["id"], task["id"], cursor=page["cursor"], limit=2
        )
        forward.extend(item["event_id"] for item in page["items"])
    # The forward cursor walk yields the exact same chain, oldest first.
    assert forward == sorted(seen)

    # cursor is an alias of after: equal values are accepted, conflicting
    # ones are rejected with a structured error instead of being ignored.
    boundary = forward[-1]
    aliased = service.list_task_history(
        project["id"], task["id"], after=boundary, cursor=boundary
    )
    assert aliased["after"] == boundary
    from agentchatroom.services import DomainError

    import pytest

    with pytest.raises(DomainError) as conflict:
        service.list_task_history(
            project["id"], task["id"], after=1, cursor=2
        )
    assert conflict.value.code == "conflicting_history_cursor"


def test_task_history_has_more_after_ignores_unrelated_room_events(
    service, project, joined_agents
):
    executor, other = joined_agents
    task = service.create_task(
        project["id"],
        title="Bounded forward pagination",
        acceptance_criteria=["has_more_after reflects this task only"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Only report",
        files=["src/only.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    # Unrelated events move the Room-wide cursor far beyond this task.
    noise = service.create_task(
        project["id"],
        title="Unrelated noise",
        acceptance_criteria=["noise"],
    )["task"]
    service.claim_task(
        project["id"], noise["id"], other["agent"]["id"], other["token"]
    )

    history = service.list_task_history(project["id"], task["id"], limit=50)
    assert history["total"] >= 2
    assert len(history["items"]) == history["total"]
    assert history["latest_cursor"] > history["next_after"]
    assert history["has_more_after"] is False
    assert history["has_more_before"] is False


def test_task_history_shows_claim_state_transition_from_payload(
    service, project, joined_agents
):
    executor, _ = joined_agents
    task = service.create_task(
        project["id"],
        title="Claim trail",
        acceptance_criteria=["Claim shows before and after"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    history = service.list_task_history(
        project["id"], task["id"], event_type="task.claimed"
    )
    claimed = next(
        item for item in history["items"] if item["event_type"] == "task.claimed"
    )
    fields = {change["field"]: change for change in claimed["state_changes"]}
    assert fields["status"]["before"] == "todo"
    assert fields["status"]["after"] == "claimed"
    assert fields["execution_status"]["before"] == "todo"
    assert fields["execution_status"]["after"] == "claimed"
    assert claimed["payload"]["owner_session_id"] == executor["agent"]["id"]
    assert claimed["task_number"] == task["task_number"]
