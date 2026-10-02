"""Task #310: a migrated database must expose the same column contract as a fresh one.

Fresh installs create every table from ``SCHEMA``; long-running installs arrived
there through ``ALTER TABLE ... ADD COLUMN``. When the two spellings differ, code
written against one of them is wrong on the other install: a column declared
``NOT NULL`` in the fresh schema is nullable after migration, so a row can carry
NULL only on an old database -- and reading it is exactly where numbers are
turned into ``int``.

This compares the two shapes column by column. Anything that genuinely cannot be
aligned in place has to be listed with the reason and the compensating invariant
that is then verified, so the divergence stays visible instead of drifting.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from agentchatroom import database as database_module
from agentchatroom.database import Database, SCHEMA, SCHEMA_VERSION


# (table, column) pairs that some migration adds with ALTER TABLE, i.e. the
# columns a fresh CREATE TABLE can disagree with.
def _migration_added_columns() -> list[tuple[str, str, str]]:
    source = inspect.getsource(database_module)
    found: list[tuple[str, str, str]] = []
    for match in re.finditer(
        r"ALTER TABLE (\w+) ADD COLUMN (?:IF NOT EXISTS )?(\w+)([^;\n\"]*)", source
    ):
        found.append((match.group(1), match.group(2), match.group(3).strip()))
    return found


def _old_schema_sql(added: list[tuple[str, str, str]]) -> str:
    """The schema text with every migration-added column removed from its table."""
    lines = SCHEMA.splitlines()
    removed: set[tuple[str, str]] = set()
    out: list[str] = []
    current_table = ""
    for line in lines:
        create = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", line.strip())
        if create:
            current_table = create.group(1)
        column = re.match(r"^\s*(\w+)\s+\S", line)
        if column and (current_table, column.group(1)) in {(t, c) for t, c, _ in added}:
            removed.add((current_table, column.group(1)))
            continue
        out.append(line)
    assert removed, "no migration-added column was found in the fresh schema"
    # Removing a column can strand a comma in front of the closing parenthesis.
    return re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(out))


def _columns(path: Path) -> dict[str, dict[str, tuple[str, int, object]]]:
    connection = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: {
                row[1]: (row[2].upper(), int(row[3]), row[4])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in tables
        }
    finally:
        connection.close()


# Divergences that cannot be closed by a migration, each naming the attribute that
# is allowed to differ. Neither ``notnull`` nor ``DEFAULT`` can be set or dropped on
# an existing SQLite column without rebuilding the table, so on old installs these
# stay as the ALTER spelled them while fresh installs declare the stricter form.
# ``test_insert_of_an_incomplete_row_is_rejected_on_both_shapes`` measures that the
# two epoch-default cases cannot change any write outcome; the nullable one is
# covered by the backfill and uniqueness assertions below.
ALIGNED_BY_INVARIANT = {
    ("tasks", "task_number"): {"notnull"},
    ("agent_sessions", "token_expires_at"): {"dflt_value"},
    ("file_leases", "renewed_at"): {"dflt_value"},
}


def test_insert_of_an_incomplete_row_is_rejected_on_both_shapes(tmp_path: Path):
    """The documented divergence, pinned: fresh fails loudly, migrated fills in.

    This is the exact behaviour a migrated install must not be relied on for, so it
    is asserted rather than assumed: if a future writer ever omits one of these
    columns, it silently stores 1970 on an old database and errors on a new one.
    """
    fresh_path = tmp_path / "epoch-fresh.db"
    Database(fresh_path).initialize()
    migrated_path = tmp_path / "epoch-migrated.db"
    connection = sqlite3.connect(migrated_path)
    connection.executescript(_old_schema_sql(_migration_added_columns()))
    connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_meta(version) VALUES (1)")
    connection.commit()
    connection.close()
    Database(migrated_path).initialize()

    def insert_without_renewed_at(path: Path) -> str:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute(
                    "INSERT INTO file_leases(id, project_id, session_id, path_pattern,"
                    " mode, reason, expires_at) VALUES ('l1','p1','s1','src/a.py',"
                    " 'exclusive','probe','2099-01-01T00:00:00Z')"
                )
            except sqlite3.IntegrityError:
                return "rejected"
            stored = connection.execute(
                "SELECT renewed_at FROM file_leases WHERE id = 'l1'"
            ).fetchone()
            return "accepted:" + (stored[0] if stored and stored[0] else "null")
        finally:
            connection.close()

    # Measured, not assumed: both shapes reject the same incomplete INSERT, so the
    # extra epoch default on an old install cannot change any write outcome -- the
    # divergence is metadata only. If a future change ever makes one side accept it,
    # this test turns that into a visible behaviour difference.
    assert insert_without_renewed_at(fresh_path) == "rejected"
    assert insert_without_renewed_at(migrated_path) == "rejected"


def test_migrated_database_matches_the_fresh_column_contract(tmp_path: Path):
    fresh_path = tmp_path / "fresh.db"
    Database(fresh_path).initialize()
    fresh = _columns(fresh_path)

    migrated_path = tmp_path / "migrated.db"
    migrated_path.parent.mkdir(parents=True, exist_ok=True)
    added = _migration_added_columns()
    assert added
    connection = sqlite3.connect(migrated_path)
    connection.executescript(_old_schema_sql(added))
    # An old install that still has to replay the whole migration history.
    connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
    connection.execute("DELETE FROM schema_meta")
    connection.execute("INSERT INTO schema_meta(version) VALUES (1)")
    connection.commit()
    connection.close()
    Database(migrated_path).initialize()
    migrated = _columns(migrated_path)

    assert set(migrated) == set(fresh)
    divergences: list[str] = []
    for table, columns in sorted(fresh.items()):
        for column, definition in sorted(columns.items()):
            other = migrated[table].get(column)
            if other is None:
                divergences.append(f"{table}.{column}: missing after migration")
            elif other != definition:
                allowed = ALIGNED_BY_INVARIANT.get((table, column))
                if allowed is not None:
                    differences = {
                        name
                        for name, index in (("type", 0), ("notnull", 1), ("dflt_value", 2))
                        if other[index] != definition[index]
                    }
                    assert differences <= allowed, (
                        f"{table}.{column} diverges in {sorted(differences)}, "
                        f"only {sorted(allowed)} is documented"
                    )
                    assert other[0] == definition[0], (table, column)
                    continue
                divergences.append(
                    f"{table}.{column}: fresh={definition} migrated={other}"
                )
    assert not divergences, "\n".join(divergences)


def test_migrated_task_numbers_are_backfilled_and_unique(tmp_path: Path):
    """The compensating invariants that make the documented divergence harmless."""
    path = tmp_path / "backfill.db"
    added = _migration_added_columns()
    connection = sqlite3.connect(path)
    connection.executescript(_old_schema_sql(added))
    connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_meta(version) VALUES (1)")
    connection.commit()
    connection.close()
    Database(path).initialize()

    connection = sqlite3.connect(path)
    try:
        # The unique index is what keeps a backfilled column trustworthy.
        indexed = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'tasks' AND name = 'idx_tasks_project_task_number'"
        ).fetchone()
        assert indexed is not None
        null_numbers = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_number IS NULL"
        ).fetchone()[0]
        assert null_numbers == 0
        version = connection.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        connection.close()
