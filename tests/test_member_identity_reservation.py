"""Task #344: the ``software:`` member-key namespace belongs to the backend.

A member row carrying that key is adopted as a durable software identity the next
time the matching client joins, so pre-staking one handed out somebody else's
persistent identity (shared member_id, forged display name, and therefore legal
cross-identity task reclaim). Renaming any member onto an existing identity name
used to bypass the duplicate-name guard joins already honour.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.errors import DomainError
from agentchatroom.services import SOFTWARE_MEMBER_PREFIX
from tests.conftest import join_room_with_workspace


def _join_codex(service, project, **overrides):
    values = {
        "agent_key": "",
        "software_key": "codex",
        "name": "Codex",
        "client": "codex",
        "model": "test-model",
        "role": "executor",
    }
    values.update(overrides)
    return join_room_with_workspace(service, project, **values)


def _stake_reserved_row(service, project, *, member_key, name, kind, metadata):
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            INSERT INTO project_members(
                id, project_id, member_key, name, kind, role, status,
                metadata_json, created_at, updated_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, '', 'active', ?, ?, ?, NULL)
            """,
            (
                f"member_staked_{member_key.replace(':', '_')}",
                project["id"],
                member_key,
                name,
                kind,
                __import__("json").dumps(metadata),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )


@pytest.mark.parametrize(
    "member_key",
    [
        f"{SOFTWARE_MEMBER_PREFIX}codex",
        f"{SOFTWARE_MEMBER_PREFIX.upper()}codex",
        f"  {SOFTWARE_MEMBER_PREFIX}evil  ",
    ],
)
def test_member_create_rejects_the_reserved_namespace(
    service, project, member_key
):
    with pytest.raises(DomainError) as error:
        service.create_project_member(
            project["id"],
            member_key=member_key,
            name="Stake",
            kind="agent",
        )
    assert error.value.code == "reserved_member_key"


def test_member_create_still_accepts_an_ordinary_key(service, project):
    created = service.create_project_member(
        project["id"], member_key="zcode-local", name="Z Code", kind="agent"
    )
    assert created["member"]["member_key"] == "zcode-local"


def test_join_refuses_to_adopt_a_pre_staked_reserved_row(service, project):
    _stake_reserved_row(
        service,
        project,
        member_key=f"{SOFTWARE_MEMBER_PREFIX}codex",
        name="Codex Impostor",
        kind="agent",
        metadata={},
    )

    with pytest.raises(DomainError) as error:
        _join_codex(service, project)

    assert error.value.code == "software_identity_unmanaged"
    assert error.value.details["member_id"] == "member_staked_software_codex"
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM agent_sessions"
        ).fetchone()["total"] == 0


def test_join_refuses_a_managed_row_without_a_client_binding(service, project):
    _stake_reserved_row(
        service,
        project,
        member_key=f"{SOFTWARE_MEMBER_PREFIX}codex",
        name="Codex",
        kind="software_agent",
        metadata={"managed_identity": True},
    )

    with pytest.raises(DomainError) as error:
        _join_codex(service, project)

    assert error.value.code == "software_identity_client_unbound"


def test_the_issuing_path_still_creates_a_joinable_identity(service, project):
    joined = _join_codex(service, project)

    assert joined["agent"]["client"] == "codex"
    assert joined["identity"]["member_key"] == f"{SOFTWARE_MEMBER_PREFIX}codex"

    again = _join_codex(service, project)
    assert again["identity"]["member_key"] == joined["identity"]["member_key"]


def test_rename_onto_an_existing_identity_name_is_rejected(service, project):
    joined = _join_codex(service, project)
    other = service.create_project_member(
        project["id"], member_key="tool-operator", name="Tool", kind="agent"
    )

    with pytest.raises(DomainError) as error:
        service.update_project_member(
            project["id"],
            other["member"]["id"],
            name="Codex",
            kind="software_agent",
            management_authorized=True,
        )

    assert error.value.code == "software_identity_name_conflict"
    assert error.value.details["conflicting_member_id"] == joined["identity"]["id"]


def test_rename_to_a_distinct_name_is_still_allowed(service, project):
    _join_codex(service, project)
    other = service.create_project_member(
        project["id"], member_key="tool-operator", name="Tool", kind="agent"
    )

    updated = service.update_project_member(
        project["id"],
        other["member"]["id"],
        name="Sponsorship Tool",
        kind="software_agent",
        management_authorized=True,
    )

    assert updated["member"]["name"] == "Sponsorship Tool"


def test_management_may_issue_a_managed_identity_row(service, project):
    created = service.create_project_member(
        project["id"],
        member_key=f"{SOFTWARE_MEMBER_PREFIX}codex",
        name="Codex",
        kind="software_agent",
        metadata={
            "managed_identity": True,
            "software_key": "codex",
            "client": "codex",
        },
        management_authorized=True,
    )

    joined = _join_codex(service, project)
    assert joined["identity"]["id"] == created["member"]["id"]


def test_management_must_still_issue_the_managed_shape(service, project):
    with pytest.raises(DomainError) as error:
        service.create_project_member(
            project["id"],
            member_key=f"{SOFTWARE_MEMBER_PREFIX}zcode",
            name="Z Code",
            kind="agent",
            metadata={},
            management_authorized=True,
        )
    assert error.value.code == "software_identity_unmanaged"


def test_rest_member_create_enforces_the_managed_shape(settings, service, project):
    # Loopback REST calls already are the management surface (see _require_management),
    # so what the boundary has to guarantee is that a reserved key cannot be
    # created without the managed software-identity shape.
    with TestClient(create_app(settings)) as client:
        response = client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={
                "member_key": f"{SOFTWARE_MEMBER_PREFIX}codex",
                "name": "Stake Without Shape",
                "kind": "agent",
                "metadata": {},
            },
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "software_identity_unmanaged"
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM project_members"
        ).fetchone()["total"] == 0
