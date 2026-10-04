from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def test_default_task_priority_comes_from_project_settings(service, project):
    service.update_project(
        project["id"], settings={"default_task_priority": 0}
    )
    author = service.join_room(
        project["id"], name="Author", client="codex", model="unknown"
    )
    created = service.create_task(
        project["id"],
        title="uses project default",
        acceptance_criteria=["c1"],
        actor_session_id=author["agent"]["id"],
        token=author["token"],
    )
    assert created["task"]["priority"] == 0

    explicit = service.create_task(
        project["id"],
        title="explicit wins",
        acceptance_criteria=["c1"],
        priority=3,
        actor_session_id=author["agent"]["id"],
        token=author["token"],
    )
    assert explicit["task"]["priority"] == 3


def test_default_task_priority_validation(service, project):
    with pytest.raises(DomainError) as bad:
        service.update_project(
            project["id"], settings={"default_task_priority": 9}
        )
    assert bad.value.code == "invalid_project_settings"


def test_audit_retention_purges_only_expired_and_audits(service, project):
    from datetime import timedelta

    from agentchatroom.services import iso_now, utc_now

    service.post_message(project["id"], body="ancient")
    service.post_message(project["id"], body="recent keeper")
    # 把最早两条事件的时间戳改到 40 天前（仅测试夹具用途：构造"过期"数据）。
    with service.database.connect(write=True) as connection:
        stale = (utc_now() - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        rows = connection.execute(
            "SELECT id FROM events WHERE project_id = ? ORDER BY id ASC LIMIT 2",
            (project["id"],),
        ).fetchall()
        stale_ids = {row["id"] for row in rows}
        for row in rows:
            connection.execute(
                "UPDATE events SET created_at = ? WHERE id = ?",
                (stale, row["id"]),
            )

    before = service.query_audit(project["id"], limit=100)["events"]
    assert len(before) >= 3

    # 永久保留（默认）不清 anything。
    untouched = service.enforce_audit_retention(project["id"])
    assert untouched["purged"] == 0
    assert untouched["retention_days"] == 0
    assert untouched["purged_before"] is None

    service.update_project(
        project["id"], settings={"audit_retention_days": 30}
    )
    after = service.query_audit(project["id"], limit=100)["events"]
    remaining_ids = {event["id"] for event in after}
    for stale_id in stale_ids:
        assert stale_id not in remaining_ids
    assert "recent keeper" in " ".join(
        event["payload"].get("body", "")
        for event in after
        if event["event_type"] == "message.message"
    )
    assert all(event["id"] not in stale_ids or event["event_type"] == "audit.purged" for event in after)
    purge_events = service.query_audit(project["id"], event_type="audit.purged")["events"]
    assert purge_events and purge_events[-1]["payload"]["deleted_events"] == 2
    assert purge_events[-1]["payload"]["retention_days"] == 30


def _age_all_events(service, project_id, iso_timestamp):
    """Backdate every event so a retention sweep has something to remove."""
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE events SET created_at = ? WHERE project_id = ?",
            (iso_timestamp, project_id),
        )


def test_purge_receipt_survives_later_retention_windows(service, project):
    """#339: the audit.purged receipt must not be purged by its own successor."""
    service.update_project(project["id"], settings={"audit_retention_days": 30})
    session = service.join_room(
        project["id"], name="Worker", client="codex", model="unknown"
    )
    service.post_message(
        project["id"],
        body="first",
        session_id=session["agent"]["id"],
        token=session["token"],
        model_display_name="test-model",
    )

    service._age_all_events = None  # guard against accidental reuse
    _age_all_events(service, project["id"], "2000-01-01T00:00:00Z")
    first = service.enforce_audit_retention(project["id"])
    assert first["purged"] > 0
    assert first["purged_before"] is not None

    # A second window later must still leave the first receipt readable.
    _age_all_events(service, project["id"], "2000-01-01T00:00:00Z")
    second = service.enforce_audit_retention(project["id"])

    receipts = service.query_audit(project["id"], event_type="audit.purged")["events"]
    assert len(receipts) >= 1
    assert first["purged_before"] in [r["payload"]["purged_before"] for r in receipts]
    assert second["purged"] >= 0


def test_purge_does_not_cascade_away_acknowledgements(service, project):
    """#339: deleting an acknowledged event erased "who confirmed what, when"."""
    service.update_project(project["id"], settings={"audit_retention_days": 30})
    session = service.join_room(
        project["id"], name="Worker", client="codex", model="unknown"
    )
    posted = service.post_message(
        project["id"],
        body="needs confirmation",
        session_id=session["agent"]["id"],
        token=session["token"],
        model_display_name="test-model",
        requires_ack=True,
    )
    event_seq = posted["event_id"]
    service.acknowledge_event(
        project["id"],
        event_seq,
        session_id=session["agent"]["id"],
        token=session["token"],
    )

    _age_all_events(service, project["id"], "2000-01-01T00:00:00Z")
    result = service.enforce_audit_retention(project["id"])
    assert result["deferred_referenced"] >= 1

    remaining = {
        event["id"]
        for event in service.query_audit(project["id"], limit=200)["events"]
    }
    assert event_seq in remaining, "the acknowledged event was purged"
    # The acknowledgement row itself is the thing ON DELETE CASCADE would have
    # destroyed, so assert on the row rather than on an API projection.
    with service.database.connect() as connection:
        acks = connection.execute(
            "SELECT event_id FROM event_acknowledgements WHERE event_id = ?",
            (event_seq,),
        ).fetchall()
    assert [int(row["event_id"]) for row in acks] == [int(event_seq)]


def test_purge_defers_the_event_behind_a_task_spec_receipt(service, project, tmp_path):
    """#339: purging the claimed event silently degraded spec_receipt to {}."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    registered = service.register_workspace(
        project["id"],
        host_key="receipt-host",
        host_name="Receipt Host",
        local_path=str(worktree),
    )
    session = service.join_room(
        project["id"],
        name="Worker",
        client="codex",
        model="unknown",
        worktree=str(worktree),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    # The spec receipt is derived from the newest task.claimed that carries a
    # project-documents snapshot, so a binding document must exist first.
    service.upsert_project_document(
        project["id"],
        doc_key="standards",
        kind="binding",
        title="工程规范",
        content="先取租约再改共享文件。",
        actor="management",
        management_authorized=True,
    )
    task = service.create_task(
        project["id"],
        title="Receipt holder",
        acceptance_criteria=["bound documents are recorded"],
        actor_session_id=session["agent"]["id"],
        token=session["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], session["agent"]["id"], session["token"]
    )
    bound = service.get_task(project["id"], task["id"])
    assert bound["spec_receipt"], "fixture did not record a spec receipt"

    service.update_project(project["id"], settings={"audit_retention_days": 30})
    _age_all_events(service, project["id"], "2000-01-01T00:00:00Z")
    service.enforce_audit_retention(project["id"])

    assert service.get_task(project["id"], task["id"])["spec_receipt"], (
        "the spec receipt was destroyed by the retention purge"
    )


def test_read_paths_report_a_purged_before_marker(service, project):
    """#339: a client holding an old cursor must be able to tell a hole from a start."""
    service.update_project(project["id"], settings={"audit_retention_days": 30})
    session = service.join_room(
        project["id"], name="Worker", client="codex", model="unknown"
    )
    service.post_message(
        project["id"],
        body="before the purge",
        session_id=session["agent"]["id"],
        token=session["token"],
        model_display_name="test-model",
    )
    _age_all_events(service, project["id"], "2000-01-01T00:00:00Z")
    purged_before = service.enforce_audit_retention(project["id"])["purged_before"]
    assert purged_before

    listed = service.list_events(project["id"], after=0, limit=50)
    assert listed["purged_before"] == purged_before
    assert listed["cursor_is_purged"] is True

    at_head = service.list_events(project["id"], after=purged_before, limit=50)
    assert at_head["cursor_is_purged"] is False


def test_audit_retention_policy_validation(service, project):
    with pytest.raises(DomainError) as bad:
        service.update_project(
            project["id"], settings={"audit_retention_days": 45}
        )
    assert bad.value.code == "invalid_project_settings"


def test_backup_settings_roundtrip_via_management_api(settings, project_dir, tmp_path):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    local_settings = settings
    with TestClient(create_app(local_settings)) as client:
        current = client.get("/api/v1/admin/backup-settings")
        assert current.status_code == 200
        payload = current.json()
        assert payload["auto_backup_max_kept"] >= 1

        updated = client.put(
            "/api/v1/admin/backup-settings",
            json={"auto_backup_enabled": True, "auto_backup_max_kept": 3},
        )
        assert updated.status_code == 200
        assert updated.json()["auto_backup_enabled"] is True
        assert updated.json()["auto_backup_max_kept"] == 3

        rejected = client.put(
            "/api/v1/admin/backup-settings",
            json={"auto_backup_interval_seconds": 10},
        )
        assert rejected.status_code == 422
