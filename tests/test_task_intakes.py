"""Regression tests for the two-stage user task intake and stable task_number."""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _join(service, project, *, agent_key, name, client="codex"):
    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )
    return service.join_room(
        project["id"],
        agent_key=agent_key,
        name=name,
        client=client,
        software_key=agent_key,
        model="test-model",
        role="executor",
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )


def _join_ordered(service, project):
    """Join a creator and a target so each gets its own software identity."""
    creator = _join(
        service, project, agent_key="intake-creator", name="Creator", client="creator-cli"
    )
    target = _join(
        service, project, agent_key="intake-target", name="Target", client="target-cli"
    )
    return creator, target


def test_task_intake_targets_include_active_offline_and_exclude_revoked(
    service, project
):
    online = _join(service, project, agent_key="intake-online", name="Online")
    offline = _join(service, project, agent_key="intake-offline", name="Offline")
    revoked = _join(service, project, agent_key="intake-revoked", name="Revoked")
    suspended = _join(service, project, agent_key="intake-suspended", name="Suspended")
    service.leave_session(project["id"], offline["agent"]["id"], offline["token"])
    service.leave_session(project["id"], revoked["agent"]["id"], revoked["token"])
    service.leave_session(project["id"], suspended["agent"]["id"], suspended["token"])
    service.revoke_project_member(project["id"], revoked["agent"]["member_id"])
    service.update_project_member(project["id"], suspended["agent"]["member_id"], status="suspended")

    targets = service.list_task_intake_targets(project["id"])
    by_id = {item["member_id"]: item for item in targets}
    assert set(by_id) == {
        online["agent"]["member_id"],
        offline["agent"]["member_id"],
        suspended["agent"]["member_id"],
    }
    assert by_id[online["agent"]["member_id"]]["connection_status"] == "connected"
    assert by_id[offline["agent"]["member_id"]]["connection_status"] == "disconnected"
    assert by_id[suspended["agent"]["member_id"]]["connection_status"] == "disconnected"


def test_reassign_task_intake_allows_active_offline_target_without_session(
    service, project
):
    creator = _join(service, project, agent_key="intake-creator", name="Creator")
    first_target = _join(service, project, agent_key="intake-first", name="First")
    second_target = _join(service, project, agent_key="intake-second", name="Second")
    service.leave_session(project["id"], second_target["agent"]["id"], second_target["token"])
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Reassign when the second Agent reconnects",
        target_member_id=first_target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]

    reassigned = service.reassign_task_intake(
        project["id"],
        intake["id"],
        target_member_id=second_target["agent"]["member_id"],
    )["intake"]
    assert reassigned["target_member_id"] == second_target["agent"]["member_id"]
    assert reassigned["target_session_id"] is None


def test_submit_task_intake_allows_active_offline_target_without_session(
    service, project
):
    creator = _join(service, project, agent_key="intake-creator", name="Creator")
    target = _join(service, project, agent_key="intake-offline", name="Offline")
    service.leave_session(project["id"], target["agent"]["id"], target["token"])

    intake = service.submit_task_intake(
        project["id"],
        raw_description="Fix the auth flow when the target reconnects",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    assert intake["status"] == "pending"
    assert intake["target_member_id"] == target["agent"]["member_id"]
    assert intake["target_session_id"] is None


def test_submit_task_intake_rejects_revoked_member(service, project):
    target = _join(service, project, agent_key="intake-revoked", name="Revoked")
    service.revoke_project_member(project["id"], target["agent"]["member_id"])
    with pytest.raises(DomainError) as failure:
        service.submit_task_intake(
            project["id"],
            raw_description="Do not route to revoked Agent",
            target_member_id=target["agent"]["member_id"],
        )
    assert failure.value.code == "task_intake_target_unavailable"


def test_submit_task_intake_still_rejects_explicit_offline_session(
    service, project
):
    target = _join(service, project, agent_key="intake-target", name="Target")
    service.leave_session(project["id"], target["agent"]["id"], target["token"])
    with pytest.raises(DomainError) as failure:
        service.submit_task_intake(
            project["id"],
            raw_description="Fix the auth flow",
            target_member_id=target["agent"]["member_id"],
            target_session_id=target["agent"]["id"],
        )
    assert failure.value.code == "task_intake_target_unavailable"


def test_submit_task_intake_auto_selects_connected_session(service, project):
    creator, target = _join_ordered(service, project)
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Investigate the report latency",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    assert intake["status"] == "pending"
    assert intake["target_session_id"] == target["agent"]["id"]


def test_define_task_from_intake_creates_formal_task_and_links_back(service, project):
    creator, target = _join_ordered(service, project)
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Tighten retry policy on upload failure",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=target["agent"]["id"],
        token=target["token"],
        response="accepted",
    )
    defined = service.define_task_from_intake(
        project["id"],
        intake["id"],
        session_id=target["agent"]["id"],
        token=target["token"],
        title="Harden upload retry",
        description="Add bounded retries and a circuit breaker",
        acceptance_criteria=[
            "Retries are bounded by config and logged",
            "Circuit breaker trips after 3 consecutive failures",
        ],
        priority=1,
    )
    task = defined["task"]
    intake_after = defined["intake"]
    assert task["task_number"] == 1
    assert defined["assignment"]["assigned_to_session_id"] == target["agent"]["id"]
    assert defined["assignment"]["status"] == "pending"
    assert intake_after["status"] == "defined"
    assert intake_after["formal_task_id"] == task["id"]

    fetched_intake = service.get_task_intake(project["id"], intake["id"])
    assert fetched_intake["formal_task_id"] == task["id"]

    events = service.list_events(project["id"], after=0)["events"]
    sequence = [event["event_type"] for event in events]
    assert (
        sequence.index("task.created")
        < sequence.index("task.intake_defined")
        < sequence.index("task.assigned")
    )


def test_task_number_starts_at_one_and_advances_within_project(service, project):
    numbers = [
        service.create_task(
            project["id"],
            title=f"Task {index}",
            acceptance_criteria=["Pass"],
        )["task"]["task_number"]
        for index in range(3)
    ]
    assert numbers == [1, 2, 3]


def test_task_number_unique_constraint_rejects_duplicate(service, project):
    service.create_task(
        project["id"],
        title="Anchor",
        acceptance_criteria=["Pass"],
    )
    with service.database.connect(write=False) as connection:
        with pytest.raises(Exception):
            connection.execute(
                "INSERT INTO tasks("
                "id, project_id, task_number, title, acceptance_criteria_json,"
                " priority, status, execution_status, verification_status,"
                " integration_status, progress_percent, current_step,"
                " blocker_reason, next_step, created_at, updated_at)"
                " VALUES (?, ?, 1, 'dup', '[\"a\"]', 2, 'todo', 'todo',"
                " 'not_required', 'pending', 0, '', '', '', 'now', 'now')",
                ("task_dup", project["id"]),
            )


def test_task_number_is_immutable_across_lifecycle_changes(service, project, joined_agents):
    executor, reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Lifecycle",
        acceptance_criteria=["Pass"],
    )["task"]
    assert task["task_number"] == 1
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Lifecycle complete",
        files=["src/x.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Pass", "status": "passed"}],
    )
    service.submit_integration(
        project["id"],
        task["id"],
        result="done",
        summary="Merged",
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    fetched = service.get_task_by_number(project["id"], 1)
    assert fetched["id"] == task["id"]
    assert fetched["status"] == "done"
    assert fetched["task_number"] == 1


def test_get_task_by_number_returns_404_for_unknown_numbers(service, project):
    with pytest.raises(DomainError) as failure:
        service.get_task_by_number(project["id"], 99)
    assert failure.value.code == "task_not_found"


def test_define_task_from_intake_rejects_non_target_agent(service, project):
    creator, target = _join_ordered(service, project)
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Define-formal-task boundary check",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=target["agent"]["id"],
        token=target["token"],
        response="accepted",
    )
    with pytest.raises(DomainError) as failure:
        service.define_task_from_intake(
            project["id"],
            intake["id"],
            session_id=creator["agent"]["id"],
            token=creator["token"],
            title="Should fail",
            acceptance_criteria=["Pass"],
        )
    assert failure.value.code == "task_intake_not_for_agent"


def test_define_task_from_intake_requires_accepted_status(service, project):
    creator, target = _join_ordered(service, project)
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Pending intake cannot define",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    with pytest.raises(DomainError) as failure:
        service.define_task_from_intake(
            project["id"],
            intake["id"],
            session_id=target["agent"]["id"],
            token=target["token"],
            title="Skip accept",
            acceptance_criteria=["Pass"],
        )
    assert failure.value.code == "task_intake_not_accepted"


def test_decline_or_block_response_requires_note(service, project):
    creator, target = _join_ordered(service, project)
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Decline must record a reason",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    with pytest.raises(DomainError) as failure:
        service.acknowledge_task_intake(
            project["id"],
            intake["id"],
            session_id=target["agent"]["id"],
            token=target["token"],
            response="declined",
        )
    assert failure.value.code == "intake_response_note_required"

    blocked = service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=target["agent"]["id"],
        token=target["token"],
        response="blocked",
        note="Waiting on the upstream dependency",
    )
    assert blocked["intake"]["status"] == "blocked"
    assert blocked["intake"]["note"] == "Waiting on the upstream dependency"


def test_pending_intake_id_cannot_be_used_for_claim_or_work_report(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    creator, target = _join_ordered(service, project)
    intake = service.submit_task_intake(
        project["id"],
        raw_description="Pending intake must not be claimable",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )["intake"]
    with pytest.raises(DomainError) as failure:
        service.claim_task(
            project["id"], intake["id"], executor["agent"]["id"], executor["token"]
        )
    assert failure.value.code == "task_not_found"

    with pytest.raises(DomainError) as failure:
        service.submit_work_report(
            project["id"],
            intake["id"],
            session_id=executor["agent"]["id"],
            token=executor["token"],
            summary="Should not accept",
            files=["src/x.py"],
            tests=[{"command": "pytest", "exit_code": 0}],
        )
    assert failure.value.code == "task_not_found"


def test_intake_submit_records_event_with_requires_agent_acceptance(service, project):
    creator, target = _join_ordered(service, project)
    result = service.submit_task_intake(
        project["id"],
        raw_description="Verify intake event payload",
        target_member_id=target["agent"]["member_id"],
        created_by_session_id=creator["agent"]["id"],
        token=creator["token"],
    )
    intake_event = next(
        event
        for event in service.list_events(project["id"], after=0)["events"]
        if event["event_type"] == "task.intake_submitted"
    )
    assert intake_event["payload"]["requires_agent_acceptance"] is True
    assert intake_event["payload"]["intake_id"] == result["intake"]["id"]
