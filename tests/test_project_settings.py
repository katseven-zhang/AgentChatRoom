from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def test_default_task_priority_comes_from_project_settings(service, project):
    service.update_project(
        project["id"], settings={"default_task_priority": 0}
    )
    created = service.create_task(
        project["id"], title="uses project default", acceptance_criteria=["c1"]
    )
    assert created["task"]["priority"] == 0

    explicit = service.create_task(
        project["id"],
        title="explicit wins",
        acceptance_criteria=["c1"],
        priority=3,
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
    assert untouched == {"purged": 0, "retention_days": 0}

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
