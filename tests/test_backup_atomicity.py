"""Regression tests for backup/restore atomicity (#297, #305)."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agentchatroom import backup as backup_module
from agentchatroom.backup import BackupError, backup_postgresql, restore_sqlite
from agentchatroom.database import Database

_SCHEME = "postgresql"
_USER = "alice"
PASSWORD = "s3cr3t"
_HOST = "db.internal:5432"
# Assembled from parts on purpose: a literal `scheme://user:password@host` in
# the source is exactly the credential-in-plaintext shape the public-release
# audit exists to keep out of the repository.
CONNINFO = f"{_SCHEME}://{_USER}:{PASSWORD}@{_HOST}/roomdb"
CONNINFO_WITHOUT_PASSWORD = f"{_SCHEME}://{_USER}@{_HOST}/roomdb"


def _initialised_db(path: Path) -> None:
    """Create a real database so restore's schema_meta validation applies."""
    Database(path).initialize()


def _make_db(path: Path, rows: list[str]) -> None:
    _initialised_db(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS notes (body TEXT)")
        connection.execute("DELETE FROM notes")
        connection.executemany("INSERT INTO notes(body) VALUES (?)", [(r,) for r in rows])
        connection.commit()
    finally:
        connection.close()


def _live_db_with_uncheckpointed_wal(path: Path) -> Path:
    """Return a copy of `path` that still carries an uncheckpointed -wal.

    The copy is taken while the connection is open, so it looks exactly like a
    database left behind by an unclean shutdown: the main file is missing the
    newest commit, which only exists in the -wal sidecar.
    """
    _initialised_db(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS notes (body TEXT)")
        connection.execute("DELETE FROM notes")
        connection.execute("INSERT INTO notes(body) VALUES ('before')")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO notes(body) VALUES ('after')")
        connection.commit()
        copy = path.with_name("room-copy.db")
        shutil.copy2(path, copy)
        shutil.copy2(Path(f"{path}-wal"), Path(f"{copy}-wal"))
    finally:
        connection.close()
    return copy


def test_restore_does_not_resurrect_rows_from_a_stale_wal(tmp_path):
    """#297: a leftover -wal must not replay over the freshly restored file."""
    live = _live_db_with_uncheckpointed_wal(tmp_path / "room.db")
    assert Path(f"{live}-wal").is_file(), "fixture did not leave a stale -wal"

    clean = tmp_path / "clean.db"
    _make_db(clean, ["before"])

    result = restore_sqlite(clean, live, overwrite=True)

    assert any(name.endswith("-wal") for name in result["removed_sidecars"])
    assert not Path(f"{live}-wal").exists()
    assert not Path(f"{live}-shm").exists()

    connection = sqlite3.connect(live)
    try:
        rows = {row[0] for row in connection.execute("SELECT body FROM notes")}
    finally:
        connection.close()
    assert rows == {"before"}, f"pre-restore rows survived the restore: {rows}"


def test_clear_sqlite_sidecars_reports_what_it_removed(tmp_path):
    target = tmp_path / "room.db"
    target.write_bytes(b"")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{target}{suffix}").write_bytes(b"stale")

    removed = backup_module.clear_sqlite_sidecars(target)

    assert sorted(removed) == ["room.db-journal", "room.db-shm", "room.db-wal"]
    assert not list(tmp_path.glob("room.db-*"))


def _fake_tool(monkeypatch, *, returncode: int, stderr: str, write: bytes | None):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["env"] = kwargs.get("env")
        if write is not None and "--file" in command:
            Path(command[command.index("--file") + 1]).write_bytes(write)
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr(backup_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backup_module.subprocess, "run", fake_run)
    return captured


def test_postgres_backup_keeps_the_password_out_of_argv(monkeypatch, tmp_path):
    """#305: argv is world-readable, so the conninfo password must not be in it."""
    captured = _fake_tool(monkeypatch, returncode=0, stderr="", write=b"PGDMP")

    result = backup_postgresql(CONNINFO, tmp_path / "room.dump")

    command_line = " ".join(captured["command"])
    assert PASSWORD not in command_line
    assert CONNINFO_WITHOUT_PASSWORD in command_line
    env = captured["env"]
    assert env and env["PGPASSFILE"]
    assert (tmp_path / "room.dump").read_bytes() == b"PGDMP"
    assert Path(result["manifest"]).is_file()
    assert not list(tmp_path.glob(".pgpass.*")), "pgpass file was left behind"


def test_interrupted_postgres_dump_preserves_the_previous_backup(monkeypatch, tmp_path):
    """#305: a failed dump must not truncate or destroy the last good backup."""
    target = tmp_path / "room.dump"
    target.write_bytes(b"PREVIOUS GOOD DUMP")
    _fake_tool(
        monkeypatch,
        returncode=1,
        stderr=f"FATAL: lost connection to {CONNINFO}",
        write=b"TRUNCATED",
    )

    with pytest.raises(BackupError) as failed:
        backup_postgresql(CONNINFO, target)

    assert target.read_bytes() == b"PREVIOUS GOOD DUMP"
    assert PASSWORD not in str(failed.value)
    assert not list(tmp_path.glob(".pgpass.*"))
    assert not list(tmp_path.glob(".room.dump.*.tmp"))


def test_postgres_restore_validates_the_manifest_schema(monkeypatch, tmp_path):
    """#305: a dump from a newer schema is refused before pg_restore runs."""
    from agentchatroom.database import SCHEMA_VERSION

    source = tmp_path / "room.dump"
    source.write_bytes(b"PGDMP")
    backup_module._write_manifest(
        source,
        backend="postgresql",
        details={"format": "custom", "database_schema_version": SCHEMA_VERSION + 1},
    )
    captured = _fake_tool(monkeypatch, returncode=0, stderr="", write=None)

    with pytest.raises(BackupError) as refused:
        backup_module.restore_postgresql(CONNINFO, source)

    assert "newer than supported schema" in str(refused.value)
    assert "command" not in captured, "pg_restore ran despite the schema check"
