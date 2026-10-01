"""Task #243: an explicit empty permission list must not mean "grant everything".

``issue_agent_token`` used ``permissions or DEFAULT_AGENT_PERMISSIONS``, and ``[]``
is falsy - so a caller who asked for a token with no permissions received all
eleven default grants including ``member:write``, ``task:write`` and
``lease:write``, with no error, while the sibling update path rejected the very
same input.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.errors import DomainError
from agentchatroom.services import DEFAULT_AGENT_PERMISSIONS


def _issued_permissions(service, project, **kwargs):
    issued = service.issue_agent_token(
        project["id"], name="Scoped token", **kwargs
    )
    return sorted(issued["credential"]["permissions"])


def test_an_empty_permission_list_is_rejected_not_escalated(service, project):
    with pytest.raises(DomainError) as error:
        _issued_permissions(service, project, permissions=[])

    assert error.value.code == "invalid_agent_permissions"
    assert error.value.details["required_action"] == (
        "pass_supported_permissions_or_omit_the_field"
    )
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM agent_credentials"
        ).fetchone()["total"] == 0


def test_omitting_permissions_still_grants_the_default_set(service, project):
    assert _issued_permissions(service, project) == sorted(
        DEFAULT_AGENT_PERMISSIONS
    )


def test_a_requested_subset_is_recorded_exactly_as_requested(service, project):
    issued = service.issue_agent_token(
        project["id"],
        name="Read only token",
        permissions=["room:read", "message:write"],
    )
    assert sorted(issued["credential"]["permissions"]) == [
        "message:write",
        "room:read",
    ]
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM events WHERE event_type = 'credential.issued' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert sorted(payload["permissions"]) == ["message:write", "room:read"]


def test_issue_and_update_reject_the_same_input_identically(service, project):
    issued = service.issue_agent_token(
        project["id"], name="Baseline", permissions=["room:read"]
    )
    credential_id = issued["credential"]["id"]

    with pytest.raises(DomainError) as on_issue:
        _issued_permissions(service, project, permissions=["room:read", "not:a:power"])
    with pytest.raises(DomainError) as on_update:
        service.update_agent_token_permissions(
            project["id"], credential_id, permissions=["room:read", "not:a:power"]
        )

    assert on_issue.value.code == on_update.value.code == "invalid_agent_permissions"

    with pytest.raises(DomainError) as empty_issue:
        _issued_permissions(service, project, permissions=[])
    with pytest.raises(DomainError) as empty_update:
        service.update_agent_token_permissions(
            project["id"], credential_id, permissions=[]
        )
    assert empty_issue.value.code == empty_update.value.code


def test_rest_issue_rejects_an_empty_permission_list(settings, service, project):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            f"/api/v1/projects/{project['id']}/agent-tokens",
            json={"name": "Escalation attempt", "permissions": []},
        )

    assert response.status_code in (400, 422), response.text
    assert response.json()["error"]["code"] == "invalid_agent_permissions"
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM agent_credentials"
        ).fetchone()["total"] == 0
