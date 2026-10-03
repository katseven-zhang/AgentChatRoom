"""Task #296: a fresh database and a migrated one must have the same indexes.

``initialize`` has two routes. A new file runs ``SCHEMA`` plus the ``ensure_*``
helpers and then jumps straight to the current version; an existing older file
replays ``MIGRATIONS``. Three member-dimension indexes existed only in the
migration text, so a database created from scratch never had them and the two
routes behaved differently - silently, with the same schema version.

The indexes are now on both sides. This test builds one database each way and
compares the resulting index sets, so a future index added to only one route
fails here instead of in production. It also asserts the member lookups use an
index rather than a scan, which is the reason the indexes exist.
"""

from __future__ import annotations

import sqlite3

from agentchatroom.config import Settings
from agentchatroom.database import SCHEMA_VERSION, Database

MEMBER_INDEXES = (
    "idx_agent_credentials_member",
    "idx_agent_sessions_member",
    "idx_task_assignments_member",
)


def _index_names(path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _fresh_database(tmp_path, name: str):
    settings = Settings(data_dir=tmp_path / name)
    database = Database(settings.database_path)
    database.initialize()
    return settings.database_path


def _migrated_database(tmp_path, name: str, from_version: int):
    """Build an older-version database, then let initialize migrate it."""
    settings = Settings(data_dir=tmp_path / name)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(settings.database_path))
    try:
        # An empty file stamped at the older version is enough: initialize
        # reads the version and replays MIGRATIONS from there.
        connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (from_version,))
        connection.commit()
    finally:
        connection.close()

    database = Database(settings.database_path)
    database.initialize()
    return settings.database_path


def test_a_fresh_database_has_the_member_indexes(tmp_path):
    """The regression itself: a from-scratch file used to have none of them."""
    names = _index_names(_fresh_database(tmp_path, "fresh"))
    for index in MEMBER_INDEXES:
        assert index in names, f"{index} missing from a fresh database"


def test_fresh_and_migrated_agree_on_every_index(tmp_path):
    """The guard: the two routes must not drift apart again."""
    fresh = _index_names(_fresh_database(tmp_path, "route-fresh"))
    migrated = _index_names(_migrated_database(tmp_path, "route-migrated", 23))

    only_fresh = sorted(fresh - migrated)
    only_migrated = sorted(migrated - fresh)
    assert not only_fresh, f"indexes a fresh database has but a migrated one lacks: {only_fresh}"
    assert not only_migrated, (
        f"indexes a migrated database has but a fresh one lacks: {only_migrated}"
    )


def test_a_migrated_database_reaches_the_current_version(tmp_path):
    """The comparison above is only meaningful if the route really migrated."""
    from agentchatroom.database import read_schema_version

    path = _migrated_database(tmp_path, "route-version", 23)
    connection = sqlite3.connect(str(path))
    try:
        assert read_schema_version(connection) == SCHEMA_VERSION
    finally:
        connection.close()


def test_member_lookups_use_an_index_rather_than_scanning(tmp_path):
    """Why the indexes exist: these predicates run on every join and claim."""
    path = _fresh_database(tmp_path, "query-plan")
    connection = sqlite3.connect(str(path))
    try:
        stamp = "2026-01-01T00:00:00Z"
        connection.execute(
            "INSERT INTO project_members("
            "id, project_id, member_key, name, created_at, updated_at) "
            "VALUES ('member_1', 'project_1', 'software:x', 'X', ?, ?)",
            (stamp, stamp),
        )
        connection.execute(
            "INSERT INTO agent_sessions("
            "id, project_id, member_id, name, client, token_hash, "
            "token_expires_at, created_at, last_heartbeat) "
            "VALUES ('agent_1', 'project_1', 'member_1', 'X', 'pytest', 'hash', ?, ?, ?)",
            (stamp, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO agent_credentials("
            "id, project_id, member_id, name, token_hash, expires_at, "
            "created_at, updated_at) "
            "VALUES ('cred_1', 'project_1', 'member_1', 'X', 'hash', ?, ?, ?)",
            (stamp, stamp, stamp),
        )
        connection.commit()

        for table, predicate in (
            ("agent_sessions", "project_id = 'project_1' AND member_id = 'member_1'"),
            ("agent_credentials", "project_id = 'project_1' AND member_id = 'member_1'"),
        ):
            plan = " ".join(
                row[3]
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE {predicate}"
                ).fetchall()
            )
            assert "USING INDEX" in plan, (
                f"{table} member lookup does not use an index: {plan}"
            )
    finally:
        connection.close()
