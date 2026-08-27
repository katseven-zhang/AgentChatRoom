from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .database import (
    MIGRATIONS,
    SCHEMA,
    SCHEMA_VERSION,
    ensure_agent_identity_columns,
    ensure_project_member_columns,
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
            row = connection.execute(
                "SELECT version FROM schema_meta LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported database schema {row['version']}; expected {SCHEMA_VERSION}"
                )
            else:
                version = int(row["version"])
                while version < SCHEMA_VERSION:
                    target = version + 1
                    connection.executescript(MIGRATIONS[target])
                    connection.execute(
                        "UPDATE schema_meta SET version = ?", (target,)
                    )
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
