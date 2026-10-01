"""Task #309: connection setup must not leak handles, and the timeout must be configured.

``Database.connect`` opened the connection and ran its PRAGMAs *before* the guard
that closes it, so a failing ``PRAGMA journal_mode = WAL`` - which is exactly what
happens while the recovery path holds ``locking_mode=EXCLUSIVE`` on another
connection - leaked the open handle and let a raw ``sqlite3.OperationalError``
escape the adapter layer. The busy wait was also two hardcoded tens while
``database_connect_timeout_seconds`` only reached the PostgreSQL backend.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentchatroom import database as database_module
from agentchatroom.config import Settings
from agentchatroom.database import Database, create_database
from agentchatroom.errors import DomainError


def _hold_exclusive(path):
    blocker = sqlite3.connect(path, timeout=5)
    blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
    blocker.execute("BEGIN EXCLUSIVE")
    return blocker


def test_a_failing_pragma_closes_the_connection_it_opened(tmp_path, monkeypatch):
    path = tmp_path / "room.db"
    store = Database(path)
    store.initialize()
    blocker = _hold_exclusive(path)
    try:
        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        def tracking_connect(*arguments, **keyword):
            connection = real_connect(*arguments, **keyword)
            opened.append(connection)
            return connection

        monkeypatch.setattr(database_module.sqlite3, "connect", tracking_connect)

        with pytest.raises(DomainError) as error:
            with store.connect(write=True):
                pass

        assert error.value.code == "database_write_contended", error.value.as_dict()
        assert opened, "expected the adapter to have attempted a connection"
        for connection in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.cursor()
    finally:
        blocker.rollback()
        blocker.close()


def test_the_sqlite_wait_comes_from_configuration(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", database_connect_timeout_seconds=2.5)
    database = create_database(settings)
    database.initialize()

    assert database.busy_timeout_seconds == 2.5
    with database.connect() as connection:
        assert int(
            connection.execute("PRAGMA busy_timeout").fetchone()[0]
        ) == 2500


def test_the_default_sqlite_wait_is_still_documented(tmp_path):
    settings = Settings(data_dir=tmp_path / "default")
    database = create_database(settings)

    assert database.busy_timeout_seconds == settings.database_connect_timeout_seconds
