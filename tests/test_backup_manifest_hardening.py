"""Backup manifest and retention hardening (#250, #295, #352, #240)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentchatroom.config import Settings
from agentchatroom.database import SCHEMA_VERSION, Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


def _managed_backup(service, project, *, cursor: int | None = 1) -> Path:
    """Create a managed backup through the service and return its file."""
    result = service.create_backup(source="test", actor_session_id=None)
    target = Path(result["output"])
    manifest_path = target.with_name(target.name + ".manifest.json")
    if cursor is None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("latest_event_id", None)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latest_event_id"] = cursor
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return target


def test_a_corrupt_manifest_does_not_break_the_backup_listing(service, project):
    """#250: one bad manifest must not 500 the page or stop auto backups."""
    good = _managed_backup(service, project)

    broken = good.with_name("backup-20990101T000000Z.sqlite")
    broken.write_bytes(b"not really a database")
    broken.with_name(broken.name + ".manifest.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    listed = service.list_backups()
    by_name = {Path(item["file"]).name: item for item in listed}

    assert "backup-20990101T000000Z.sqlite" in by_name
    assert by_name["backup-20990101T000000Z.sqlite"]["manifest_readable"] is False
    assert by_name[good.name]["manifest_readable"] is True


def test_an_undecodable_manifest_is_also_tolerated(service, project):
    """#250: binary garbage in a manifest is a listing detail, not a crash."""
    _managed_backup(service, project)
    broken = Path(service._backup_directory()) / "backup-20990102T000000Z.sqlite"
    broken.write_bytes(b"\x00\x01\x02binary")
    broken.with_name(broken.name + ".manifest.json").write_bytes(b"\xff\xfe\x00binary")

    listed = {Path(item["file"]).name: item for item in service.list_backups()}

    entry = listed.get("backup-20990102T000000Z.sqlite")
    assert entry is not None
    assert entry["manifest_readable"] is False


def test_pruning_still_sweeps_when_a_manifest_is_corrupt(service, project):
    """#250: retention must keep working with one unreadable manifest."""
    _managed_backup(service, project)
    broken = Path(service._backup_directory()) / "backup-20990103T000000Z.sqlite"
    broken.write_bytes(b"x")
    broken.with_name(broken.name + ".manifest.json").write_text("not json", encoding="utf-8")

    removed = service.enforce_backup_retention(0)

    assert any("20990103" in item for item in removed)
    assert not broken.exists()


def test_a_manifest_without_a_cursor_is_not_treated_as_cursor_zero(service, project):
    """#295: "no cursor recorded" must not silently read as "ancient backup"."""
    target = _managed_backup(service, project, cursor=None)
    session = service.join_room(
        project["id"], name="Writer", client="codex", model="unknown"
    )
    service.post_message(
        project["id"],
        body="newer than the backup",
        session_id=session["agent"]["id"],
        token=session["token"],
        model_display_name="test-model",
    )

    with pytest.raises(DomainError) as unknown:
        service.restore_backup(str(target), confirm="REPLACE")

    assert unknown.value.code == "backup_cursor_unknown"
    assert unknown.value.details["backup_latest_event_id"] is None


def test_restore_still_accepts_a_recorded_cursor(service, project):
    """#295: the normal path keeps working after the unknown-cursor split."""
    target = _managed_backup(service, project, cursor=10_000)
    result = service.restore_backup(str(target), confirm="REPLACE")

    assert result["restored"] is True


def test_saving_a_larger_retention_is_not_undone_by_a_later_backup(tmp_path):
    """#352: a manual backup must prune with the applied count, not startup's."""
    # The startup snapshot keeps 1; the operator then raises retention to 5.
    # A later manual "back up now" used to prune back to 1 and irreversibly
    # delete backups the operator asked to keep.
    settings = replace(
        Settings(data_dir=tmp_path / "data", heartbeat_timeout_seconds=30),
        auto_backup_max_kept=1,
    )
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Retained")

    service.enforce_backup_retention(5)
    for _ in range(3):
        service.create_backup(source="test", actor_session_id=None)
    assert len(service.list_backups()) == 3

    service.create_backup(source="test", actor_session_id=None)

    remaining = len(service.list_backups())
    assert remaining == 4, (
        f"retention 5 was undone by a later manual backup ({remaining} left)"
    )


def test_restore_refuses_a_path_outside_the_backups_directory(service, project, tmp_path):
    """#240: a management request must not be able to install any file as the DB."""
    outsider = tmp_path / "backup-not-really.sqlite"
    outsider.write_bytes(b"arbitrary bytes an attacker controls")
    outsider.with_name(outsider.name + ".manifest.json").write_text(
        json.dumps({"database_schema_version": SCHEMA_VERSION, "latest_event_id": 10**9}),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as refused:
        service.restore_backup(str(outsider), confirm="REPLACE", allow_data_loss=True)

    assert refused.value.code == "backup_path_outside_root"
    assert refused.value.status_code == 403


def test_restore_refuses_an_unparseable_manifest(service, project):
    """#240: an unverifiable snapshot is never copied over the live database."""
    target = _managed_backup(service, project)
    target.with_name(target.name + ".manifest.json").write_text(
        "{ broken", encoding="utf-8"
    )

    with pytest.raises(DomainError) as refused:
        service.restore_backup(str(target), confirm="REPLACE", allow_data_loss=True)

    assert refused.value.code == "backup_manifest_unreadable"


def test_postgres_restore_audits_after_the_restore_runs(service, project, monkeypatch):
    """#249: pg_restore rewrites its target, destroying a pre-written audit row."""
    target = _managed_backup(service, project, cursor=10**9)
    order: list[str] = []
    monkeypatch.setattr(
        "agentchatroom.services.restore_postgresql",
        lambda *_args, **_kwargs: order.append("pg_restore"),
    )
    real_emit = service._emit_backup_event

    def recording_emit(event_type, **kwargs):
        order.append(str(event_type))
        return real_emit(event_type, **kwargs)

    monkeypatch.setattr(service, "_emit_backup_event", recording_emit)
    # Route the restore down the PostgreSQL branch.
    monkeypatch.setattr(
        service, "settings", replace(service.settings, database_backend="postgresql")
    )

    service.restore_backup(str(target), confirm="REPLACE")

    assert "pg_restore" in order, "the PostgreSQL branch was never exercised"
    assert order.index("pg_restore") < order.index("backup.restore_started"), (
        "restore_started was written before pg_restore, so pg_restore deleted it"
    )


def test_interrupted_manifest_write_leaves_the_published_file(tmp_path, monkeypatch):
    """#250: a failed publish must not replace a complete manifest with a partial one."""
    import os

    from agentchatroom.backup import _write_manifest

    target = tmp_path / "backup-keep.sqlite"
    target.write_bytes(b"x")
    published = target.with_name(target.name + ".manifest.json")
    published.write_text('{"complete": true}\n', encoding="utf-8")
    original = published.read_bytes()

    def fail_replace(src, dst):
        raise OSError("interrupted before publish")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="interrupted before publish"):
        _write_manifest(
            target, backend="sqlite", details={"source": "test"}, latest_event_id=3
        )

    assert published.read_bytes() == original
    assert [path for path in tmp_path.iterdir() if path.suffix == ".tmp"] == []


def test_postgres_restore_blocks_a_writer_until_pg_restore_returns(
    service, project, monkeypatch
):
    """#249: a second writer cannot commit while pg_restore is still running."""
    import threading
    from contextlib import contextmanager

    target = _managed_backup(service, project, cursor=10**9)
    lock = threading.Lock()
    phases: list[str] = []

    @contextmanager
    def hold_restore_lock():
        assert lock.acquire(timeout=2)
        phases.append("locked")
        try:
            yield
        finally:
            phases.append("unlocked")
            lock.release()

    service.database.hold_restore_lock = hold_restore_lock
    monkeypatch.setattr(
        service, "settings", replace(service.settings, database_backend="postgresql")
    )
    writer_started = threading.Event()
    writer_finished = threading.Event()

    def writer():
        writer_started.set()
        acquired = lock.acquire(timeout=2)
        phases.append("writer" if acquired else "writer-gave-up")
        if acquired:
            lock.release()
        writer_finished.set()

    def fake_restore(*_args, **_kwargs):
        phases.append("pg_restore")
        thread = threading.Thread(target=writer)
        thread.start()
        assert writer_started.wait(timeout=2)
        thread.join(timeout=0.2)
        assert not writer_finished.is_set()
        phases.append("pg_restore_done")

    monkeypatch.setattr("agentchatroom.services.restore_postgresql", fake_restore)

    service.restore_backup(str(target), confirm="REPLACE")

    assert writer_finished.wait(timeout=2)
    assert phases.index("locked") < phases.index("pg_restore")
    assert phases.index("pg_restore_done") < phases.index("unlocked")
    assert phases.index("unlocked") < phases.index("writer")


def test_postgres_restore_lock_is_a_session_advisory_lock():
    """#249: the shipped PostgreSQL lock is the same key writers already take."""
    from agentchatroom.postgres_database import (
        POSTGRES_WRITE_LOCK_KEY,
        PostgresDatabase,
    )

    calls: list[tuple[str, object]] = []

    class Raw:
        def execute(self, sql, params=None):
            calls.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Pool:
        def connection(self, timeout=None):
            return Raw()

    database = PostgresDatabase.__new__(PostgresDatabase)
    database._opened = True
    database.connect_timeout_seconds = 1
    database._pool = Pool()

    with database.hold_restore_lock():
        assert calls[0] == (
            "SELECT pg_advisory_lock(%s)",
            (POSTGRES_WRITE_LOCK_KEY,),
        )
    assert calls[1][0] == "SELECT pg_advisory_unlock(%s)"
    assert calls[1][1] == (POSTGRES_WRITE_LOCK_KEY,)


def test_cli_backup_stamps_the_event_cursor(service, project, tmp_path):
    """#295: a backup made outside the service must not be cursor-unknown."""
    from agentchatroom.backup import backup_sqlite

    session = service.join_room(
        project["id"], name="Worker", client="codex", model="unknown"
    )
    service.post_message(
        project["id"],
        body="before the backup",
        session_id=session["agent"]["id"],
        token=session["token"],
        model_display_name="test-model",
    )
    cursor = service._latest_event_id()
    assert cursor > 0

    stamped = backup_sqlite(
        service.database.path, tmp_path / "backup-cli.sqlite", latest_event_id=cursor
    )
    manifest = json.loads(Path(stamped["manifest"]).read_text(encoding="utf-8"))
    assert manifest["latest_event_id"] == cursor

    # A caller that genuinely cannot read the cursor still writes no cursor,
    # and the restore refuses instead of guessing. The file must sit inside the
    # managed directory, otherwise #240's path guard answers first.
    managed = Path(service._backup_directory())
    managed.mkdir(parents=True, exist_ok=True)
    bare_target = managed / "backup-bare.sqlite"
    bare = backup_sqlite(service.database.path, bare_target)
    bare_manifest = json.loads(Path(bare["manifest"]).read_text(encoding="utf-8"))
    assert "latest_event_id" not in bare_manifest

    with pytest.raises(DomainError) as unknown:
        service.restore_backup(str(bare_target), confirm="REPLACE")
    assert unknown.value.code == "backup_cursor_unknown"


def test_every_migrated_index_exists_in_a_fresh_install(tmp_path):
    """#296: a fresh install and a migrated database must not diverge."""
    import re
    import sqlite3
    from pathlib import Path as _Path

    from agentchatroom import database as database_module

    source = _Path(database_module.__file__).read_text(encoding="utf-8")
    split_at = source.index("MIGRATIONS = {")
    pattern = re.compile(r"CREATE INDEX IF NOT EXISTS (\w+)", re.I)
    in_migrations = {m.group(1) for m in pattern.finditer(source[split_at:])}

    # A fresh install only ever runs Database.initialize(); it never replays
    # MIGRATIONS, so this is the state a brand new deployment actually gets.
    fresh_path = tmp_path / "fresh.sqlite"
    database_module.Database(fresh_path).initialize()
    connection = sqlite3.connect(fresh_path)
    try:
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
            )
        }
    finally:
        connection.close()

    missing = sorted(in_migrations - present)
    assert not missing, f"a fresh install is missing migrated indexes: {missing}"
