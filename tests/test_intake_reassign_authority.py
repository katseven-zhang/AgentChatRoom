"""Task #275: reassigning an intake needs a real actor, and the audit must say who.

The REST route and the domain method both accepted an unauthenticated call, and
the emitted `task.intake_reassigned` event recorded no actor at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.errors import DomainError


def _intake(service, project, owner):
    return service.submit_task_intake(
        project["id"],
        raw_description="Reassign authority probe",
        target_member_id=owner["agent"]["member_id"],
        created_by_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["intake"]


def _event_for(service, project_id, intake_id):
    for event in service.list_events(project_id, after=0)["events"]:
        if event["event_type"] == "task.intake_reassigned":
            if (event["payload"] or {}).get("intake_id") == intake_id:
                return event
    return None


def test_anonymous_domain_reassign_is_rejected_and_changes_nothing(
    service, project, joined_agents
):
    owner, other = joined_agents
    intake = _intake(service, project, other)

    with pytest.raises(DomainError) as captured:
        service.reassign_task_intake(
            project["id"], intake["id"], target_member_id=owner["agent"]["member_id"]
        )

    assert captured.value.code == "management_auth_required"
    assert captured.value.details["required_action"]
    stored = service.get_task_intake(project["id"], intake["id"])
    assert stored["status"] == "pending"
    assert _event_for(service, project["id"], intake["id"]) is None


def test_management_authorized_reassign_records_management_as_the_actor(
    service, project, joined_agents
):
    owner, other = joined_agents
    intake = _intake(service, project, other)

    reassigned = service.reassign_task_intake(
        project["id"],
        intake["id"],
        target_member_id=owner["agent"]["member_id"],
        management_authorized=True,
    )
    event = _event_for(service, project["id"], intake["id"])

    assert reassigned["intake"]["status"] == "pending"
    assert event["payload"]["performed_by"] == "management"


def test_credentialed_reassign_records_the_acting_session(service, project, joined_agents):
    owner, other = joined_agents
    intake = _intake(service, project, other)

    service.reassign_task_intake(
        project["id"],
        intake["id"],
        target_member_id=owner["agent"]["member_id"],
        session_id=other["agent"]["id"],
        token=other["token"],
    )
    event = _event_for(service, project["id"], intake["id"])

    actor = event["payload"]["actor"]
    assert event["payload"]["performed_by"] == "agent_session"
    assert other["agent"]["id"] in actor.values()


def test_a_bogus_credential_is_not_treated_as_management_authority(
    service, project, joined_agents
):
    owner, other = joined_agents
    intake = _intake(service, project, other)

    with pytest.raises(DomainError) as captured:
        service.reassign_task_intake(
            project["id"],
            intake["id"],
            target_member_id=owner["agent"]["member_id"],
            session_id=other["agent"]["id"],
            token="not-the-real-token",
        )

    assert captured.value.code != "management_auth_required"
    assert captured.value.status_code in {401, 403}


def _seed_intake(client, project_dir):
    service = client.app.state.service
    project = service.create_project(root_path=str(project_dir), name="Authority Room")
    joined = service.join_room(
        project["id"], name="Worker", client="codex", model="test-model"
    )
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Needs an actor",
        target_member_id=joined["agent"]["member_id"],
        created_by_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["intake"]
    path = f"/api/v1/projects/{project['id']}/task-intakes/{intake['id']}/reassign"
    return service, project, joined, intake, path


def _client(tmp_path, **overrides):
    from agentchatroom.config import Settings

    return TestClient(create_app(Settings(data_dir=tmp_path / "data", **overrides)))


def test_rest_anonymous_reassign_is_rejected_when_management_is_enforced(
    tmp_path, project_dir, monkeypatch
):
    # Any token of the required length; the value is not a secret.
    monkeypatch.setenv("AGENTCHATROOM_ADMIN_TOKEN", "m" * 32)
    client = _client(tmp_path, management_auth_required=True)
    with client:
        service, project, joined, intake, path = _seed_intake(client, project_dir)

        response = client.post(
            path, json={"target_member_id": joined["agent"]["member_id"]}
        )

        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "management_auth_required"
        assert service.get_task_intake(project["id"], intake["id"])["status"] == "pending"


def test_rest_local_anonymous_reassign_is_audited_as_management(
    tmp_path, project_dir
):
    client = _client(tmp_path)
    with client:
        service, project, joined, _intake, path = _seed_intake(client, project_dir)

        response = client.post(
            path, json={"target_member_id": joined["agent"]["member_id"]}
        )

        assert response.status_code == 200, response.text
        events = service.list_events(project["id"], after=0)["events"]
        reassigned = [e for e in events if e["event_type"] == "task.intake_reassigned"]
        assert reassigned[-1]["payload"]["performed_by"] == "management"


def test_rest_credentialed_reassign_is_audited_to_the_session(tmp_path, project_dir):
    client = _client(tmp_path)
    with client:
        service, project, joined, _intake, path = _seed_intake(client, project_dir)

        response = client.post(
            path,
            json={
                "target_member_id": joined["agent"]["member_id"],
                "session_id": joined["agent"]["id"],
                "token": joined["token"],
            },
        )

        assert response.status_code == 200, response.text
        events = service.list_events(project["id"], after=0)["events"]
        reassigned = [e for e in events if e["event_type"] == "task.intake_reassigned"]
        assert reassigned[-1]["payload"]["performed_by"] == "agent_session"
        assert joined["agent"]["id"] in reassigned[-1]["payload"]["actor"].values()
