"""Task #334: a handoff is an ownership decision, not an anonymous request.

handoff_task used to treat "no credentials presented" as owner authority: it
borrowed the task's own owner session id, the ownership check then compared
that value with itself, and the audit recorded the caller as requested_by
"manager". The target's acknowledgement afterwards moved ownership and released
the real owner's leases.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


def _owned_task(service, project_id: str, owner):
    task = service.create_task(
        project_id,
        title="Hand me over",
        acceptance_criteria=["Handoff requires the owner's own credentials"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    service.claim_task(project_id, task["id"], owner["agent"]["id"], owner["token"])
    service.acquire_lease(
        project_id,
        session_id=owner["agent"]["id"],
        token=owner["token"],
        path_pattern="src/handoff-334.py",
        task_id=task["id"],
    )
    return task


def _handoff_events(service, project_id: str, task_id: str):
    return [
        event
        for event in service.query_audit(project_id, task_id=task_id)["events"]
        if event["event_type"] == "task.handoff_requested"
    ]


def test_anonymous_handoff_is_refused_without_management_authority(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join(service, project_id, "worker-a")
    task = _owned_task(service, project_id, owner)

    with pytest.raises(DomainError) as refused:
        service.handoff_task(
            project_id,
            task["id"],
            to_session_id=owner["agent"]["id"],
            summary="intruder attempt",
            next_step="pick up",
        )
    assert refused.value.code == "management_auth_required"
    assert refused.value.details["required_action"]
    live = service.get_task(project_id, task["id"])
    assert live["owner_session_id"] == owner["agent"]["id"]
    assert (live.get("handoffs") or []) == []


def test_a_different_session_cannot_hand_off_another_owner_task(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join(service, project_id, "worker-a")
    intruder = _join(service, project_id, "worker-b")
    task = _owned_task(service, project_id, owner)

    with pytest.raises(DomainError) as refused:
        service.handoff_task(
            project_id,
            task["id"],
            to_session_id=intruder["agent"]["id"],
            summary="borrowed authority",
            next_step="pick up",
            from_session_id=intruder["agent"]["id"],
            token=intruder["token"],
        )
    assert refused.value.code == "not_task_owner"
    assert "handoff_requires_current_owner_credentials" in str(refused.value.details)
    assert (service.get_task(project_id, task["id"]).get("handoffs") or []) == []


def test_owner_and_management_handoffs_record_the_real_initiator(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    owner = _join(service, project_id, "worker-a")
    target = _join(service, project_id, "worker-b")

    own_task = _owned_task(service, project_id, owner)
    result = service.handoff_task(
        project_id,
        own_task["id"],
        to_session_id=target["agent"]["id"],
        summary="owner hands over",
        next_step="continue",
        from_session_id=owner["agent"]["id"],
        token=owner["token"],
    )
    assert result["handoff"]["requested_by_session_id"] == owner["agent"]["id"]
    assert _handoff_events(service, project_id, own_task["id"])[0]["payload"][
        "requested_by"
    ] == "agent"

    second = _owned_task(service, project_id, owner)
    managed = service.handoff_task(
        project_id,
        second["id"],
        to_session_id=target["agent"]["id"],
        summary="operator reassigns",
        next_step="continue",
        management_authorized=True,
    )
    assert managed["handoff"]["requested_by_session_id"] is None
    assert _handoff_events(service, project_id, second["id"])[0]["payload"][
        "requested_by"
    ] == "manager"


def test_rest_handoff_requires_management_like_its_siblings(
    settings, tmp_path, monkeypatch
):
    """An uncredentialed REST handoff must hit the same management gate that
    release and assign use, instead of being honoured as the owner's own act."""
    guarded = replace(settings, management_auth_required=True)
    monkeypatch.setenv(guarded.management_token_env, "t" * 32)
    service = AgentChatRoomService(Database(guarded.database_path), guarded)
    service.initialize()
    project = service.create_project(root_path=str(tmp_path))
    owner = _join(service, project["id"], "worker-a")
    task = _owned_task(service, project["id"], owner)

    with TestClient(create_app(guarded)) as client:
        response = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/handoffs",
            json={
                "to_session_id": owner["agent"]["id"],
                "summary": "anonymous",
                "next_step": "continue",
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "management_auth_required"
