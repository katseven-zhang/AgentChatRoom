from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .database import (
    MIGRATIONS,
    read_schema_version,
    write_schema_version,
    SCHEMA,
    SCHEMA_VERSION,
    ensure_agent_identity_columns,
    ensure_event_number_schema,
    ensure_member_lookup_indexes,
    ensure_project_member_columns,
    ensure_task_assignment_member_column,
    ensure_task_number_schema,
    migrate_knowledge_source_event_ids,
)


POSTGRES_WRITE_LOCK_KEY = 4_142_843_429


def _load_postgres_driver() -> tuple[Any, Any]:
    """Load optional PostgreSQL dependencies only when the backend is selected."""
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as error:  # pragma: no cover - exercised by deployment setup
        raise RuntimeError(
            'PostgreSQL support requires: pip install -e ".[postgresql]"'
        ) from error
    return dict_row, ConnectionPool


def translate_qmark(sql: str) -> str:
    output: list[str] = []
    in_single_quote = False
    in_double_quote = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if character == "'" and not in_double_quote:
            output.append(character)
            if in_single_quote and index + 1 < len(sql) and sql[index + 1] == "'":
                output.append("'")
                index += 2
                continue
            in_single_quote = not in_single_quote
        elif character == '"' and not in_single_quote:
            output.append(character)
            in_double_quote = not in_double_quote
        elif character == "?" and not in_single_quote and not in_double_quote:
            output.append("%s")
        else:
            output.append(character)
        index += 1
    return "".join(output)


def postgres_script(script: str) -> str:
    return (
        script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        .replace(
            "event_id INTEGER NOT NULL REFERENCES events(id)",
            "event_id BIGINT NOT NULL REFERENCES events(id)",
        )
        .replace("last_read_cursor INTEGER NOT NULL", "last_read_cursor BIGINT NOT NULL")
    )


def split_sql_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class PostgresConnection:
    def __init__(self, connection: Any) -> None:
        self.raw = connection

    def execute(self, sql: str, parameters: tuple[Any, ...] | list[Any] = ()):
        return self.raw.execute(translate_qmark(sql), parameters)

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(postgres_script(script)):
            self.raw.execute(statement)


class PostgresDatabase:
    backend = "postgresql"

    def __init__(
        self,
        database_url: str,
        *,
        pool_min_size: int,
        pool_max_size: int,
        connect_timeout_seconds: float,
    ) -> None:
        dict_row, connection_pool = _load_postgres_driver()
        self.connect_timeout_seconds = connect_timeout_seconds
        self._pool = connection_pool(
            conninfo=database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            timeout=connect_timeout_seconds,
            kwargs={
                "autocommit": False,
                "row_factory": dict_row,
                "connect_timeout": max(1, math.ceil(connect_timeout_seconds)),
            },
            open=False,
        )
        self._open_lock = threading.Lock()
        self._opened = False
        self._active_connection: ContextVar[
            tuple[PostgresConnection, bool] | None
        ] = ContextVar(f"agentchatroom_postgres_connection_{id(self)}", default=None)

    @contextmanager
    def hold_restore_lock(self) -> Iterator[None]:
        """Block other writers from the stale check until pg_restore returns.

        Writers already take ``pg_advisory_xact_lock`` on
        ``POSTGRES_WRITE_LOCK_KEY`` for one transaction. A session-level lock
        on that same key is not dropped when pg_restore rebuilds tables, and
        it is not confined to the transaction that only read the event cursor.
        """
        self._ensure_open()
        with self._pool.connection(timeout=self.connect_timeout_seconds) as raw:
            raw.execute(
                "SELECT pg_advisory_lock(%s)",
                (POSTGRES_WRITE_LOCK_KEY,),
            )
            try:
                yield
            finally:
                raw.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (POSTGRES_WRITE_LOCK_KEY,),
                )

    def _ensure_open(self) -> None:
        if self._opened:
            return
        with self._open_lock:
            if self._opened:
                return
            self._pool.open(
                wait=True,
                timeout=self.connect_timeout_seconds,
            )
            self._opened = True

    def initialize(self) -> None:
        self._ensure_open()
        with self.connect(write=True) as connection:
            connection.executescript(SCHEMA)
            ensure_project_member_columns(connection, postgres=True)
            ensure_agent_identity_columns(connection, postgres=True)
            ensure_task_number_schema(connection, postgres=True)
            ensure_task_assignment_member_column(connection, postgres=True)
            ensure_event_number_schema(connection, postgres=True)
            # Mirrors the SQLite backend: a fresh PostgreSQL install never
            # replays MIGRATIONS, so it needs the member lookup indexes too.
            ensure_member_lookup_indexes(connection)
            row_version = read_schema_version(connection)
            if row_version is None:
                write_schema_version(connection, SCHEMA_VERSION)
            elif row_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported database schema {row_version}; expected {SCHEMA_VERSION}"
                )
            else:
                version = int(row_version)
                while version < SCHEMA_VERSION:
                    target = version + 1
                    # The whole unit - statements and the version bump - shares
                    # this transaction, exactly like the SQLite backend, so a
                    # failure halfway leaves the old schema and a restart can
                    # replay the same migration instead of hitting a duplicate
                    # column.
                    connection.executescript(MIGRATIONS[target])
                    if target == 25:
                        migrate_knowledge_source_event_ids(connection)
                    write_schema_version(connection, target)
                    version = target

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[PostgresConnection]:
        active = self._active_connection.get()
        if active is not None:
            connection, active_write = active
            if write and not active_write:
                raise RuntimeError("Cannot open a nested write inside a read transaction")
            yield connection
            return

        self._ensure_open()
        with self._pool.connection(
            timeout=self.connect_timeout_seconds
        ) as raw_connection:
            connection = PostgresConnection(raw_connection)
            with raw_connection.transaction():
                if write:
                    raw_connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (POSTGRES_WRITE_LOCK_KEY,),
                    )
                binding = self._active_connection.set((connection, write))
                try:
                    yield connection
                finally:
                    self._active_connection.reset(binding)

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False
