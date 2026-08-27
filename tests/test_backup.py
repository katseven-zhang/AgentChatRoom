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
