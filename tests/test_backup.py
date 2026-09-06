from __future__ import annotations

import sqlite3

import pytest

from agentchatroom.backup import BackupError, backup_sqlite, backup_postgresql, restore_sqlite
from agentchatroom.cli import main
from agentchatroom.database import Database, SCHEMA_VERSION


def test_sqlite_backup_and_restore_preserve_events(service, project, tmp_path):
    backup_path = tmp_path / "room-backup.sqlite"
    restored_path = tmp_path / "restored.sqlite"

    created = backup_sqlite(service.database.path, backup_path)
    assert created["backend"] == "sqlite"
    assert (tmp_path / "room-backup.sqlite.manifest.json").exists()

    restored = restore_sqlite(backup_path, restored_path)
    assert restored["backend"] == "sqlite"
    with sqlite3.connect(restored_path) as connection:
        project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        schema_version = connection.execute(
            "SELECT version FROM schema_meta LIMIT 1"
        ).fetchone()[0]
    assert project_count == 1
    assert event_count >= 1
    assert schema_version == SCHEMA_VERSION


def test_sqlite_restore_requires_explicit_overwrite( service, tmp_path):
    backup_path = tmp_path / "room-backup.sqlite"
    target_path = tmp_path / "existing.sqlite"
    backup_sqlite(service.database.path, backup_path)
    target_path.write_bytes(b"existing")

    with pytest.raises(BackupError, match="overwrite confirmation"):
        restore_sqlite(backup_path, target_path)


def test_postgresql_backup_reports_missing_client_tool(monkeypatch, tmp_path):
    monkeypatch.setattr("agentchatroom.backup.shutil.which", lambda _name: None)

    with pytest.raises(BackupError, match="pg_dump"):
        backup_postgresql(
            "postgresql://test-user:test-password@localhost/test-room",
            tmp_path / "room.dump",
        )


def test_cli_backup_and_restore_use_the_configured_sqlite_database(monkeypatch, tmp_path, capsys):
    data_dir = tmp_path / "runtime"
    Database(data_dir / "agentchatroom.db").initialize()
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(data_dir))
    backup_path = tmp_path / "cli-backup.sqlite"

    main(["backup", "--output", str(backup_path)])
    assert (json_output := capsys.readouterr().out)
    assert "cli-backup.sqlite" in json_output

    main(["restore", "--input", str(backup_path), "--confirm"])
    restore_output = capsys.readouterr().out
    assert "restored.sqlite" not in restore_output
    assert "cli-backup.sqlite" in restore_output


def test_service_create_backup_audits_and_prunes(service, project, tmp_path, monkeypatch):
    import json as json_module
    from pathlib import Path

    service.post_message(project["id"], body="before backup")
    created = service.create_backup(source="management")

    assert Path(created["output"]).is_file()
    events = service.query_audit(project["id"], event_type="backup.created")["events"]
    assert events and events[-1]["payload"]["output"] == created["output"]
    manifest = json_module.loads(Path(created["manifest"]).read_text(encoding="utf-8"))
    # The manifest cursor covers the audit event about the backup itself,
    # so a fresh backup is never considered stale.
    assert manifest["latest_event_id"] >= events[-1]["id"]
    assert created["pruned"] == []

    settings = service.settings
    object.__setattr__(settings, "auto_backup_max_kept", 1)
    second = service.create_backup(source="auto")
    assert len(second["pruned"]) == 1
    listed = service.list_backups()
    assert len(listed) <= 1
    assert all(Path(item["file"]).name.startswith("backup-") for item in listed)


def test_backup_retention_applies_immediately(service, project):
    """Setting 保留份数 must prune existing backups right away, not wait for
    the next backup (user feedback)."""
    from pathlib import Path

    created = [service.create_backup() for _ in range(4)]
    assert len(service.list_backups()) == 4

    pruned = service.enforce_backup_retention(2)
    assert len(pruned) == 2
    listed = service.list_backups()
    assert len(listed) == 2
    kept_names = {Path(item["file"]).name for item in listed}
    assert Path(created[-1]["output"]).name in kept_names

    # create_backup honours an explicit max_kept in the same way.
    service.create_backup(max_kept=1)
    assert len(service.list_backups()) == 1


def test_list_backups_hides_legacy_nonmanaged_snapshots(service, project, tmp_path):
    """迁移前 .db 旧快照不进管理列表（磁盘保留）。"""
    from pathlib import Path

    directory = service._backup_directory()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pre-some-migration-20260830.db").write_bytes(b"x")
    service.create_backup()
    listed = service.list_backups()
    assert [Path(item["file"]).suffix for item in listed] == [".sqlite"]
    assert all("pre-some-migration" not in item["file"] for item in listed)


def test_service_restore_rejects_unsafe_and_restores_with_confirmation(service, project):
    from agentchatroom.errors import DomainError

    service.post_message(project["id"], body="checkpoint")
    created = service.create_backup()

    # Missing confirm.
    with pytest.raises(DomainError) as missing:
        service.restore_backup(created["output"], confirm="")
    assert missing.value.code == "backup_confirm_required"

    # A fresh backup is never stale: restore succeeds after typed confirm.
    restored = service.restore_backup(created["output"], confirm="REPLACE")
    assert restored["restored"] is True and restored["data_loss"] is False
    post_events = service.query_audit(project["id"], event_type="backup.restore_completed")["events"]
    assert post_events

    # A stale backup (older than current latest event) needs allow_data_loss.
    stale = service.create_backup()
    service.post_message(project["id"], body="newer than backup")
    with pytest.raises(DomainError) as rejected:
        service.restore_backup(stale["output"], confirm="REPLACE")
    assert rejected.value.code == "backup_stale"
    rejections = service.query_audit(project["id"], event_type="backup.restore_rejected")["events"]
    assert rejections and rejections[-1]["payload"]["reason"] == "backup_stale"
    restored_with_loss = service.restore_backup(
        stale["output"], confirm="REPLACE", allow_data_loss=True
    )
    assert restored_with_loss["data_loss"] is True


def test_service_restore_rejects_schema_mismatch(service, project):
    import json as json_module

    from agentchatroom.errors import DomainError

    created = service.create_backup()
    manifest_path = created["manifest"]
    manifest = json_module.loads(open(manifest_path, encoding="utf-8").read())
    manifest["database_schema_version"] = 999
    open(manifest_path, "w", encoding="utf-8").write(json_module.dumps(manifest))
    with pytest.raises(DomainError) as mismatch:
        service.restore_backup(created["output"], confirm="REPLACE")
    assert mismatch.value.code == "backup_schema_mismatch"


def test_service_restore_reports_busy_database(service, project):
    import sqlite3

    from agentchatroom.errors import DomainError

    created = service.create_backup()
    holder = sqlite3.connect(service.database.path, timeout=0.1)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DomainError) as busy:
            service.restore_backup(created["output"], confirm="REPLACE")
        assert busy.value.code == "database_busy"
    finally:
        holder.rollback()
        holder.close()


def test_auto_backup_cycle_creates_backup_and_audits(service, project):
    from agentchatroom.api import run_auto_backup_cycle

    result = run_auto_backup_cycle(service, max_kept=5)
    assert result is not None
    from pathlib import Path

    assert Path(result["output"]).is_file()
    events = service.query_audit(project["id"], event_type="backup.created")["events"]
    assert events[-1]["payload"]["source"] == "auto"


def test_auto_backup_cycle_swallows_and_logs_errors():
    import logging

    from agentchatroom.api import run_auto_backup_cycle

    class Flaky:
        def create_backup(self, *, source):
            raise RuntimeError("boom")

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    logger = logging.getLogger("agentchatroom.api")
    logger.addHandler(handler)
    try:
        assert run_auto_backup_cycle(Flaky()) is None
    finally:
        logger.removeHandler(handler)
    assert any(record.getMessage() == "automatic backup failed" for record in records)


class _WorkerSettings:
    auto_backup_enabled = True
    auto_backup_interval_seconds = 3600
    auto_backup_max_kept = 10


def test_auto_backup_loop_continues_after_failure():
    from agentchatroom.api import auto_backup_worker

    calls: list[str] = []

    class Stub:
        def create_backup(self, *, source, max_kept=None):
            calls.append(source)
            if len(calls) == 1:
                raise RuntimeError("first cycle fails")
            return {"ok": True}

    class FakeEvent:
        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout):
            self.wait_calls += 1
            return self.wait_calls >= 3

    auto_backup_worker(Stub(), _WorkerSettings(), FakeEvent())
    assert calls == ["auto", "auto"]


def test_auto_backup_loop_skips_cycles_when_disabled():
    from agentchatroom.api import auto_backup_worker

    calls: list[str] = []

    class Stub:
        def create_backup(self, *, source, max_kept=None):
            calls.append(source)
            return {"ok": True}

    class DisabledSettings(_WorkerSettings):
        auto_backup_enabled = False

    class FakeEvent:
        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout):
            self.wait_calls += 1
            return self.wait_calls >= 3

    auto_backup_worker(Stub(), DisabledSettings(), FakeEvent())
    assert calls == []


def test_start_auto_backup_worker_always_starts_daemon_thread(service, monkeypatch):
    """The worker thread starts unconditionally; per-cycle gating happens in
    the loop so management toggles apply without a restart."""
    import threading

    from agentchatroom.api import start_auto_backup_worker

    class DisabledSettings:
        auto_backup_enabled = False
        auto_backup_interval_seconds = 3600
        auto_backup_max_kept = 10

    started: dict = {}

    class FakeThread:
        def __init__(self, target=None, args=None, name=None, daemon=None):
            started["name"] = name
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(threading, "Thread", FakeThread)
    worker = start_auto_backup_worker(service, DisabledSettings(), threading.Event())
    assert started == {"name": "agentchatroom-auto-backup", "daemon": True, "started": True}
    assert worker is not None


def test_service_delete_backup_success_and_audit(service, project):
    from pathlib import Path

    created = service.create_backup(source="management")
    output_path = Path(created["output"])
    manifest_path = Path(created["manifest"])
    assert output_path.is_file()
    assert manifest_path.is_file()

    filename = output_path.name
    result = service.delete_backup(filename, actor="admin_tester")
    assert result["deleted"] is True
    assert result["filename"] == filename
    assert not output_path.exists()
    assert not manifest_path.exists()

    events = service.query_audit(project["id"], event_type="backup.deleted")["events"]
    assert len(events) >= 1
    last_event = events[-1]
    assert last_event["payload"]["filename"] == filename
    assert last_event["payload"]["actor"] == "admin_tester"


def test_service_delete_backup_security_and_validation(service, project, tmp_path):
    import pytest
    from agentchatroom.errors import DomainError

    # Invalid filename format (directory traversal, wrong extension, wrong prefix)
    for bad in [
        "../backup-foo.sqlite",
        "backup-foo.db",
        "not-a-backup.sqlite",
        "backup-with/slash.sqlite",
        "",
        "backup-foo.sqlite.manifest.json",
    ]:
        with pytest.raises(DomainError) as exc:
            service.delete_backup(bad)
        assert exc.value.code == "invalid_backup_filename"

    # Non-existent backup
    with pytest.raises(DomainError) as exc:
        service.delete_backup("backup-nonexistent2026.sqlite")
    assert exc.value.code == "backup_not_found"

    # Legacy snapshot in backup directory must not be deletable
    directory = service._backup_directory()
    directory.mkdir(parents=True, exist_ok=True)
    legacy_file = directory / "pre-migration-old.db"
    legacy_file.write_bytes(b"legacy")
    with pytest.raises(DomainError) as exc:
        service.delete_backup(legacy_file.name)
    assert exc.value.code == "invalid_backup_filename"
    assert legacy_file.is_file()


def test_api_delete_managed_backup(service, project):
    from pathlib import Path
    from starlette.testclient import TestClient
    from agentchatroom.api import create_app

    settings = service.settings
    created = service.create_backup()
    filename = Path(created["output"]).name

    app = create_app(settings)
    with TestClient(app) as client:
        # Success deletion
        response = client.delete(f"/api/v1/admin/backups/{filename}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        # 404 when deleting already deleted backup
        response = client.delete(f"/api/v1/admin/backups/{filename}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "backup_not_found"

        # 400 for invalid filename
        response = client.delete("/api/v1/admin/backups/invalid-file.txt")
        assert response.status_code == 400


def test_api_delete_managed_backup_requires_auth(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from agentchatroom.api import create_app
    from agentchatroom.config import Settings
    from agentchatroom.database import Database

    token = "a" * 24
    monkeypatch.setenv("AGENTCHATROOM_MANAGEMENT_TOKEN", token)
    monkeypatch.setenv("AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED", "true")
    data_dir = tmp_path / "runtime"
    Database(data_dir / "agentchatroom.db").initialize()

    settings = Settings(
        data_dir=data_dir,
        management_auth_required=True,
        management_token_env="AGENTCHATROOM_MANAGEMENT_TOKEN",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        resp = client.delete("/api/v1/admin/backups/backup-test.sqlite")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "management_auth_required"

        resp = client.delete(
            "/api/v1/admin/backups/backup-test.sqlite",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
