"""Task #268: member roster writes are identity decisions.

`member:write` ships inside the default Agent credential bundle, so passing an
arbitrary member_id through MCP or REST let any Agent rename, rewrite or
irrevocably revoke somebody else's durable software identity - and after #236
that also tears down their sessions, tokens and leases.
"""

from __future__ import annotations

import pytest
from dataclasses import replace
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.errors import DomainError


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


def test_one_agent_cannot_revoke_another_identity(service, project, joined_agents):
    first, second = joined_agents

    with pytest.raises(DomainError) as refused:
        service.revoke_project_member(
            project["id"],
            second["agent"]["member_id"],
            actor_session_id=first["agent"]["id"],
            token=first["token"],
        )
    assert refused.value.code == "management_auth_required"
    assert refused.value.details["required_action"] == "use_management_identity"
    members = {
        member["id"]: member["status"]
        for member in service.list_project_members(project["id"])
    }
    assert members[second["agent"]["member_id"]] == "active"


def test_one_agent_cannot_rewrite_another_identity(service, project, joined_agents):
    first, second = joined_agents

    with pytest.raises(DomainError) as refused:
        service.update_project_member(
            project["id"],
            second["agent"]["member_id"],
            name="Impersonated",
            metadata={"software_key": "stolen-key"},
            actor_session_id=first["agent"]["id"],
            token=first["token"],
        )
    assert refused.value.code == "management_auth_required"
    stored = next(
        member
        for member in service.list_project_members(project["id"])
        if member["id"] == second["agent"]["member_id"]
    )
    assert stored["name"] != "Impersonated"


def test_an_agent_may_still_maintain_its_own_member_record(service, project, joined_agents):
    first, _ = joined_agents

    updated = service.update_project_member(
        project["id"],
        first["agent"]["member_id"],
        role="integrator",
        actor_session_id=first["agent"]["id"],
        token=first["token"],
    )
    assert updated["member"]["role"] == "integrator"


def test_missing_credentials_are_not_management_authority(service, project, joined_agents):
    _, second = joined_agents

    with pytest.raises(DomainError) as refused:
        service.update_project_member(
            project["id"], second["agent"]["member_id"], role="reviewer"
        )
    assert refused.value.code == "management_auth_required"


def test_management_may_revoke_another_identity(service, project, joined_agents):
    _, second = joined_agents

    result = service.revoke_project_member(
        project["id"], second["agent"]["member_id"], management_authorized=True
    )
    assert result["member"]["status"] == "revoked"


def test_rest_member_writes_require_management(settings, tmp_path, monkeypatch):
    guarded = replace(settings, management_auth_required=True)
    monkeypatch.setenv(guarded.management_token_env, "t" * 32)
    from agentchatroom.database import Database
    from agentchatroom.services import AgentChatRoomService

    service = AgentChatRoomService(Database(guarded.database_path), guarded)
    service.initialize()
    project = service.create_project(root_path=str(tmp_path))
    agent = _join(service, project["id"], "worker-a")

    with TestClient(create_app(guarded)) as client:
        revoked = client.delete(
            f"/api/v1/projects/{project['id']}/members/{agent['agent']['member_id']}"
        )
        patched = client.patch(
            f"/api/v1/projects/{project['id']}/members/{agent['agent']['member_id']}",
            json={"role": "reviewer"},
        )

    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "management_auth_required"
    assert patched.status_code == 401
    # An Agent maintaining its own roster entry is served by the domain rule,
    # which test_an_agent_may_still_maintain_its_own_member_record covers; the
    # management-guarded REST surface stays reserved for operators.
