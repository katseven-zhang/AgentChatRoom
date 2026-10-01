"""Task #236: revoking a member must actually revoke its access.

The roster row is only a display. The credentials, the Sessions already joined and
the file leases they hold are what grant write authority, so a member that was
"removed" while keeping all three kept posting messages, claiming tasks and
renewing its own token and leases through the heartbeat forever.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


def _revoke(service, project_id: str, member_id: str):
    # No actor Session means the caller is the management surface, which is what
    # revokes a member in REST/MCP/CLI.
    return service.revoke_project_member(project_id, member_id)


def _setup(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "worker-a")
    credential = service.issue_agent_token(
        project_id, name="worker-a token", member_id=agent["agent"]["member_id"]
    )
    task = service.create_task(
        project_id,
        title="Owned by the revoked identity",
        acceptance_criteria=["Revocation ends write authority"],
        actor_session_id=agent["agent"]["id"],
        token=agent["token"],
    )["task"]
    service.claim_task(project_id, task["id"], agent["agent"]["id"], agent["token"])
    service.acquire_lease(
        project_id,
        session_id=agent["agent"]["id"],
        token=agent["token"],
        path_pattern="src/revoked.py",
        task_id=task["id"],
    )
    return project_id, agent, credential, task


def _session_row(service, project_id: str, session_id: str):
    with service.database.connect() as connection:
        return connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ? AND project_id = ?",
            (session_id, project_id),
        ).fetchone()


def test_revocation_tears_down_credentials_sessions_and_leases(service, project_dir):
    project_id, agent, _, task = _setup(service, project_dir)

    _revoke(service, project_id, agent["agent"]["member_id"])

    events = service.query_audit(project_id)["events"]
    revoke_event = next(
        event for event in events if event["event_type"] == "member.revoked"
    )
    counts = revoke_event["payload"]["access_revocation"]
    assert counts["credentials_revoked"] == 1
    assert counts["sessions_closed"] == 1
    assert counts["leases_released"] == 1
    assert {"credential.revoked", "session.leave"} <= {
        event["event_type"] for event in events
    }

    row = _session_row(service, project_id, agent["agent"]["id"])
    assert row["left_at"] is not None
    assert row["status"] != "online"
    # The task contract itself is append-only history and stays as it was.
    assert service.get_task(project_id, task["id"])["owner_session_id"] == agent[
        "agent"
    ]["id"]
    assert [
        lease["id"]
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    ] == []


@pytest.mark.parametrize(
    "call",
    [
        lambda service, pid, agent, task: service.post_message(
            pid,
            body="still writing",
            session_id=agent["agent"]["id"],
            token=agent["token"],
            model_display_name="unknown",
        ),
        lambda service, pid, agent, task: service.acquire_lease(
            pid,
            session_id=agent["agent"]["id"],
            token=agent["token"],
            path_pattern="src/another.py",
        ),
        lambda service, pid, agent, task: service.claim_task(
            pid, task["id"], agent["agent"]["id"], agent["token"]
        ),
        lambda service, pid, agent, task: service.submit_work_report(
            pid,
            task["id"],
            session_id=agent["agent"]["id"],
            token=agent["token"],
            summary="reported after revocation",
            files=["src/revoked.py"],
            tests=[{"command": "pytest", "exit_code": 0}],
        ),
        lambda service, pid, agent, task: service.submit_review(
            pid,
            task["id"],
            reviewer_session_id=agent["agent"]["id"],
            token=agent["token"],
            verdict="approved",
            criteria=[
                {"criterion": "Revocation ends write authority", "status": "passed"}
            ],
        ),
    ],
    ids=["message_post", "lease_acquire", "task_claim", "work_report", "review_submit"],
)
def test_a_revoked_identity_cannot_keep_writing(service, project_dir, call):
    project_id, agent, _, task = _setup(service, project_dir)
    _revoke(service, project_id, agent["agent"]["member_id"])

    with pytest.raises(DomainError) as refused:
        call(service, project_id, agent, task)
    assert refused.value.code in {
        "session_closed",
        "session_token_expired",
        "invalid_session_token",
    }


def test_heartbeat_stops_renewing_a_revoked_identity(service, project_dir):
    project_id, agent, _, _ = _setup(service, project_dir)
    before = _session_row(service, project_id, agent["agent"]["id"])
    lease_before = next(
        lease
        for lease in service.list_leases(project_id)
        if lease.get("released_at") is None
    )

    _revoke(service, project_id, agent["agent"]["member_id"])

    with pytest.raises(DomainError) as refused:
        service.heartbeat(project_id, agent["agent"]["id"], agent["token"])
    assert refused.value.code in {"session_closed", "session_token_expired"}
    after = _session_row(service, project_id, agent["agent"]["id"])
    # Revocation pulled the token expiry back into the past, and the refused
    # heartbeat did not extend it again.
    assert after["token_expires_at"] < before["token_expires_at"]
    assert (
        next(
            lease
            for lease in service.list_leases(project_id, include_inactive=True)
            if lease["id"] == lease_before["id"]
        )["released_at"]
        is not None
    )


def test_the_revoked_project_credential_is_rejected_at_the_token_gate(
    service, project_dir
):
    project_id, agent, credential, _ = _setup(service, project_dir)
    token = credential["token"]
    assert service.authenticate_agent_token(token, project_id=project_id)

    _revoke(service, project_id, agent["agent"]["member_id"])

    with pytest.raises(DomainError) as refused:
        service.authenticate_agent_token(token, project_id=project_id)
    assert refused.value.code in {
        "project_credential_revoked",
        "agent_token_revoked",
    }
    assert (
        refused.value.details["required_action"]
        == "issue_new_project_token_and_update_bundle"
    )
