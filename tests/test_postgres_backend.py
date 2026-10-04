"""PostgreSQL-only behaviour that SQLite cannot exercise.

The row-locking branches in services.py are gated on
``getattr(self.database, "backend", "sqlite") == "postgresql"``, so on SQLite
they never run and no amount of SQLite testing reaches them. Until now nothing
in the suite instantiated a PostgreSQL backend at all: the only PG assertions
compared SQL substrings against a fake connection, which cannot tell a working
``SELECT ... FOR UPDATE`` from a plausible-looking string.

These tests run against a real server when one is configured and skip with a
pointer when it is not - they are never silently satisfied. Configure with the
same variables the application reads:

    AGENTCHATROOM_DATABASE_URL=<your PostgreSQL connection URL>

Set ``AGENTCHATROOM_PG_TESTS=required`` to turn the skip into a failure, so the
coverage cannot quietly rot again.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

from agentchatroom.config import Settings
from agentchatroom.errors import DomainError
from agentchatroom.postgres_database import PostgresDatabase
from agentchatroom.services import AgentChatRoomService


def _dsn() -> str:
    return (
        os.getenv("AGENTCHATROOM_DATABASE_URL")
        or os.getenv("AGENTCHATROOM_TEST_DATABASE_URL")
        or ""
    ).strip()


_REQUIRED = os.getenv("AGENTCHATROOM_PG_TESTS", "").strip().lower() in {
    "1",
    "required",
    "true",
}


@pytest.fixture
def pg_service(tmp_path):
    """A real PostgreSQL-backed service on its own schema, dropped afterwards.

    A private schema per test lets the suite run against a shared server without
    one test's rows deciding another's outcome.
    """
    if not _dsn():
        if _REQUIRED:
            pytest.fail(
                "AGENTCHATROOM_PG_TESTS=required but no AGENTCHATROOM_DATABASE_URL "
                "is configured"
            )
        pytest.skip(
            "no PostgreSQL configured; set AGENTCHATROOM_DATABASE_URL to run the "
            "row-locking paths SQLite cannot reach"
        )

    schema = f"test_{uuid.uuid4().hex[:12]}"
    dsn = _dsn()
    separator = "&" if "?" in dsn else "?"
    scoped = f"{dsn}{separator}options=-csearch_path%3D{schema}"

    database = PostgresDatabase(
        scoped,
        pool_min_size=1,
        pool_max_size=4,
        connect_timeout_seconds=10.0,
    )
    database.initialize()
    service = AgentChatRoomService(
        Settings(database_backend="postgresql", data_dir=tmp_path), database=database
    )
    try:
        yield service
    finally:
        with database.connect(write=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        database.close()


def test_the_postgres_backend_is_really_in_use(pg_service, tmp_path):
    """Guard the guard: prove this fixture is not silently SQLite.

    Without this, a misconfigured backend would let every test below pass for
    the wrong reason - the same way the fake-connection substring checks could.
    """
    assert pg_service.database.backend == "postgresql"
    assert isinstance(pg_service.database, PostgresDatabase)
    project = pg_service.create_project(root_path=str(tmp_path / "repo"))
    assert pg_service.get_project(project["id"])["id"] == project["id"]


def test_number_sequence_reads_take_a_row_lock_on_postgres(pg_service, tmp_path):
    """The numbering counters must be locked, which only the PG branch does.

    ``_lock_task_number_sequence`` / ``_lock_event_number_sequence`` add
    ``FOR UPDATE`` on PostgreSQL only. Two writers reading the counter without
    that lock compute the same number, so this asserts the lock is part of the
    statement the service really issues on a real backend - the thing a
    substring check against a fake connection could not establish.
    """
    recorded: list[str] = []

    class _Proxy:
        """Substitute for the driver connection; psycopg allows attribute
        assignment on its Connection, but a proxy works for both backends and
        keeps the spy out of the driver's own API surface."""

        def __init__(self, inner) -> None:
            self._inner = inner

        def execute(self, sql, parameters=()):  # type: ignore[no-untyped-def]
            recorded.append(" ".join(str(sql).split()).upper())
            return self._inner.execute(sql, parameters)

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._inner, name)

    database = pg_service.database
    real_connect = database.connect

    def spy_connect(*, write: bool = False):  # type: ignore[no-untyped-def]
        inner = real_connect(write=write)

        class _Wrapper:
            def __enter__(self):
                return _Proxy(inner.__enter__())

            def __exit__(self, *exc_info):
                return inner.__exit__(*exc_info)

        return _Wrapper()

    database.connect = spy_connect  # type: ignore[method-assign]
    try:
        created = pg_service.create_task(
            pg_service.create_project(root_path=str(tmp_path / "repo"))["id"],
            title="PG numbering",
            acceptance_criteria=["c"],
        )
    finally:
        database.connect = real_connect  # type: ignore[method-assign]

    assert created["task"]["task_number"] >= 1
    task_locks = [
        sql
        for sql in recorded
        if "TASK_NUMBER_SEQUENCES" in sql and "FOR UPDATE" in sql
    ]
    event_locks = [
        sql
        for sql in recorded
        if "EVENT_NUMBER_SEQUENCES" in sql and "FOR UPDATE" in sql
    ]
    assert task_locks, (
        "the task number counter was read without FOR UPDATE on PostgreSQL; "
        f"statements seen: {[s for s in recorded if 'SEQUENCE' in s]}"
    )
    assert event_locks, (
        "the event number counter was read without FOR UPDATE on PostgreSQL; "
        f"statements seen: {[s for s in recorded if 'SEQUENCE' in s]}"
    )


def test_task_numbers_stay_unique_across_concurrent_writers_on_postgres(
    pg_service, tmp_path
):
    """Concurrent creates must not collide on a real PostgreSQL backend.

    This is the behaviour the lock exists for. SQLite already has equivalent
    coverage; the point here is that the PG branch produces the same guarantee
    rather than being assumed to.
    """
    project = pg_service.create_project(root_path=str(tmp_path / "repo"))
    errors: list[BaseException] = []
    numbers: list[int] = []
    barrier = threading.Barrier(4)

    def create() -> None:
        try:
            barrier.wait(timeout=30)
            result = pg_service.create_task(
                project["id"], title="Concurrent PG task", acceptance_criteria=["c"]
            )
            numbers.append(int(result["task"]["task_number"]))
        except BaseException as error:  # noqa: BLE001 - surfaced in the assert
            errors.append(error)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, f"concurrent create failed on PostgreSQL: {errors}"
    assert len(numbers) == 4
    assert len(set(numbers)) == 4, f"task numbers collided: {sorted(numbers)}"


def test_review_independence_is_enforced_on_postgres_too(pg_service, tmp_path):
    """The authorship rule must hold through the psycopg adapter as well.

    The independence check reads work_reports through a driver that translates
    placeholders and returns dict rows - a different code path from sqlite3.
    Asserting it here keeps the guarantee from being a property of the SQLite
    adapter alone.
    """
    project = pg_service.create_project(root_path=str(tmp_path / "repo"))
    author = pg_service.join_room(
        project["id"], name="Author", client="codex", model="test-model",
        role="executor",
    )
    reviewer = pg_service.join_room(
        project["id"], name="Reviewer", client="qoder", model="test-model",
        role="reviewer",
    )
    task = pg_service.create_task(
        project["id"], title="PG review", acceptance_criteria=["c"],
        actor_session_id=author["agent"]["id"], token=author["token"],
    )["task"]
    pg_service.claim_task(
        project["id"], task["id"], author["agent"]["id"], author["token"]
    )
    pg_service.update_task(
        project["id"], task["id"], status="in_progress",
        session_id=author["agent"]["id"], token=author["token"],
    )
    pg_service.submit_work_report(
        project["id"], task["id"],
        session_id=author["agent"]["id"], token=author["token"],
        summary="done", files=["a.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    with pytest.raises(DomainError) as refused:
        pg_service.submit_review(
            project["id"], task["id"],
            reviewer_session_id=author["agent"]["id"], token=author["token"],
            verdict="approved", criteria=[{"criterion": "c", "status": "passed"}],
        )
    assert refused.value.code == "reviewer_not_independent"

    approved = pg_service.submit_review(
        project["id"], task["id"],
        reviewer_session_id=reviewer["agent"]["id"], token=reviewer["token"],
        verdict="approved", criteria=[{"criterion": "c", "status": "passed"}],
    )
    assert approved["verification_status"] == "approved"
