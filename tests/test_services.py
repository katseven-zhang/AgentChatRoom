from __future__ import annotations

import subprocess
import threading
from datetime import timedelta

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import parse_time, patterns_overlap, utc_now


def test_project_identity_is_idempotent(service, project_dir):
    first = service.create_project(root_path=str(project_dir), name="One")
    second = service.create_project(root_path=str(project_dir), name="Two")
    assert first["id"] == second["id"]
    assert len(service.list_projects()) == 1


def test_request_id_replays_write_without_duplicate_event(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    first = service.post_message(
        project["id"],
        body="Reliable message",
        session_id=executor["agent"]["id"],
        token=executor["token"],
        model_display_name="Model A",
        request_id="request-message-1",
    )
    replay = service.post_message(
        project["id"],
        body="Reliable message",
        session_id=executor["agent"]["id"],
        token=executor["token"],
        model_display_name="Model A",
        request_id="request-message-1",
    )

    assert first["event_id"] == replay["event_id"]
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert len(
        [
            event
            for event in service.list_events(project["id"])["events"]
            if event["event_type"] == "message.message"
        ]
    ) == 1

    with pytest.raises(DomainError) as failure:
        service.post_message(
            project["id"],
            body="Different message",
            session_id=executor["agent"]["id"],
            token=executor["token"],
            model_display_name="Model A",
            request_id="request-message-1",
        )
    assert failure.value.code == "idempotency_conflict"


def test_session_leave_releases_leases_and_closes_token(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    lease = service.acquire_lease(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        path_pattern="src/**",
    )["lease"]

    left = service.leave_session(
        project["id"],
        executor["agent"]["id"],
        executor["token"],
        request_id="request-leave-1",
    )
    replay = service.leave_session(
        project["id"],
        executor["agent"]["id"],
        executor["token"],
        request_id="request-leave-1",
    )

    assert left["agent"]["status"] == "offline"
    assert left["agent"]["left_at"] is not None
    assert left["released_lease_ids"] == [lease["id"]]
    assert replay["event_id"] == left["event_id"]
    assert replay["idempotent_replay"] is True
    historical = next(
        item
        for item in service.list_leases(project["id"], include_inactive=True)
        if item["id"] == lease["id"]
    )
    assert historical["released_at"] is not None

    with pytest.raises(DomainError) as failure:
        service.post_message(
            project["id"],
            body="Should be rejected",
            session_id=executor["agent"]["id"],
            token=executor["token"],
            model_display_name="Model A",
        )
    assert failure.value.code == "session_closed"


def test_software_identity_reconnect_replaces_the_previous_session(service, project):
    first = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex Coordinator",
        client="codex",
        model="unknown",
        role="coordinator",
    )
    second = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex Coordinator",
        client="codex",
        model="codex-ui-model",
        role="coordinator",
    )

    snapshot = service.snapshot(project["id"])
    assert len(snapshot["agents"]) == 2
    assert len(snapshot["agent_identities"]) == 1
    identity = snapshot["agent_identities"][0]
    assert identity["agent_key"] == second["agent"]["member_id"]
    assert first["agent"]["member_id"] == second["agent"]["member_id"]
    assert identity["session_count"] == 2
    assert identity["active_session_count"] == 1
    assert identity["connection_status"] == "connected"
    assert identity["status"] == "online"
    assert identity["models"] == ["codex-ui-model", "unknown"]
    first_session = next(
        item for item in snapshot["agents"] if item["id"] == first["agent"]["id"]
    )
    assert first_session["left_at"] is not None

    service.leave_session(
        project["id"], second["agent"]["id"], second["token"]
    )
    disconnected = service.snapshot(project["id"])["agent_identities"][0]
    assert disconnected["active_session_count"] == 0
    assert disconnected["connection_status"] == "disconnected"
    assert disconnected["activity_status"] is None
    assert disconnected["status"] == "registered"


def test_revoked_member_is_kept_in_history_but_removed_from_current_roster(
    service, project
):
    online = service.join_room(
        project["id"],
        agent_key="online-main",
        name="Online Agent",
        client="codex",
        model="unknown",
    )
    offline = service.join_room(
        project["id"],
        agent_key="offline-main",
        name="Offline Agent",
        client="trae",
        model="unknown",
    )
    revoked = service.join_room(
        project["id"],
        agent_key="revoked-main",
        name="Revoked Agent",
        client="qoder",
        model="unknown",
    )
    service.leave_session(project["id"], offline["agent"]["id"], offline["token"])
    service.leave_session(project["id"], revoked["agent"]["id"], revoked["token"])
    service.revoke_project_member(project["id"], revoked["agent"]["member_id"])

    snapshot = service.snapshot(project["id"])
    identities = {item["member_id"]: item for item in snapshot["agent_identities"]}
    assert len(snapshot["agents"]) == 3
    assert set(identities) == {
        online["agent"]["member_id"],
        offline["agent"]["member_id"],
    }
    assert identities[online["agent"]["member_id"]]["connection_status"] == "connected"
    assert identities[offline["agent"]["member_id"]]["connection_status"] == "disconnected"
    assert all(item["member_status"] != "revoked" for item in identities.values())

    members = service.list_project_members(project["id"])
    revoked_member = next(
        item for item in members if item["id"] == revoked["agent"]["member_id"]
    )
    assert revoked_member["status"] == "revoked"
    assert revoked_member["session_count"] == 1
    targets = service.list_task_intake_targets(project["id"])
    assert [item["member_id"] for item in targets] == [
        online["agent"]["member_id"],
        offline["agent"]["member_id"],
    ]


def test_agent_key_aliases_cannot_create_another_software_identity(service, project):
    first = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
        role="executor",
    )
    second = service.join_room(
        project["id"],
        agent_key="codex-review-main",
        name="Codex Review",
        client="codex",
        model="unknown",
        role="reviewer",
    )

    identities = service.snapshot(project["id"])["agent_identities"]
    assert len(identities) == 1
    assert first["agent"]["member_id"] == second["agent"]["member_id"]
    assert identities[0]["name"] == "Codex"
    assert identities[0]["active_session_count"] == 1


def test_reconnect_transfers_owned_task_and_active_lease(service, project):
    first = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )
    task = service.create_task(
        project["id"],
        title="Continue after reconnect",
        acceptance_criteria=["Ownership and lease survive reconnect"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], first["agent"]["id"], first["token"]
    )
    lease = service.acquire_lease(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        task_id=task["id"],
        path_pattern="src/identity.py",
    )["lease"]

    second = service.join_room(
        project["id"],
        agent_key="codex-runtime-check",
        name="Codex Runtime Check",
        client="codex",
        model="unknown",
        role="reviewer",
    )

    assert second["replaced"]["previous_session_ids"] == [first["agent"]["id"]]
    assert second["replaced"]["transferred_task_ids"] == [task["id"]]
    assert second["replaced"]["transferred_lease_ids"] == [lease["id"]]
    snapshot = service.snapshot(project["id"])
    stored_task = next(item for item in snapshot["tasks"] if item["id"] == task["id"])
    stored_lease = next(item for item in snapshot["leases"] if item["id"] == lease["id"])
    assert stored_task["owner_session_id"] == second["agent"]["id"]
    assert stored_lease["session_id"] == second["agent"]["id"]


def test_reconnect_recovers_unfinished_task_from_a_closed_identity_session(
    service, project
):
    first = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )
    task = service.create_task(
        project["id"],
        title="Resume after explicit disconnect",
        acceptance_criteria=["The software identity keeps unfinished work"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], first["agent"]["id"], first["token"]
    )
    service.update_task(
        project["id"],
        task["id"],
        status="in_progress",
        session_id=first["agent"]["id"],
        token=first["token"],
    )
    service.leave_session(project["id"], first["agent"]["id"], first["token"])

    second = service.join_room(
        project["id"],
        agent_key="runtime-check-main",
        name="Runtime Check",
        client="codex",
        model="unknown",
        role="reviewer",
    )

    assert second["replaced"]["previous_session_ids"] == []
    assert second["replaced"]["transferred_task_ids"] == [task["id"]]
    stored_task = next(
        item for item in service.snapshot(project["id"])["tasks"] if item["id"] == task["id"]
    )
    assert stored_task["owner_session_id"] == second["agent"]["id"]


def test_different_software_clients_have_distinct_identities(service, project):
    codex = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
    )
    trae = service.join_room(
        project["id"],
        agent_key="trae-main",
        name="Trae",
        client="trae",
        model="unknown",
    )
    identities = service.snapshot(project["id"])["agent_identities"]
    assert len(identities) == 2
    assert codex["agent"]["member_id"] != trae["agent"]["member_id"]


def test_unbound_legacy_sessions_remain_in_audit_but_not_the_software_roster(
    service, project
):
    joined = service.join_room(
        project["id"],
        agent_key="legacy-main",
        name="Legacy",
        client="legacy-client",
        model="unknown",
    )
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET member_id = NULL, agent_key = '' WHERE id = ?",
            (joined["agent"]["id"],),
        )

    snapshot = service.snapshot(project["id"])
    assert len(snapshot["agents"]) == 1
    assert snapshot["agent_identities"] == []


@pytest.mark.parametrize(
    ("model", "error_code"),
    [
        ("", "invalid_agent_model"),
    ],
)
def test_join_room_requires_model_code(service, project, model, error_code):
    with pytest.raises(DomainError) as failure:
        service.join_room(
            project["id"],
            agent_key="",
            name="Agent",
            client="generic",
            model=model,
        )

    assert failure.value.code == error_code


def test_agent_token_host_workspace_and_remote_session_flow(service, project):
    issued = service.issue_agent_token(
        project["id"],
        name="Remote coding agents",
        permissions=["room:join", "room:read", "task:write"],
        expires_in_seconds=3600,
    )
    credential = issued["credential"]
    token = issued["token"]
    assert token.startswith(f"acr.{credential['id']}.")
    assert "token_hash" not in credential
    assert credential["active"] is True

    first_workspace = service.register_workspace(
        project["id"],
        host_key="host-fingerprint-1",
        host_name="Developer PC",
        local_path="D:/work/project",
        branch="main",
    )
    repeated_workspace = service.register_workspace(
        project["id"],
        host_key="host-fingerprint-1",
        host_name="Developer PC",
        local_path="D:/work/project",
        branch="feature",
    )
    assert repeated_workspace["host"]["id"] == first_workspace["host"]["id"]
    assert (
        repeated_workspace["workspace"]["id"]
        == first_workspace["workspace"]["id"]
    )
    assert repeated_workspace["workspace"]["branch"] == "feature"

    joined = service.join_remote_room(
        project["id"],
        agent_token=token,
        host_key="host-fingerprint-1",
        host_name="Developer PC",
        workspace_path="D:/work/project",
        agent_key="remote-codex-main",
        name="Remote Codex",
        client="codex",
        model="test-model",
        branch="feature",
        capabilities={"mcp": True},
    )
    assert joined["agent"]["credential_id"] == credential["id"]
    assert joined["agent"]["host_id"] == first_workspace["host"]["id"]
    assert joined["agent"]["workspace_id"] == first_workspace["workspace"]["id"]
    assert joined["workspace"]["local_path"] == "D:/work/project"

    listed_tokens = service.list_agent_tokens(project["id"])
    assert listed_tokens[0]["id"] == credential["id"]
    assert "token" not in listed_tokens[0]
    assert "token_hash" not in listed_tokens[0]
    serialized_events = str(service.list_events(project["id"], after=0)["events"])
    assert token not in serialized_events

    audit = service.query_audit(project["id"], event_type="credential.issued")
    assert len(audit["events"]) == 1
    assert audit["events"][0]["payload"]["credential_id"] == credential["id"]

    rotated = service.rotate_agent_token(
        project["id"], credential["id"], expires_in_seconds=3600
    )
    with pytest.raises(DomainError) as old_rejected:
        service.authenticate_agent_token(token)
    assert old_rejected.value.code == "agent_token_revoked"
    credential = rotated["credential"]
    token = rotated["token"]
    assert service.authenticate_agent_token(token)["id"] == credential["id"]

    revoked = service.revoke_agent_token(project["id"], credential["id"])
    assert revoked["credential"]["active"] is False
    with pytest.raises(DomainError) as rejected:
        service.authenticate_agent_token(token)
    assert rejected.value.code == "agent_token_revoked"


def test_project_members_are_versioned_audited_and_linked_to_tokens(
    service, project
):
    created = service.create_project_member(
        project["id"],
        member_key="grok-build",
        name="Grok Build",
        kind="agent",
        role="executor",
        metadata={"vendor": "Tencent-compatible"},
    )
    member = created["member"]
    assert member["schema_version"] == 1
    assert member["status"] == "active"
    assert member["metadata"]["vendor"] == "Tencent-compatible"

    token = service.issue_agent_token(
        project["id"],
        name="Grok short token",
        member_id=member["id"],
        permissions=["room:join", "room:read", "member:read"],
    )
    assert token["credential"]["member_id"] == member["id"]
    joined = service.join_remote_room(
        project["id"],
        agent_token=token["token"],
        host_key="member-host",
        host_name="Member Host",
        workspace_path="D:/member-project",
        agent_key="grok-main",
        name="Grok Session",
        client="grok-build",
        model="unknown",
    )
    assert joined["agent"]["member_id"] == member["id"]

    updated = service.update_project_member(
        project["id"],
        member["id"],
        status="suspended",
        role="reviewer",
    )
    assert updated["member"]["status"] == "suspended"
    assert updated["member"]["role"] == "reviewer"
    with pytest.raises(DomainError) as blocked:
        service.issue_agent_token(
            project["id"],
            name="Blocked token",
            member_id=member["id"],
        )
    assert blocked.value.code == "project_member_inactive"

    revoked = service.revoke_project_member(project["id"], member["id"])
    assert revoked["member"]["status"] == "revoked"
    with pytest.raises(DomainError) as reactivated:
        service.update_project_member(
            project["id"], member["id"], status="active"
        )
    assert reactivated.value.code == "project_member_revoked"

    listed = service.list_project_members(project["id"])
    assert listed[0]["credential_count"] == 1
    assert listed[0]["session_count"] == 1
    events = service.list_events(project["id"])["events"]
    assert [event["event_type"] for event in events if event["event_type"].startswith("member.")] == [
        "member.created",
        "member.updated",
        "member.revoked",
    ]


def test_agent_token_enforces_project_and_permission_scope(
    service, project, tmp_path
):
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    other_project = service.create_project(root_path=str(other_path))
    scoped = service.issue_agent_token(
        project["id"],
        name="Read only",
        permissions=["room:read"],
    )

    with pytest.raises(DomainError) as missing_permission:
        service.join_remote_room(
            project["id"],
            agent_token=scoped["token"],
            host_key="host-1",
            host_name="Host",
            workspace_path="D:/project",
            agent_key="generic-main",
            name="Agent",
            client="generic",
            model="unknown",
        )
    assert missing_permission.value.code == "agent_token_permission_forbidden"

    with pytest.raises(DomainError) as wrong_project:
        service.authenticate_agent_token(
            scoped["token"],
            project_id=other_project["id"],
            required_permission="room:read",
        )
    assert wrong_project.value.code == "agent_token_project_forbidden"


def test_room_message_and_incremental_sync(service, project, joined_agents):
    executor, _ = joined_agents
    initial_cursor = executor["cursor"]
    posted = service.post_message(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        body="Starting authentication work",
        kind="message",
        model_display_name="Model A",
    )

    result = service.room_sync(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        after=initial_cursor,
    )

    assert result["cursor"] == posted["cursor"]
    assert result["events"][-1]["payload"]["body"] == "Starting authentication work"
    assert result["events"][-1]["payload"]["model_display_name"] == "Model A"
    assert len(result["snapshot"]["agents"]) == 2


def test_agent_message_model_is_required_and_preserved_per_event(
    service, project, joined_agents
):
    sender, _receiver = joined_agents
    session_id = sender["agent"]["id"]
    token = sender["token"]

    with pytest.raises(DomainError) as missing:
        service.post_message(
            project["id"],
            session_id=session_id,
            token=token,
            body="Missing model",
        )
    assert missing.value.code == "message_model_required"

    first = service.post_message(
        project["id"],
        session_id=session_id,
        token=token,
        body="Before switching models",
        model_display_name="Hy3",
    )
    second = service.post_message(
        project["id"],
        session_id=session_id,
        token=token,
        body="After switching models",
        model_display_name="MiniMax-M3",
    )

    events = {
        event["id"]: event
        for event in service.list_events(project["id"], after=0)["events"]
    }
    assert events[first["event_id"]]["payload"]["model_display_name"] == "Hy3"
    assert events[second["event_id"]]["payload"]["model_display_name"] == "MiniMax-M3"


def test_human_message_cannot_claim_an_agent_model(service, project):
    posted = service.post_message(project["id"], body="Manager note")
    event = next(
        event
        for event in service.list_events(project["id"], after=0)["events"]
        if event["id"] == posted["event_id"]
    )
    assert event["actor_session_id"] is None
    assert event["payload"]["model_display_name"] is None

    with pytest.raises(DomainError) as invalid:
        service.post_message(
            project["id"],
            body="Manager note with a fabricated model",
            model_display_name="Not allowed",
        )
    assert invalid.value.code == "message_model_requires_session"


def test_task_claim_is_atomic(service, project, joined_agents):
    first, second = joined_agents
    task = service.create_task(
        project["id"],
        title="Implement authentication",
        acceptance_criteria=["Login works"],
    )["task"]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def claim(joined):
        barrier.wait()
        try:
            service.claim_task(
                project["id"], task["id"], joined["agent"]["id"], joined["token"]
            )
            outcomes.append("claimed")
        except DomainError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=claim, args=(joined,)) for joined in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["claimed", "task_already_claimed"]


def test_task_contract_exposes_versioned_state(service, project):
    task = service.create_task(
        project["id"],
        title="Versioned task state",
        acceptance_criteria=["State contract is exposed"],
    )["task"]

    assert task["schema_version"] == 7
    assert task["execution_status"] == "todo"
    assert task["verification_status"] == "not_required"
    assert task["integration_status"] == "pending"
    assert task["phase"] == "todo"
    assert task["state"] == {
        "schema_version": 7,
        "execution_status": "todo",
        "verification_status": "not_required",
        "integration_status": "pending",
        "legacy_status": "todo",
        "phase": "todo",
        "completed": False,
        "verified": False,
        "integrated": False,
    }
    assert task["state_view"] == {
        "schema_version": 2,
        "phase": "todo",
        "group": "claimable",
        "needs_attention": False,
        "primary_badge": "todo",
        "auxiliary_badges": [],
        "execution_status": "todo",
        "verification_status": "not_required",
        "integration_status": "pending",
    }
    assert task["assignments"] == []
    assert task["handoffs"] == []
    assert task["integrations"] == []


def test_get_task_returns_one_complete_task(service, project):
    created = service.create_task(
        project["id"],
        title="Read one task",
        description="Avoid expanding a large project board",
        acceptance_criteria=["The exact task is returned"],
    )["task"]

    fetched = service.get_task(project["id"], created["id"])

    assert fetched["id"] == created["id"]
    assert fetched["title"] == "Read one task"
    assert fetched["acceptance_criteria"] == ["The exact task is returned"]
    assert fetched["state"]["schema_version"] == 7
    assert fetched["assignments"] == []
    assert fetched["handoffs"] == []
    assert fetched["integrations"] == []


def test_task_assignment_acceptance_is_targeted_and_idempotent(
    service, project, joined_agents
):
    assigner, wrong_agent = joined_agents
    worker = service.join_room(
        project["id"],
        agent_key="python-worker-main",
        name="Python Worker",
        client="grok-build",
        model="test-model",
        role="executor",
        capabilities={"python": True},
    )
    task = service.create_task(
        project["id"],
        title="Implement Python adapter",
        acceptance_criteria=["Adapter tests pass"],
    )["task"]
    assigned = service.assign_task(
        project["id"],
        task["id"],
        assigned_by_session_id=assigner["agent"]["id"],
        token=assigner["token"],
        assigned_to_session_id=worker["agent"]["id"],
        required_capability="python",
        note="Please implement the adapter",
    )

    with pytest.raises(DomainError) as wrong_target:
        service.acknowledge_task_assignment(
            project["id"],
            task["id"],
            assigned["assignment"]["id"],
            session_id=wrong_agent["agent"]["id"],
            token=wrong_agent["token"],
            response="accepted",
        )
    assert wrong_target.value.code == "assignment_not_for_agent"

    accepted = service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assigned["assignment"]["id"],
        session_id=worker["agent"]["id"],
        token=worker["token"],
        response="accepted",
        note="Acknowledged",
    )
    assert accepted["assignment"]["status"] == "accepted"
    assert accepted["task"]["owner_session_id"] == worker["agent"]["id"]
    assert accepted["task"]["execution_status"] == "claimed"

    repeated = service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assigned["assignment"]["id"],
        session_id=worker["agent"]["id"],
        token=worker["token"],
        response="accepted",
        note="Acknowledged",
    )
    assert repeated["event_id"] is None
    assert repeated["assignment"]["status"] == "accepted"

    with pytest.raises(DomainError) as changed_response:
        service.acknowledge_task_assignment(
            project["id"],
            task["id"],
            assigned["assignment"]["id"],
            session_id=worker["agent"]["id"],
            token=worker["token"],
            response="declined",
        )
    assert changed_response.value.code == "assignment_already_acknowledged"


@pytest.mark.parametrize("response", ["declined", "blocked"])
def test_task_assignment_can_be_declined_or_blocked(
    service, project, joined_agents, response
):
    assigner, worker = joined_agents
    task = service.create_task(
        project["id"],
        title=f"Assignment response {response}",
        acceptance_criteria=["Response is recorded"],
    )["task"]
    assigned = service.assign_task(
        project["id"],
        task["id"],
        assigned_by_session_id=assigner["agent"]["id"],
        token=assigner["token"],
        assigned_to_session_id=worker["agent"]["id"],
    )

    acknowledged = service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assigned["assignment"]["id"],
        session_id=worker["agent"]["id"],
        token=worker["token"],
        response=response,
        note="Cannot take this now",
    )

    assert acknowledged["assignment"]["status"] == response
    assert acknowledged["assignment"]["responded_by_session_id"] == worker["agent"]["id"]
    assert acknowledged["task"]["owner_session_id"] is None
    assert acknowledged["task"]["execution_status"] == "todo"


def test_manager_can_assign_without_impersonating_an_agent(
    service, project, joined_agents
):
    _, worker = joined_agents
    task = service.create_task(
        project["id"],
        title="Manager assignment",
        acceptance_criteria=["Worker acknowledges"],
    )["task"]

    assigned = service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=worker["agent"]["id"],
        note="Assigned from the management console",
    )

    assert assigned["assignment"]["assigned_by_session_id"] is None
    event = next(
        item
        for item in service.list_events(project["id"], after=0)["events"]
        if item["id"] == assigned["event_id"]
    )
    assert event["actor_session_id"] is None
    assert event["payload"]["assigned_by"] == "manager"


def test_task_handoff_transfers_owner_and_releases_task_leases(
    service, project, joined_agents
):
    executor, successor = joined_agents
    task = service.create_task(
        project["id"],
        title="Continue an implementation",
        acceptance_criteria=["New owner can continue"],
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
    lease = service.acquire_lease(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        task_id=task["id"],
        path_pattern="src/handoff/**",
    )["lease"]

    requested = service.handoff_task(
        project["id"],
        task["id"],
        from_session_id=executor["agent"]["id"],
        token=executor["token"],
        to_session_id=successor["agent"]["id"],
        summary="Core behavior is implemented",
        completed_items=["Service layer"],
        pending_items=["Adapter tests"],
        files=["src/handoff/service.py"],
        risks=["Compatibility needs verification"],
        next_step="Run adapter tests",
    )
    assert requested["handoff"]["status"] == "pending"
    assert requested["handoff"]["pending_items"] == ["Adapter tests"]
    assert requested["task"]["owner_session_id"] == executor["agent"]["id"]

    repeated_request = service.handoff_task(
        project["id"],
        task["id"],
        from_session_id=executor["agent"]["id"],
        token=executor["token"],
        to_session_id=successor["agent"]["id"],
        summary="Core behavior is implemented",
        next_step="Run adapter tests",
    )
    assert repeated_request["event_id"] is None

    accepted = service.acknowledge_task_handoff(
        project["id"],
        task["id"],
        requested["handoff"]["id"],
        session_id=successor["agent"]["id"],
        token=successor["token"],
        response="accepted",
        note="Context received",
    )
    assert accepted["handoff"]["status"] == "accepted"
    assert accepted["task"]["owner_session_id"] == successor["agent"]["id"]
    assert accepted["task"]["execution_status"] == "in_progress"
    assert accepted["task"]["current_step"] == "Run adapter tests"
    assert accepted["handoff"]["files"] == ["src/handoff/service.py"]
    historical_lease = next(
        item
        for item in service.list_leases(project["id"], include_inactive=True)
        if item["id"] == lease["id"]
    )
    assert historical_lease["active"] is False
    assert historical_lease["released_at"] is not None

    repeated_ack = service.acknowledge_task_handoff(
        project["id"],
        task["id"],
        requested["handoff"]["id"],
        session_id=successor["agent"]["id"],
        token=successor["token"],
        response="accepted",
    )
    assert repeated_ack["event_id"] is None


def test_manager_can_request_handoff_without_impersonating_owner(
    service, project, joined_agents
):
    owner, successor = joined_agents
    task = service.create_task(
        project["id"],
        title="Manager-requested handoff",
        acceptance_criteria=["Handoff is acknowledged"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], owner["agent"]["id"], owner["token"]
    )

    requested = service.handoff_task(
        project["id"],
        task["id"],
        to_session_id=successor["agent"]["id"],
        summary="Manager is coordinating a shift change",
        next_step="Review the current branch",
    )

    assert requested["handoff"]["from_session_id"] == owner["agent"]["id"]
    assert requested["handoff"]["requested_by_session_id"] is None
    event = next(
        item
        for item in service.list_events(project["id"], after=0)["events"]
        if item["id"] == requested["event_id"]
    )
    assert event["actor_session_id"] is None
    assert event["payload"]["requested_by"] == "manager"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("src/auth/**", "src/auth/login.py", True),
        ("src/**", "src/api/**", True),
        ("frontend/**", "backend/**", False),
        ("README.md", "README.md", True),
    ],
)
def test_path_overlap(left, right, expected):
    assert patterns_overlap(left, right) is expected


def test_conflicting_exclusive_lease_is_rejected(service, project, joined_agents):
    first, second = joined_agents
    acquired = service.acquire_lease(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/auth/**",
    )
    assert acquired["lease"]["active"] is True

    with pytest.raises(DomainError) as failure:
        service.acquire_lease(
            project["id"],
            session_id=second["agent"]["id"],
            token=second["token"],
            path_pattern="src/auth/login.py",
        )

    assert failure.value.code == "lease_conflict"
    assert failure.value.details["conflicts"][0]["agent_name"] == "Builder"
    events = service.list_events(project["id"], after=0)["events"]
    assert any(event["event_type"] == "lease.conflict" for event in events)


def test_work_report_releases_task_leases(service, project, joined_agents):
    executor, _reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Release task leases",
        acceptance_criteria=["Lease is released with the work report"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    lease = service.acquire_lease(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        task_id=task["id"],
        path_pattern="src/agentchatroom/services.py",
    )["lease"]

    report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Lease lifecycle completed",
        files=["src/agentchatroom/services.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )

    event = next(
        event
        for event in service.list_events(project["id"])["events"]
        if event["event_type"] == "work.reported"
    )
    historical = next(
        item
        for item in service.list_leases(project["id"], include_inactive=True)
        if item["id"] == lease["id"]
    )
    assert event["payload"]["released_lease_ids"] == [lease["id"]]
    assert historical["released_at"] is not None
    assert report["task_status"] == "awaiting_review"


def test_work_must_be_independently_reviewed(service, project, joined_agents):
    executor, reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Implement authentication",
        acceptance_criteria=["Login works", "Tests pass"],
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
    report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Implemented login",
        files=["src/auth/login.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    assert report["task_status"] == "awaiting_review"
    assert report["execution_status"] == "completed"
    assert report["verification_status"] == "pending"
    events_before_review = service.list_events(project["id"], after=0)["events"]
    completion_event = next(
        event for event in events_before_review if event["event_type"] == "task.completed"
    )
    assert completion_event["id"] == report["event_id"]

    with pytest.raises(DomainError) as failure:
        service.submit_review(
            project["id"],
            task["id"],
            reviewer_session_id=executor["agent"]["id"],
            token=executor["token"],
            verdict="approved",
            criteria=[{"criterion": "Login works", "status": "passed"}],
        )
    assert failure.value.code == "reviewer_not_independent"

    with pytest.raises(DomainError) as incomplete_review:
        service.submit_review(
            project["id"],
            task["id"],
            reviewer_session_id=reviewer["agent"]["id"],
            token=reviewer["token"],
            verdict="approved",
            criteria=[{"criterion": "Login works", "status": "passed"}],
        )
    assert incomplete_review.value.code == "acceptance_criteria_not_satisfied"

    approved = service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[
            {"criterion": "Login works", "status": "passed"},
            {"criterion": "Tests pass", "status": "passed"},
        ],
    )
    assert approved["task_status"] == "verified"
    assert approved["execution_status"] == "completed"
    assert approved["verification_status"] == "approved"

    events_after_review = service.list_events(project["id"], after=0)["events"]
    review_event = next(
        event for event in events_after_review if event["event_type"] == "review.submitted"
    )
    assert completion_event["id"] < review_event["id"]

    with pytest.raises(DomainError) as direct_done:
        service.update_task(project["id"], task["id"], status="done")
    assert direct_done.value.code == "structured_transition_required"

    with pytest.raises(DomainError) as failed_tests:
        service.submit_integration(
            project["id"],
            task["id"],
            integrator_session_id=reviewer["agent"]["id"],
            token=reviewer["token"],
            result="done",
            summary="Integrated authentication",
            files=["src/auth/login.py"],
            tests=[{"command": "pytest", "exit_code": 1}],
        )
    assert failed_tests.value.code == "integration_tests_failed"

    done = service.submit_integration(
        project["id"],
        task["id"],
        integrator_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        result="done",
        summary="Integrated authentication",
        files=["src/auth/login.py"],
        commit_hash="def456",
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    assert done["task"]["status"] == "done"
    assert done["task"]["integration_status"] == "done"
    assert done["integration"]["commit_hash"] == "def456"
    integration_event = next(
        event
        for event in service.list_events(project["id"], after=0)["events"]
        if event["event_type"] == "task.integration_completed"
    )
    assert review_event["id"] < integration_event["id"]


def test_same_software_cannot_become_an_independent_reviewer_by_reconnecting(
    service, project
):
    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )
    executor = service.join_room(
        project["id"],
        agent_key="codex-main",
        name="Codex",
        client="codex",
        model="unknown",
        role="executor",
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Identity-aware review",
        acceptance_criteria=["Review comes from another software identity"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Implemented",
        files=["src/identity.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    reviewer_alias = service.join_room(
        project["id"],
        agent_key="codex-review-main",
        name="Codex Review",
        client="codex",
        model="unknown",
        role="reviewer",
    )

    with pytest.raises(DomainError) as failure:
        service.submit_review(
            project["id"],
            task["id"],
            reviewer_session_id=reviewer_alias["agent"]["id"],
            token=reviewer_alias["token"],
            verdict="approved",
            criteria=[
                {
                    "criterion": "Review comes from another software identity",
                    "status": "passed",
                }
            ],
        )
    assert failure.value.code == "reviewer_not_independent"


def test_approved_completed_task_can_handoff_to_integrator(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    integrator = service.join_room(
        project["id"],
        agent_key="integrator-main",
        name="Integrator",
        client="trae",
        model="unknown",
        role="integrator",
    )
    task = service.create_task(
        project["id"],
        title="Handoff after independent approval",
        acceptance_criteria=["Implementation is verified"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Implementation is complete",
        files=["src/verified.py"],
        tests=[{"command": "pytest tests/test_verified.py", "exit_code": 0}],
    )

    with pytest.raises(DomainError) as before_review:
        service.handoff_task(
            project["id"],
            task["id"],
            from_session_id=executor["agent"]["id"],
            token=executor["token"],
            to_session_id=integrator["agent"]["id"],
            summary="Not independently approved yet",
            next_step="Integrate the verified change",
        )
    assert before_review.value.code == "task_not_handoffable"

    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Implementation is verified", "status": "passed"}],
    )
    requested = service.handoff_task(
        project["id"],
        task["id"],
        from_session_id=executor["agent"]["id"],
        token=executor["token"],
        to_session_id=integrator["agent"]["id"],
        summary="Implementation passed independent review",
        completed_items=["Implementation", "Independent review"],
        pending_items=["Integration"],
        files=["src/verified.py"],
        next_step="Run integration tests and record the result",
    )
    accepted = service.acknowledge_task_handoff(
        project["id"],
        task["id"],
        requested["handoff"]["id"],
        session_id=integrator["agent"]["id"],
        token=integrator["token"],
        response="accepted",
    )
    assert accepted["task"]["owner_session_id"] == integrator["agent"]["id"]
    assert accepted["task"]["execution_status"] == "completed"
    assert accepted["task"]["verification_status"] == "approved"
    assert accepted["task"]["integration_status"] == "pending"

    integrated = service.submit_integration(
        project["id"],
        task["id"],
        integrator_session_id=integrator["agent"]["id"],
        token=integrator["token"],
        result="done",
        summary="Integration tests passed",
        files=["src/verified.py"],
        tests=[{"command": "pytest tests/test_verified.py", "exit_code": 0}],
    )
    assert integrated["task"]["status"] == "done"
    assert integrated["task"]["integration_status"] == "done"


def test_changes_requested_preserves_completion_history_and_allows_resubmission(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Correctable implementation",
        acceptance_criteria=["Behavior is correct"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    first_report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Initial implementation",
        files=["src/implementation.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    rejected = service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="changes_requested",
        criteria=[{"criterion": "Behavior is correct", "status": "failed"}],
        notes="Missing an edge case",
    )

    assert rejected["task_status"] == "in_progress"
    assert rejected["execution_status"] == "in_progress"
    assert rejected["verification_status"] == "changes_requested"
    history_after_rejection = service.list_events(project["id"], after=0)["events"]
    assert any(
        event["id"] == first_report["event_id"]
        and event["event_type"] == "task.completed"
        for event in history_after_rejection
    )

    second_report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Corrected implementation",
        files=["src/implementation.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    approved = service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Behavior is correct", "status": "passed"}],
    )

    assert second_report["event_id"] > first_report["event_id"]
    assert approved["verification_status"] == "approved"
    completion_events = [
        event
        for event in service.list_events(project["id"], after=0)["events"]
        if event["event_type"] == "task.completed"
    ]
    assert [event["id"] for event in completion_events] == [
        first_report["event_id"],
        second_report["event_id"],
    ]


def test_changes_requested_task_can_be_reassigned_after_owner_leaves(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )
    replacement = service.join_room(
        project["id"],
        agent_key="revision-worker-main",
        name="Revision Worker",
        client="grok-build",
        model="MiniMax-M3",
        role="executor",
        capabilities={"mcp": True},
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Reassign a rejected task",
        acceptance_criteria=["The revised report is accepted"],
    )["task"]

    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Initial report",
        files=[],
        tests=[{"command": "mcp lifecycle", "exit_code": 0}],
        no_code_change_reason="Initial protocol pass",
    )
    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="changes_requested",
        criteria=[{"criterion": "The revised report is accepted", "status": "failed"}],
        notes="The original owner must provide a revised report.",
    )
    service.leave_session(
        project["id"], executor["agent"]["id"], executor["token"]
    )

    assigned = service.assign_task(
        project["id"],
        task["id"],
        assigned_to_session_id=replacement["agent"]["id"],
        note="Continue the revision after the original session left.",
    )
    accepted = service.acknowledge_task_assignment(
        project["id"],
        task["id"],
        assigned["assignment"]["id"],
        session_id=replacement["agent"]["id"],
        token=replacement["token"],
        response="accepted",
    )

    assert accepted["assignment"]["status"] == "accepted"
    assert accepted["task"]["owner_session_id"] == replacement["agent"]["id"]
    assert accepted["task"]["execution_status"] == "claimed"
    assert accepted["task"]["verification_status"] == "changes_requested"
    assignment_event = next(
        event
        for event in service.list_events(project["id"], after=0)["events"]
        if event["id"] == accepted["event_id"]
    )
    assert assignment_event["event_type"] == "task.assignment_acknowledged"
    assert assignment_event["payload"]["reclaimed_from_session_id"] == executor["agent"]["id"]

    revised = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=replacement["agent"]["id"],
        token=replacement["token"],
        summary="Revised report",
        files=[],
        tests=[{"command": "mcp lifecycle", "exit_code": 0}],
        no_code_change_reason="Revision added the missing acceptance evidence.",
    )
    assert revised["task_status"] == "awaiting_review"


def test_work_report_requires_evidence(service, project, joined_agents):
    executor, _ = joined_agents
    task = service.create_task(
        project["id"], title="Small change", acceptance_criteria=["Change is complete"]
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    with pytest.raises(DomainError) as failure:
        service.submit_work_report(
            project["id"],
            task["id"],
            session_id=executor["agent"]["id"],
            token=executor["token"],
            summary="Done",
            files=[],
            tests=[],
        )
    assert failure.value.code == "insufficient_work_evidence"


def test_work_report_accepts_and_exports_structured_no_code_reason(
    service, project, joined_agents
):
    executor, _ = joined_agents
    task = service.create_task(
        project["id"],
        title="Investigate current architecture",
        acceptance_criteria=["Findings cite the current implementation"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )

    result = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Inspected the implementation and documented the findings",
        files=[],
        tests=[{"command": "Read-only source inspection", "exit_code": 0}],
        no_code_change_reason="The assigned task was an evidence-only investigation",
    )

    assert result["no_code_change_reason"] == (
        "The assigned task was an evidence-only investigation"
    )
    snapshot = service.snapshot(project["id"])
    assert snapshot["reports"][0]["files"] == []
    assert snapshot["reports"][0]["no_code_change_reason"] == (
        "The assigned task was an evidence-only investigation"
    )
    events = service.list_events(project["id"], after=0)["events"]
    work_event = next(event for event in events if event["event_type"] == "work.reported")
    completion_event = next(
        event for event in events if event["event_type"] == "task.completed"
    )
    assert work_event["payload"]["no_code_change_reason"] == (
        "The assigned task was an evidence-only investigation"
    )
    assert completion_event["payload"]["no_code_change_reason"] == (
        "The assigned task was an evidence-only investigation"
    )
    exported = service.export_project(project["id"])
    assert exported["reports"][0]["no_code_change_reason"] == (
        "The assigned task was an evidence-only investigation"
    )


def test_work_report_rejects_unstructured_test_evidence(service, project, joined_agents):
    executor, _ = joined_agents
    task = service.create_task(
        project["id"], title="Evidence change", acceptance_criteria=["Evidence is valid"]
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    with pytest.raises(DomainError) as failure:
        service.submit_work_report(
            project["id"],
            task["id"],
            session_id=executor["agent"]["id"],
            token=executor["token"],
            summary="Done",
            files=["src/example.py"],
            tests=[{"command": "pytest", "status": "reported"}],
        )
    assert failure.value.code == "invalid_test_evidence"


def test_task_dependencies_block_claim_until_verified(service, project, joined_agents):
    executor, reviewer = joined_agents
    prerequisite = service.create_task(
        project["id"],
        title="Foundation",
        acceptance_criteria=["Foundation works"],
    )["task"]
    dependent = service.create_task(
        project["id"],
        title="Dependent",
        acceptance_criteria=["Dependent works"],
        depends_on=[prerequisite["id"]],
    )["task"]

    with pytest.raises(DomainError) as blocked:
        service.claim_task(
            project["id"], dependent["id"], executor["agent"]["id"], executor["token"]
        )
    assert blocked.value.code == "task_dependencies_incomplete"

    service.claim_task(
        project["id"], prerequisite["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        prerequisite["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Foundation ready",
        files=["src/foundation.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    service.submit_review(
        project["id"],
        prerequisite["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Foundation works", "status": "passed"}],
    )
    claimed = service.claim_task(
        project["id"], dependent["id"], reviewer["agent"]["id"], reviewer["token"]
    )
    assert claimed["task"]["status"] == "claimed"


def test_required_message_can_be_acknowledged(service, project, joined_agents):
    sender, receiver = joined_agents
    message = service.post_message(
        project["id"],
        session_id=sender["agent"]["id"],
        token=sender["token"],
        body="Please confirm the API contract",
        model_display_name="Model A",
        requires_ack=True,
    )
    acknowledged = service.acknowledge_event(
        project["id"],
        message["event_id"],
        receiver["agent"]["id"],
        receiver["token"],
    )
    assert acknowledged["acknowledged"] is True
    assert acknowledged["acknowledged_event_id"] == message["event_id"]
    assert acknowledged["event_id"] == acknowledged["cursor"]
    assert acknowledged["event_id"] > message["event_id"]
    assert service.snapshot(project["id"])["acknowledgements"][0]["event_id"] == message["event_id"]


def test_archived_project_is_hidden_and_can_be_restored(service, project_dir):
    project = service.create_project(root_path=str(project_dir), name="Archivable")
    archived = service.archive_project(project["id"])
    repeated = service.archive_project(project["id"])
    assert archived["event_id"] == archived["cursor"]
    assert repeated["event_id"] is None
    assert repeated["cursor"] == archived["cursor"]
    assert service.list_projects() == []

    restored = service.create_project(root_path=str(project_dir))
    assert restored["id"] == project["id"]
    assert restored["archived_at"] is None


def test_agent_join_resolution_does_not_restore_archived_project(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    service.archive_project(project["id"])

    with pytest.raises(DomainError) as archived:
        service.resolve_project_for_join(
            root_path=str(project_dir),
            registered_project_key=project["project_key"],
        )

    assert archived.value.code == "project_archived"
    assert service.list_projects() == []


def test_agent_join_resolution_and_explicit_create_reuse_repository_scope(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    resolved = service.resolve_project_for_join(root_path=str(project_dir))
    assert resolved["id"] == project["id"]
    repeated = service.create_project(root_path=str(project_dir))
    assert repeated["id"] == project["id"]
    assert len(service.list_projects()) == 1


@pytest.mark.parametrize(
    "logical_path",
    [r"D:\\claw\\agentchatroom", "/srv/agentchatroom", "../agentchatroom"],
)
def test_project_scope_rejects_non_relative_logical_paths(
    service, project_dir, logical_path
):
    with pytest.raises(DomainError) as invalid_create:
        service.create_project(
            root_path=str(project_dir), logical_path=logical_path
        )
    assert invalid_create.value.code == "invalid_logical_path"

    with pytest.raises(DomainError) as invalid_join:
        service.resolve_project_for_join(
            root_path=str(project_dir), logical_path=logical_path
        )
    assert invalid_join.value.code == "invalid_logical_path"


def test_project_scope_rejects_relative_path_that_does_not_match_project_directory(
    service, tmp_path
):
    repository = tmp_path / "repository"
    api_dir = repository / "packages" / "api"
    api_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)

    with pytest.raises(DomainError) as mismatch:
        service.create_project(
            root_path=str(api_dir), logical_path="packages/web"
        )
    assert mismatch.value.code == "invalid_logical_path"


def test_archived_repository_scope_must_be_restored_instead_of_duplicated(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    service.archive_project(project["id"])

    restored = service.create_project(root_path=str(project_dir))
    assert restored["id"] == project["id"]
    assert restored["archived_at"] is None


def test_deleted_project_can_be_recreated_by_first_agent_join(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    service.delete_project(project["id"])

    with pytest.raises(DomainError) as orphaned:
        service.resolve_project_for_join(
            root_path=str(project_dir),
            registered_project_key=project["project_key"],
        )
    assert orphaned.value.code == "project_registration_orphaned"

    recreated = service.resolve_project_for_join(root_path=str(project_dir))
    assert recreated["id"] != project["id"]
    assert recreated["project_key"] != project["project_key"]


def test_project_can_be_permanently_deleted(service, project, joined_agents):
    service.post_message(project["id"], body="Delete this room")
    deleted = service.delete_project(project["id"])

    assert deleted == {
        "deleted": True,
        "project_id": project["id"],
        "project_name": project["name"],
    }
    assert service.list_projects() == []
    with pytest.raises(DomainError) as missing:
        service.snapshot(project["id"])
    assert missing.value.code == "project_not_found"


def test_project_settings_are_validated_updated_and_exported(
    service, project, joined_agents
):
    executor, _ = joined_agents
    updated = service.update_project(
        project["id"],
        name="Configured Room",
        settings={
            "lease_conflict_policy": "pre_commit_block",
            "roles": ["implementer", "auditor"],
            "extensions": {"example.vendor/theme": "contrast"},
        },
    )
    assert updated["project"]["name"] == "Configured Room"
    assert updated["project"]["settings"]["roles"] == ["implementer", "auditor"]

    lease = service.acquire_lease(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        path_pattern="src/export.py",
    )["lease"]
    service.release_lease(
        project["id"], lease["id"], executor["agent"]["id"], executor["token"]
    )
    exported = service.export_project(project["id"])
    assert exported["export_schema_version"] == 1
    assert exported["cursor"] == exported["events"][-1]["id"]
    assert exported["leases"][0]["active"] is False
    assert all("token_hash" not in agent for agent in exported["agents"])

    with pytest.raises(DomainError) as invalid:
        service.update_project(project["id"], settings={"vendor": "hardcoded"})
    assert invalid.value.code == "invalid_project_settings"


def test_room_sync_tracks_each_agent_cursor_and_unread_count(
    service, project, joined_agents
):
    first, second = joined_agents
    posted = service.post_message(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        body="Review the contract",
        model_display_name="Model A",
        priority=0,
        channel="review",
    )
    before = {agent["id"]: agent for agent in service.snapshot(project["id"])["agents"]}
    assert before[first["agent"]["id"]]["unread_count"] >= 1
    assert before[second["agent"]["id"]]["unread_count"] == 1

    synced = service.room_sync(
        project["id"],
        session_id=second["agent"]["id"],
        token=second["token"],
        after=second["cursor"],
    )
    assert synced["cursor"] == posted["cursor"]
    assert synced["unread_count"] == 0
    assert synced["events"][-1]["channel"] == "review"
    assert synced["events"][-1]["payload"]["priority"] == 0
    agents = {agent["id"]: agent for agent in synced["snapshot"]["agents"]}
    assert agents[second["agent"]["id"]]["last_read_cursor"] == posted["cursor"]
    assert agents[second["agent"]["id"]]["unread_count"] == 0
    assert agents[first["agent"]["id"]]["unread_count"] >= 1


def test_identity_unread_count_reflects_freshest_session(service, project):
    """Identity unread uses the most current session, not the stalest."""
    joined = service.join_room(
        project["id"],
        agent_key="unread-main",
        name="Unread Agent",
        client="generic",
        model="unknown",
    )
    service.post_message(
        project["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        body="first",
        model_display_name="Model A",
    )
    # A second session of the same identity syncs to the latest cursor.
    freshest = service.join_room(
        project["id"],
        agent_key="unread-main",
        name="Unread Agent",
        client="generic",
        model="unknown",
    )
    service.room_sync(
        project["id"],
        session_id=freshest["agent"]["id"],
        token=freshest["token"],
        after=0,
    )
    snapshot = service.snapshot(project["id"])
    identity = next(
        item
        for item in snapshot["agent_identities"]
        if item["member_id"] == freshest["agent"]["member_id"]
    )
    assert identity["session_count"] == 2
    # The stale session still has unread events, but the identity badge must
    # reflect the freshest session's knowledge (min, not max).
    stale = next(
        agent
        for agent in snapshot["agents"]
        if agent["id"] == joined["agent"]["id"]
    )
    assert stale["unread_count"] >= 1
    assert identity["unread_count"] == 0


def test_task_can_be_edited_released_and_cannot_form_dependency_cycle(
    service, project, joined_agents
):
    executor, _ = joined_agents
    first = service.create_task(
        project["id"], title="First", acceptance_criteria=["First passes"]
    )["task"]
    second = service.create_task(
        project["id"],
        title="Second",
        acceptance_criteria=["Second passes"],
        depends_on=[first["id"]],
    )["task"]
    edited = service.update_task(
        project["id"],
        first["id"],
        title="First edited",
        description="Explicit scope",
        acceptance_criteria=["Edited criterion"],
        progress_percent=10,
        current_step="Inspecting",
        next_step="Implement",
    )["task"]
    assert edited["title"] == "First edited"
    assert edited["progress_percent"] == 10

    with pytest.raises(DomainError) as cycle:
        service.update_task(project["id"], first["id"], depends_on=[second["id"]])
    assert cycle.value.code == "task_dependency_cycle"

    service.claim_task(
        project["id"], first["id"], executor["agent"]["id"], executor["token"]
    )
    blocked = service.update_task(
        project["id"],
        first["id"],
        status="blocked",
        blocker_reason="Waiting for a local service",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    assert blocked["blocker_reason"] == "Waiting for a local service"
    released = service.update_task(
        project["id"],
        first["id"],
        status="todo",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    assert released["owner_session_id"] is None
    assert released["blocker_reason"] == ""
    event_types = [
        event["event_type"]
        for event in service.list_events(project["id"], after=0)["events"]
    ]
    assert "task.blocked" in event_types
    assert "task.released" in event_types


def test_readonly_lease_does_not_conflict_and_heartbeat_preserves_ttl(
    service, project, joined_agents
):
    first, second = joined_agents
    exclusive = service.acquire_lease(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/auth/**",
        ttl_seconds=600,
    )["lease"]
    readonly = service.acquire_lease(
        project["id"],
        session_id=second["agent"]["id"],
        token=second["token"],
        path_pattern="src/auth/login.py",
        mode="readonly",
    )["lease"]
    assert readonly["active"] is True

    service.heartbeat(project["id"], first["agent"]["id"], first["token"])
    renewed = next(
        lease
        for lease in service.list_leases(project["id"])
        if lease["id"] == exclusive["id"]
    )
    seconds_remaining = (parse_time(renewed["expires_at"]) - utc_now()).total_seconds()
    assert renewed["ttl_seconds"] == 600
    assert 590 <= seconds_remaining <= 600


def test_pre_commit_policy_blocks_files_held_by_another_agent(
    service, project, joined_agents
):
    first, second = joined_agents
    service.acquire_lease(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/security/**",
        mode="exclusive",
    )
    advisory = service.check_leases(
        project["id"],
        paths=["src/security/login.py"],
        session_id=second["agent"]["id"],
        token=second["token"],
    )
    assert advisory["policy"] == "advisory"
    assert advisory["blocked"] is False
    assert len(advisory["conflicts"]) == 1

    service.update_project(
        project["id"],
        settings={
            "lease_conflict_policy": "pre_commit_block",
            "roles": [],
            "extensions": {},
        },
    )
    with pytest.raises(DomainError) as blocked:
        service.check_leases(
            project["id"],
            paths=["src/security/login.py"],
            session_id=second["agent"]["id"],
            token=second["token"],
        )
    assert blocked.value.code == "pre_commit_blocked"
    assert blocked.value.details["blocked"] is True
    assert any(
        event["event_type"] == "lease.pre_commit_blocked"
        for event in service.list_events(project["id"], after=0)["events"]
    )


def test_agent_is_derived_offline_after_heartbeat_timeout(
    service, project, joined_agents
):
    agent, _ = joined_agents
    stale = (utc_now() - timedelta(seconds=service.settings.heartbeat_timeout_seconds + 1)).isoformat().replace("+00:00", "Z")
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
            (stale, agent["agent"]["id"]),
        )
        connection.execute(
            "UPDATE events SET created_at = ? WHERE actor_session_id = ?",
            (stale, agent["agent"]["id"]),
        )

    current = next(
        item
        for item in service.snapshot(project["id"])["agents"]
        if item["id"] == agent["agent"]["id"]
    )
    assert current["status"] == "offline"


def test_agent_stays_online_with_recent_activity_despite_stale_heartbeat(
    service, project, joined_agents
):
    agent, _ = joined_agents
    stale = (utc_now() - timedelta(seconds=service.settings.heartbeat_timeout_seconds + 1)).isoformat().replace("+00:00", "Z")
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
            (stale, agent["agent"]["id"]),
        )

    # The agent keeps acting after its background heartbeat went stale.
    service.post_message(
        project["id"],
        body="Still alive",
        session_id=agent["agent"]["id"],
        token=agent["token"],
        model_display_name="Model A",
    )

    current = next(
        item
        for item in service.snapshot(project["id"])["agents"]
        if item["id"] == agent["agent"]["id"]
    )
    assert current["status"] != "offline"


def test_offline_agent_lease_becomes_reclaimable(
    service, project, joined_agents
):
    first, second = joined_agents
    lease = service.acquire_lease(
        project["id"],
        session_id=first["agent"]["id"],
        token=first["token"],
        path_pattern="src/reclaimable/**",
    )["lease"]
    stale = (
        utc_now() - timedelta(seconds=service.settings.heartbeat_timeout_seconds + 1)
    ).isoformat().replace("+00:00", "Z")
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
            (stale, first["agent"]["id"]),
        )

    history = service.list_leases(project["id"], include_inactive=True)
    stale_lease = next(item for item in history if item["id"] == lease["id"])
    assert stale_lease["active"] is False
    assert stale_lease["reclaimable"] is True
    assert service.list_leases(project["id"]) == []

    acquired = service.acquire_lease(
        project["id"],
        session_id=second["agent"]["id"],
        token=second["token"],
        path_pattern="src/reclaimable/file.py",
    )
    assert acquired["lease"]["active"] is True
    assert acquired["event_id"] == acquired["cursor"]


def test_git_worktrees_share_room_and_logical_projects_remain_distinct(
    service, tmp_path
):
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    repository.mkdir()

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    git("init", "-b", "main")
    git("config", "user.email", "mvp@example.invalid")
    git("config", "user.name", "MVP Test")
    git("remote", "add", "origin", "https://example.invalid/team/project.git")
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "initial")
    git("worktree", "add", "-b", "feature", str(worktree))

    primary = service.create_project(root_path=str(repository))
    alternate = service.create_project(root_path=str(worktree))
    assert primary["id"] == alternate["id"]
    first = service.join_room(
        primary["id"],
        agent_key="first-worktree-main",
        name="First Worktree",
        client="client-one",
        model="unknown",
        worktree=str(repository),
    )
    second = service.join_room(
        alternate["id"],
        agent_key="second-worktree-main",
        name="Second Worktree",
        client="client-two",
        model="unknown",
        worktree=str(worktree),
    )
    assert first["agent"]["project_id"] == second["agent"]["project_id"]

    api_dir = repository / "packages" / "api"
    web_dir = repository / "packages" / "web"
    api_dir.mkdir(parents=True)
    web_dir.mkdir(parents=True)
    api_project = service.create_project(root_path=str(api_dir))
    web_project = service.create_project(root_path=str(web_dir))
    assert api_project["id"] != web_project["id"]
