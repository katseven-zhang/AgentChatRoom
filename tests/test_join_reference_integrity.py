"""Task #244: an unresolvable join reference must be a refusal, not a 500.

``join_room`` only validated the reference it happened to receive as
``workspace_id``; a bare ``host_id`` went straight into the INSERT, where the
``hosts`` foreign key turned a typo into a raw ``sqlite3.IntegrityError`` (HTTP 500
plus a traceback), and the credential lookup only asked whether the row existed,
so a revoked or expired Token could still mint a Session.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _register_host(service, project, key="host-a", name="Host A"):
    return service.register_workspace(
        project["id"],
        host_key=key,
        host_name=name,
        local_path=project["root_path"],
    )


def _join(service, project, **kwargs):
    values = {"name": "Joiner", "client": "codex", "model": "test-model"}
    values.update(kwargs)
    return service.join_room(project["id"], **values)


def test_unknown_host_is_a_structured_404_and_writes_nothing(service, project):
    with pytest.raises(DomainError) as error:
        _join(service, project, host_id="host_that_never_existed")

    assert error.value.code == "host_not_found"
    assert error.value.status_code == 404
    assert error.value.details["required_action"] == (
        "register_workspace_or_omit_host_id"
    )
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM agent_sessions"
        ).fetchone()["total"] == 0


def test_workspace_and_host_disagreement_is_still_refused(service, project):
    first = _register_host(service, project, key="host-a", name="Host A")
    second = _register_host(service, project, key="host-b", name="Host B")

    with pytest.raises(DomainError) as error:
        _join(
            service,
            project,
            workspace_id=first["workspace"]["id"],
            host_id=second["host"]["id"],
        )

    assert error.value.status_code == 404


def test_a_valid_host_without_a_workspace_still_joins(service, project):
    host = _register_host(service, project)

    joined = _join(service, project, host_id=host["host"]["id"], role="executor")

    assert joined["agent"]["host_id"] == host["host"]["id"]


def test_a_revoked_credential_cannot_open_a_session(service, project):
    issued = service.issue_agent_token(project["id"], name="Retired token")
    service.revoke_agent_token(project["id"], issued["credential"]["id"])

    with pytest.raises(DomainError) as error:
        _join(service, project, credential_id=issued["credential"]["id"])

    assert "revoked" in error.value.code


def test_an_expired_credential_cannot_open_a_session(service, project):
    issued = service.issue_agent_token(
        project["id"], name="Stale token", expires_in_seconds=300
    )
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_credentials SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", issued["credential"]["id"]),
        )

    with pytest.raises(DomainError) as error:
        _join(service, project, credential_id=issued["credential"]["id"])

    assert "expired" in error.value.code
