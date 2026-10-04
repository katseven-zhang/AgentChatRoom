"""Task #221: a lost MAX(version)+1 race must be a 409, never an HTTP 500.

The version number for a knowledge revision is allocated by reading
``MAX(version)`` and writing ``+1``, and the INSERT is what actually decides
which writer won, via ``UNIQUE(asset_id, version)``.

Both shipped backends serialize writers today - SQLite by ``BEGIN IMMEDIATE``,
PostgreSQL by the ``pg_advisory_xact_lock`` that every ``connect(write=True)``
takes - so the race cannot be provoked through the public API on either one.
The interesting case is therefore a backend that does *not* hold that lock:
there the UNIQUE violation is the only thing between two concurrent revisions
and an unhandled driver error, which the REST layer reports as a 500.

These tests pin the translation itself, so the guarantee does not depend on a
lock one layer down staying in place.
"""

from __future__ import annotations

import threading

import pytest

from agentchatroom.errors import DomainError


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key.title(), client="pytest", model="unknown"
    )


def _asset(service, project_id: str, author) -> dict:
    return service.submit_knowledge_candidate(
        project_id,
        session_id=author["agent"]["id"],
        token=author["token"],
        kind="decision",
        title="v1",
        body="body one",
    )["asset"]


def _revise(service, project_id: str, author, asset_id: str, *, title: str = "rev"):
    return service.submit_knowledge_candidate(
        project_id,
        session_id=author["agent"]["id"],
        token=author["token"],
        kind="decision",
        title=title,
        body=f"body {title}",
        asset_id=asset_id,
    )


class _StaleMaxConnection:
    """Connection proxy that under-reports MAX(version) by one.

    This is the exact state a writer is in when it loses the allocation race:
    it read a MAX that another writer has since superseded, so the number it is
    about to INSERT is already taken. Simulating the stale read is what makes
    the collision reachable on a backend whose writers are otherwise
    serialized - there is no way to interleave two real writers on SQLite.

    ``insert_error`` injects a non-unique driver failure on the version INSERT
    instead, which is how the translation is shown to be narrow.
    """

    def __init__(self, inner, asset_id: str, insert_error: Exception | None = None) -> None:
        self._inner = inner
        self._asset_id = asset_id
        self._insert_error = insert_error

    def execute(self, sql, parameters=()):
        if "COALESCE(MAX(version), 0)" in sql and parameters == (self._asset_id,):
            row = self._inner.execute(
                "SELECT COALESCE(MAX(version), 0) - 1 AS latest"
                " FROM knowledge_asset_versions WHERE asset_id = ?",
                parameters,
            ).fetchone()
            return _OneRowCursor(int(row["latest"]))
        if self._insert_error is not None and "INSERT INTO knowledge_asset_versions" in sql:
            raise self._insert_error
        return self._inner.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _OneRowCursor:
    """The single-row cursor shape the service reads a scalar MAX through."""

    def __init__(self, latest: int) -> None:
        self._row = {"latest": latest}

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row]


class _StaleMaxDatabase:
    """Database proxy that hands out one connection with a stale MAX read."""

    def __init__(
        self, inner, asset_id: str, insert_error: Exception | None = None
    ) -> None:
        self._inner = inner
        self._asset_id = asset_id
        self._insert_error = insert_error
        self.backend = inner.backend

    def connect(self, *, write: bool = False):
        return _ProxiedConnect(self._inner, self._asset_id, self._insert_error, write)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ProxiedConnect:
    """``connect()`` result that swaps in the proxy for write transactions."""

    def __init__(self, inner, asset_id, insert_error, write) -> None:
        self._cm = inner.connect(write=write)
        self._asset_id = asset_id
        self._insert_error = insert_error
        self._write = write

    def __enter__(self):
        connection = self._cm.__enter__()
        if self._write:
            return _StaleMaxConnection(
                connection, self._asset_id, self._insert_error
            )
        return connection

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


# --- the guarantee being pinned ---------------------------------------------


def test_losing_the_version_race_is_a_conflict_not_a_crash(service, project_dir, monkeypatch):
    """A writer that lost the race gets 409, never a raw driver error.

    Without the translation the caller sees the driver's own
    ``IntegrityError``/``UniqueViolation``, which no error handler maps and the
    REST layer therefore reports as an HTTP 500.
    """
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "racer")
    asset = _asset(service, project_id, author)
    asset_id = asset["id"]

    # A rival revision wins the allocation: version 2 is now taken.
    winner = _revise(service, project_id, author, asset_id, title="winner")
    assert winner["version"] == 2

    real_database = service.database
    monkeypatch.setattr(
        service, "database", _StaleMaxDatabase(real_database, asset_id)
    )

    with pytest.raises(DomainError) as conflict:
        _revise(service, project_id, author, asset_id, title="loser")

    assert conflict.value.code == "knowledge_version_conflict"
    assert conflict.value.status_code == 409
    assert conflict.value.details["asset_id"] == asset_id
    assert conflict.value.details["retry_after_seconds"] >= 1

    # Nothing half-applied: the winner's history is intact and readable, and
    # the loser's title never became a version.
    monkeypatch.setattr(service, "database", real_database)
    detail = service.get_knowledge_asset(project_id, asset_id)
    assert [item["version"] for item in detail["versions"]] == [1, 2]
    assert detail["version"]["title"] == "winner"
    assert detail["status"] == "candidate"


def test_an_unrelated_driver_error_is_not_disguised_as_a_conflict(
    service, project_dir, monkeypatch
):
    """The translation is narrow: only unique violations become a 409.

    Catching broadly would report a genuine defect as a retryable conflict and
    hide it from every operator.
    """
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "not-unique")
    asset = _asset(service, project_id, author)

    real_database = service.database
    monkeypatch.setattr(
        service,
        "database",
        _StaleMaxDatabase(
            real_database, asset["id"], insert_error=RuntimeError("unrelated driver failure")
        ),
    )

    with pytest.raises(RuntimeError, match="unrelated driver failure"):
        _revise(service, project_id, author, asset["id"], title="boom")


# --- the SQLite behaviour that must not regress -----------------------------


def _concurrent_sqlite_writes_available() -> tuple[bool, str]:
    """Whether this machine can hold two SQLite write transactions at once.

    Sandboxed and network-share environments refuse the second concurrent
    connection with "attempt to write a readonly database" regardless of the
    code under test, which is why several pre-existing concurrency tests in
    test_services.py fail here too. The probe has to be genuinely concurrent -
    a sequential second writer succeeds even on those machines - and it uses
    plain sqlite3 so an environmental refusal is never mistaken for a defect in
    the change.
    """
    import sqlite3
    import tempfile

    directory = tempfile.mkdtemp()
    path = f"{directory}/probe.db"
    setup = sqlite3.connect(path, isolation_level=None)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE probe(value INTEGER)")
    setup.close()

    failures: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def write(value: int) -> None:
        try:
            barrier.wait(timeout=10)
            writer = sqlite3.connect(path, timeout=5, isolation_level=None)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO probe VALUES (?)", (value,))
            writer.commit()
            writer.close()
        except Exception as error:  # noqa: BLE001
            with guard:
                failures.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return (not failures), "; ".join(failures)


def test_concurrent_revisions_on_sqlite_all_succeed_with_distinct_versions(
    service, project_dir
):
    """Real threads on the shipped backend: every revision lands, no 500.

    SQLite serializes the writers, so this asserts the fix changed nothing for
    the common path - each writer gets its own number and the history stays
    gap-free. The concurrency itself is exercised for real on PostgreSQL in
    test_knowledge_version_postgres.py; this is the SQLite counterpart.
    """
    available, reason = _concurrent_sqlite_writes_available()
    if not available:
        pytest.skip(
            f"this machine refuses a second SQLite writer ({reason}); the same "
            "limitation fails the pre-existing concurrency tests in "
            "test_services.py, so the allocation guarantee is covered on "
            "PostgreSQL instead"
        )

    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "burst-author")
    asset = _asset(service, project_id, author)
    asset_id = asset["id"]

    sessions = [_join(service, project_id, f"burst-{index}") for index in range(6)]
    barrier = threading.Barrier(len(sessions))
    outcomes: list[object] = []
    guard = threading.Lock()

    def revise(index: int, session) -> None:
        barrier.wait(timeout=30)
        try:
            result = _revise(
                service, project_id, session, asset_id, title=f"rev{index}"
            )
            with guard:
                outcomes.append(result["version"])
        except Exception as error:  # noqa: BLE001
            with guard:
                outcomes.append(error)

    threads = [
        threading.Thread(target=revise, args=(index, session))
        for index, session in enumerate(sessions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not [item for item in outcomes if isinstance(item, Exception)], outcomes
    assert sorted(outcomes) == [2, 3, 4, 5, 6, 7]

    detail = service.get_knowledge_asset(project_id, asset_id)
    assert [item["version"] for item in detail["versions"]] == [1, 2, 3, 4, 5, 6, 7]


def test_versions_stay_gap_free_and_link_by_id_not_by_arithmetic(
    service, project_dir
):
    """Readers follow the version chain by id, so numbering is not load-bearing.

    ``get``/``read`` resolve the current and historical versions through
    ``current_version_id`` and ``supersedes_version_id`` pointers. Nothing walks
    ``version +/- 1``, so a number that a lost race skipped or reused would not
    desynchronize a reader - which is why the conflict is reported and retried
    rather than backfilled.
    """
    project = service.create_project(root_path=str(project_dir))
    project_id = project["id"]
    author = _join(service, project_id, "chain")
    asset = _asset(service, project_id, author)

    second = _revise(service, project_id, author, asset["id"], title="second")
    third = _revise(service, project_id, author, asset["id"], title="third")
    assert (second["version"], third["version"]) == (2, 3)

    detail = service.get_knowledge_asset(project_id, asset["id"])
    versions = detail["versions"]
    assert [item["version"] for item in versions] == [1, 2, 3]
    # The chain is linked by identity, and the third version's predecessor is
    # the second version's row - not "the row numbered one lower".
    assert detail["version"]["id"] == versions[2]["id"]
    assert detail["version"]["supersedes_version_id"] == versions[1]["id"]
    assert versions[0]["supersedes_version_id"] is None

    # Asking for a specific version by id still resolves it.
    pinned = service.get_knowledge_asset(
        project_id, asset["id"], version_id=versions[0]["id"]
    )
    assert pinned["version"]["title"] == "v1"
