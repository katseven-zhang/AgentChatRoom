"""#136: consistent task-mutation authority and bounded Session/task recovery.

The reporting Agent hit a dead end after ``session_expired`` -> bootstrap
created a new Session: the new Session could not release the old owner's task
(``not_task_owner``), could not reclaim it while the old owner was still
considered connected (``task_owner_session_connected``), and passing the old
Session id explicitly was rejected by the runtime binding
(``runtime_context_mismatch``). These tests pin the two halves of the fix:

* authority: a bare Session id, another Session of the same software identity,
  or a request without credentials can never release, reassign, or rewrite
  another Session's task (``task_update(status="todo")`` included), while an
  explicit management-authorized call still can;
* recovery: a genuinely unrecoverable owner is bounded and actionable
  (``required_action`` + ``retry_after_seconds``), and the sanctioned takeover
  is ``task_claim(reclaim=True)`` once the owner stops being connected.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


@pytest.fixture()
def quiet_service(settings):
    """A service with a short heartbeat window so reconnect waits stay bounded."""
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def _two_sessions(service, project_id: str):
    """Two live Sessions of one software identity, plus one foreign identity."""
    member = service.create_project_member(
        project_id, member_key="workbuddy", name="WorkBuddy"
    )["member"]
    first = service.join_room(
        project_id,
        name="WorkBuddy",
        client="workbuddy",
        model="unknown",
        member_id=member["id"],
    )
    second = service.join_room(
        project_id,
        name="WorkBuddy second conversation",
        client="workbuddy",
        model="unknown",
        member_id=member["id"],
    )
    foreign_member = service.create_project_member(
        project_id, member_key="qoder", name="Qoder"
    )["member"]
    foreign = service.join_room(
        project_id,
        name="Qoder",
        client="qoder",
        model="unknown",
        member_id=foreign_member["id"],
    )
    return first, second, foreign


def _claimed_task(service, project_id: str, owner):
    task = service.create_task(
        project_id,
        title="Recovery target",
        acceptance_criteria=["Owner keeps the task until it legitimately leaves"],
    )["task"]
    service.claim_task(project_id, task["id"], owner["agent"]["id"], owner["token"])
    return task


def _release_events(service, project_id: str, task_id: str) -> list[dict]:
    events = service.query_audit(project_id, task_id=task_id)["events"]
    return [event for event in events if event["event_type"] == "task.released"]


def test_task_update_todo_cannot_release_another_sessions_task(
    quiet_service, project
):
    owner, sibling, _ = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    with pytest.raises(DomainError) as denied:
        quiet_service.update_task(
            project["id"],
            task["id"],
            status="todo",
            session_id=sibling["agent"]["id"],
            token=sibling["token"],
        )

    assert denied.value.code == "not_task_owner"
    stored = quiet_service.get_task(project["id"], task["id"])
    assert stored["owner_session_id"] == owner["agent"]["id"]
    assert stored["execution_status"] == "claimed"
    assert _release_events(quiet_service, project["id"], task["id"]) == []


def test_only_the_owner_can_update_or_reassign_an_owned_task(quiet_service, project):
    owner, sibling, foreign = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    for intruder in (sibling, foreign):
        with pytest.raises(DomainError) as update_denied:
            quiet_service.update_task(
                project["id"],
                task["id"],
                progress_percent=99,
                session_id=intruder["agent"]["id"],
                token=intruder["token"],
            )
        assert update_denied.value.code == "not_task_owner"

        with pytest.raises(DomainError) as release_denied:
            quiet_service.release_task(
                project["id"],
                task["id"],
                reason_code="other",
                session_id=intruder["agent"]["id"],
                token=intruder["token"],
            )
        assert release_denied.value.code == "not_task_owner"

    stored = quiet_service.get_task(project["id"], task["id"])
    assert stored["owner_session_id"] == owner["agent"]["id"]
    assert stored["progress_percent"] == 0


def test_missing_credentials_are_not_management_authority(quiet_service, project):
    owner, _, _ = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    attempts = (
        lambda: quiet_service.release_task(
            project["id"], task["id"], reason_code="other"
        ),
        lambda: quiet_service.update_task(project["id"], task["id"], status="todo"),
        lambda: quiet_service.assign_task(
            project["id"], task["id"], target_role="executor"
        ),
    )
    for attempt in attempts:
        with pytest.raises(DomainError) as denied:
            attempt()
        assert denied.value.code == "management_auth_required"
        assert (
            denied.value.details["required_action"]
            == "authenticate_management_or_use_owner_credentials"
        )

    stored = quiet_service.get_task(project["id"], task["id"])
    assert stored["owner_session_id"] == owner["agent"]["id"]
    assert stored["execution_status"] == "claimed"


def test_explicit_management_authority_still_recovers_an_abandoned_task(
    quiet_service, project
):
    owner, _, _ = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    released = quiet_service.release_task(
        project["id"],
        task["id"],
        reason_code="agent_unavailable",
        reason="Owner client is gone; recover the work from the task board",
        management_authorized=True,
    )

    assert released["released"] is True
    assert quiet_service.get_task(project["id"], task["id"])["execution_status"] == "todo"
    events = _release_events(quiet_service, project["id"], task["id"])
    assert len(events) == 1
    assert events[0]["payload"]["initiator"] == "management"
    assert events[0]["payload"]["reason_code"] == "agent_unavailable"


def test_owner_status_todo_release_is_still_attributed_to_the_owner(
    quiet_service, project
):
    owner, _, _ = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    released = quiet_service.update_task(
        project["id"],
        task["id"],
        status="todo",
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )

    assert released["released"] is True
    events = _release_events(quiet_service, project["id"], task["id"])
    assert len(events) == 1
    assert events[0]["payload"]["initiator"] == "owner"
    assert events[0]["payload"]["initiator_session_id"] == owner["agent"]["id"]


def test_reclaim_states_a_bounded_next_step_and_rejects_the_status_bypass(
    quiet_service, project
):
    owner, sibling, _ = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    with pytest.raises(DomainError) as connected:
        quiet_service.claim_task(
            project["id"],
            task["id"],
            sibling["agent"]["id"],
            sibling["token"],
            reclaim=True,
        )

    details = connected.value.details
    assert connected.value.code == "task_owner_session_connected"
    assert details["required_action"] == "wait_for_owner_release_or_reclaim"
    assert details["owner_session_id"] == owner["agent"]["id"]
    assert details["retry_after_seconds"] >= 1
    assert details["heartbeat_timeout_seconds"] == 0.3
    assert details["forbidden_bypass"] == "task_update(status=todo)"
    # No transport liveness source is wired on a bare domain service, so the
    # bounded heartbeat window is the only evidence and it must say so.
    assert details["owner_transport_state"] == "unavailable"

    with pytest.raises(DomainError) as bypass:
        quiet_service.update_task(
            project["id"],
            task["id"],
            status="todo",
            session_id=sibling["agent"]["id"],
            token=sibling["token"],
        )
    assert bypass.value.code == "not_task_owner"

    # Once the window really lapses the same identity takes over the work.
    # Liveness is max(heartbeat, last domain activity), so a real lapse needs
    # both to age out; the fixture keeps that window at 0.3s.
    time.sleep(quiet_service.settings.heartbeat_timeout_seconds + 0.2)
    reclaimed = quiet_service.claim_task(
        project["id"],
        task["id"],
        sibling["agent"]["id"],
        sibling["token"],
        reclaim=True,
    )
    assert reclaimed["task"]["owner_session_id"] == sibling["agent"]["id"]


def test_transport_tombstone_allows_immediate_reclaim_without_the_window(
    quiet_service, project
):
    owner, sibling, _ = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    quiet_service.transport_liveness_check = lambda project_id, session_id: (
        "gone" if session_id == owner["agent"]["id"] else "unknown"
    )
    reclaimed = quiet_service.claim_task(
        project["id"],
        task["id"],
        sibling["agent"]["id"],
        sibling["token"],
        reclaim=True,
    )

    assert reclaimed["task"]["owner_session_id"] == sibling["agent"]["id"]


def test_foreign_identity_cannot_reclaim_a_live_sessions_task(quiet_service, project):
    owner, _, foreign = _two_sessions(quiet_service, project["id"])
    task = _claimed_task(quiet_service, project["id"], owner)

    with pytest.raises(DomainError) as denied:
        quiet_service.claim_task(
            project["id"],
            task["id"],
            foreign["agent"]["id"],
            foreign["token"],
            reclaim=True,
        )

    assert denied.value.code == "task_reclaim_forbidden"
    stored = quiet_service.get_task(project["id"], task["id"])
    assert stored["owner_session_id"] == owner["agent"]["id"]
