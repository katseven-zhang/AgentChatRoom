from __future__ import annotations

from agentchatroom.database import ensure_project_member_columns
from agentchatroom.postgres_database import postgres_script, translate_qmark


def test_translate_qmark_only_rewrites_sql_parameters():
    sql = "SELECT '?' AS literal, \"?\" AS quoted, value FROM items WHERE id = ?"

    assert translate_qmark(sql) == (
        "SELECT '?' AS literal, \"?\" AS quoted, value FROM items WHERE id = %s"
    )


def test_postgres_script_adapts_sqlite_schema_types():
    script = """
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id),
        last_read_cursor INTEGER NOT NULL
    );
    """

    converted = postgres_script(script)

    assert "BIGSERIAL PRIMARY KEY" in converted
    assert "event_id BIGINT NOT NULL" in converted
    assert "last_read_cursor BIGINT NOT NULL" in converted


def test_postgres_module_can_be_imported_without_optional_driver():
    # Importing the module must remain safe for the default SQLite installation;
    # the optional driver is checked only when PostgreSQL is actually selected.
    assert callable(translate_qmark)


def test_postgres_project_member_column_migration_is_idempotent():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __init__(self, rows):
            self.rows = rows
            self.statements = []

        def execute(self, sql, parameters=()):
            self.statements.append((sql, parameters))
            if "information_schema.columns" in sql:
                return Result(self.rows)
            return Result([])

    first = FakeConnection([])
    ensure_project_member_columns(first, postgres=True)
    assert sum("ADD COLUMN member_id" in sql for sql, _ in first.statements) == 2

    existing = FakeConnection([
        {"table_name": "agent_credentials", "column_name": "member_id"},
        {"table_name": "agent_sessions", "column_name": "member_id"},
    ])
    ensure_project_member_columns(existing, postgres=True)
    assert not any("ADD COLUMN member_id" in sql for sql, _ in existing.statements)
