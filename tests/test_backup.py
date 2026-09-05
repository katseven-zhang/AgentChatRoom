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
