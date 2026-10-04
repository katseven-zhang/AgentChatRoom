"""Task #340: correlation and idempotency are different keys.

``X-Request-ID`` is a correlation header that a gateway commonly reuses for a
whole connection. Treating it as an idempotency key made two genuinely
different writes collide with ``idempotency_conflict`` 409 for the entire
retention window, and a replayed lease or heartbeat answered "acquired" long
after the protection had lapsed.

These tests pin the three behaviours: only an explicit Idempotency-Key
deduplicates, a TTL-bound write re-executes instead of replaying a stale
success, and the record table is swept without anyone reading the audit log.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


# --- the guard itself -------------------------------------------------------


def test_the_lease_replay_guard_reads_live_state_not_the_stored_deadline(
    service, project_dir
):
    """The stored deadline is a grant-time snapshot and cannot be trusted."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "guarded")
    session_id, token = agent["agent"]["id"], agent["token"]

    acquired = service.acquire_lease(
        project_id,
        session_id=session_id,
        token=token,
        path_pattern="src/agentchatroom/services.py",
    )
    recorded = {"lease": acquired["lease"]}

    assert service._lease_replay_is_live(recorded) is True

    # Expire the row. The recorded deadline still says "future", so a guard
    # reading the snapshot would wrongly call this live.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE file_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (acquired["lease"]["id"],),
        )
    assert acquired["lease"]["expires_at"] > "2000-01-01T00:00:00Z"
    assert service._lease_replay_is_live(recorded) is False

    # Releasing it ends the protection just as expiry does.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE file_leases SET expires_at = '2999-01-01T00:00:00Z',"
            " released_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (acquired["lease"]["id"],),
        )
    assert service._lease_replay_is_live(recorded) is False

    # A response describing no lease at all is left to the durable path.
    assert service._lease_replay_is_live({"lease": {}}) is True


def test_the_session_replay_guard_reflects_an_expired_token(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "sessional")
    session_id = agent["agent"]["id"]

    assert service._session_replay_is_live({"id": session_id}) is True

    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET token_expires_at = '2000-01-01T00:00:00Z'"
            " WHERE id = ?",
            (session_id,),
        )
    assert service._session_replay_is_live({"id": session_id}) is False

    # A Session that left is no longer alive either.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET token_expires_at = '2999-01-01T00:00:00Z',"
            " left_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (session_id,),
        )
    assert service._session_replay_is_live({"id": session_id}) is False

    # No session in the record means nothing to check.
    assert service._session_replay_is_live({"credential": {}}) is True


# --- header separation ------------------------------------------------------


def test_two_different_writes_under_one_x_request_id_do_not_conflict(
    settings, project_dir
):
    """The reported failure: a gateway reuses X-Request-ID per connection."""
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    app = create_app(settings)
    service = app.state.service
    project = service.create_project(root_path=str(project_dir), name="Correlation")
    project_id = project["id"]
    agent = _join(service, project_id, "writer-a")
    session_id = agent["agent"]["id"]
    token = agent["token"]

    client = TestClient(app)
    shared = {"X-Request-ID": "gateway-connection-7"}

    first = client.post(
        f"/api/v1/projects/{project_id}/messages",
        headers=shared,
        json={
            "session_id": session_id,
            "token": token,
            "body": "first message under a shared correlation id",
            "model_display_name": "pytest",
        },
    )
    second = client.post(
        f"/api/v1/projects/{project_id}/messages",
        headers=shared,
        json={
            "session_id": session_id,
            "token": token,
            "body": "second, different message under the same id",
            "model_display_name": "pytest",
        },
    )

    assert first.status_code == 201, first.text
    # The regression: 409 idempotency_conflict, valid for the whole window.
    assert second.status_code == 201, second.text
    assert second.json()["event_id"] != first.json()["event_id"]


def test_an_explicit_idempotency_key_still_deduplicates(settings, project_dir):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    app = create_app(settings)
    service = app.state.service
    project = service.create_project(root_path=str(project_dir), name="Idempotency")
    project_id = project["id"]
    agent = _join(service, project_id, "writer-b")
    payload = {
        "session_id": agent["agent"]["id"],
        "token": agent["token"],
        "body": "exactly the same body, sent twice on purpose",
        "model_display_name": "pytest",
    }

    client = TestClient(app)
    headers = {"Idempotency-Key": "caller-supplied-key-1"}

    first = client.post(
        f"/api/v1/projects/{project_id}/messages", headers=headers, json=payload
    )
    second = client.post(
        f"/api/v1/projects/{project_id}/messages", headers=headers, json=payload
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["event_id"] == first.json()["event_id"]
    assert second.json()["idempotent_replay"] is True


# --- TTL-bound writes must not replay a lapsed success ----------------------


def test_replaying_an_expired_lease_acquires_a_new_one(service, project_dir):
    """A recorded "acquired" answer must not outlive the lease it describes."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "lessee")
    session_id, token = agent["agent"]["id"], agent["token"]

    acquired = service.acquire_lease(
        project_id,
        session_id=session_id,
        token=token,
        path_pattern="src/agentchatroom/services.py",
        request_id="lease-attempt-1",
    )
    assert acquired["idempotent_replay"] is False

    # Still live: the same key replays rather than taking a second lease.
    replayed = service.acquire_lease(
        project_id,
        session_id=session_id,
        token=token,
        path_pattern="src/agentchatroom/services.py",
        request_id="lease-attempt-1",
    )
    assert replayed["idempotent_replay"] is True
    assert replayed["lease"]["id"] == acquired["lease"]["id"]

    # After the lease lapses the recorded success is worthless, so the same
    # key must run again and hand back a live lease.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE file_leases SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (acquired["lease"]["id"],),
        )

    reacquired = service.acquire_lease(
        project_id,
        session_id=session_id,
        token=token,
        path_pattern="src/agentchatroom/services.py",
        request_id="lease-attempt-1",
    )
    assert reacquired["idempotent_replay"] is False
    assert reacquired["lease"]["id"] != acquired["lease"]["id"]


# --- retention is driven by writes, not by audit reads ----------------------


def test_writing_sweeps_the_idempotency_table_without_any_audit_read(
    service, project_dir
):
    """A long-running desktop that never reads the log still cleans up."""
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    agent = _join(service, project_id, "sweeper")
    session_id, token = agent["agent"]["id"], agent["token"]

    # A real write, so the table has a row the way a long-running process does.
    service.acquire_lease(
        project_id,
        session_id=session_id,
        token=token,
        path_pattern="src/agentchatroom/services.py",
        request_id="sweep-me",
    )
    with service.database.connect() as connection:
        before = connection.execute(
            "SELECT COUNT(*) AS total FROM idempotency_records"
        ).fetchone()["total"]
    assert before > 0

    # Age every record far past the window, then let a write sweep them.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE idempotency_records SET created_at = '2000-01-01T00:00:00Z'"
        )

    # force=True bypasses only the throttle, so this proves the sweep itself
    # removes the aged rows rather than merely being scheduled.
    result = service.enforce_idempotency_retention(force=True)
    assert result["deleted"] > 0
    with service.database.connect() as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) AS total FROM idempotency_records"
        ).fetchone()["total"]
    assert remaining == 0
