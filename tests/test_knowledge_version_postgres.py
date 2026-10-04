"""Task #221 on a real PostgreSQL backend: concurrent revision allocation.

The version number for a knowledge revision is allocated by reading
``MAX(version)`` and writing ``+1``; the INSERT is the authority through
``UNIQUE(asset_id, version)``.

PostgreSQL is where the card's concern lives, so this exercises the real
service against a live server rather than inferring the answer from SQLite.
It is opt-in because a PostgreSQL server is not always available:

* set ``AGENTCHATROOM_TEST_DATABASE_URL`` to a connection string to run it;
* set ``AGENTCHATROOM_REQUIRE_POSTGRES_TESTS=1`` to turn "no server" into a
  failure, which is what CI should do so the coverage cannot silently lapse.

``scripts/verify_postgresql.py`` provisions a throwaway server the same way.
"""

from __future__ import annotations

import os
import threading

import pytest

from agentchatroom.config import Settings
from agentchatroom.postgres_database import PostgresDatabase
from agentchatroom.services import AgentChatRoomService


DATABASE_URL_ENV = "AGENTCHATROOM_TEST_DATABASE_URL"
REQUIRE_ENV = "AGENTCHATROOM_REQUIRE_POSTGRES_TESTS"
WORKERS = 6


def _postgres_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            f"{DATABASE_URL_ENV} is not set, so the PostgreSQL backend cannot be "
            f"exercised. Start a server (scripts/verify_postgresql.py provisions a "
            f"throwaway one) and set {DATABASE_URL_ENV}=postgresql://... to run "
            f"this test; set {REQUIRE_ENV}=1 to make this a failure instead."
        )
    return url


@pytest.fixture()
def postgres_service(tmp_path):
    url = _postgres_url()
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        if os.environ.get(REQUIRE_ENV, "").strip() == "1":
            raise
        pytest.skip(
            "psycopg is not installed; install the postgresql extra "
            '(pip install -e ".[postgresql]") to run this test'
        )

    settings = Settings(
        data_dir=tmp_path / "pgdata",
        database_backend="postgresql",
        database_url_env=DATABASE_URL_ENV,
        database_pool_min_size=1,
        database_pool_max_size=WORKERS + 2,
    )
    service = AgentChatRoomService(
        PostgresDatabase(
            url,
            pool_min_size=1,
            pool_max_size=WORKERS + 2,
            connect_timeout_seconds=10,
        ),
        settings,
    )
    service.initialize()
    # A shared server keeps state between runs, so this test owns a fresh
    # Project rather than assuming an empty database.
    root = tmp_path / "project"
    root.mkdir()
    project = service.create_project(root_path=str(root), name="PG #221")
    return service, project["id"]


def _join(service, project_id: str, key: str):
    return service.join_room(
        project_id, software_key=key, name=key, client="pytest", model="unknown"
    )


def test_concurrent_revisions_on_postgres_allocate_distinct_gap_free_versions(
    postgres_service,
):
    """Every concurrent revision lands on its own number, and none 500s.

    PostgreSQL serializes writers through the ``pg_advisory_xact_lock`` that
    ``connect(write=True)`` takes, so the outcome today is success for all of
    them. The assertion that matters is the one that would break if that lock
    ever stopped being taken: no writer may come back as an unmapped driver
    error, and no two writers may claim the same version.
    """
    service, project_id = postgres_service
    author = _join(service, project_id, "pg-author")
    asset = service.submit_knowledge_candidate(
        project_id,
        session_id=author["agent"]["id"],
        token=author["token"],
        kind="decision",
        title="v1",
        body="body one",
    )["asset"]

    sessions = [_join(service, project_id, f"pg-worker-{i}") for i in range(WORKERS)]
    barrier = threading.Barrier(WORKERS)
    outcomes: list[object] = []
    guard = threading.Lock()

    def revise(index: int, session) -> None:
        barrier.wait(timeout=60)
        try:
            result = service.submit_knowledge_candidate(
                project_id,
                session_id=session["agent"]["id"],
                token=session["token"],
                kind="decision",
                title=f"rev{index}",
                body=f"body {index}",
                asset_id=asset["id"],
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

    # A lost race is allowed to be reported as knowledge_version_conflict; what
    # must never happen is an untranslated driver error, which the REST layer
    # would surface as an HTTP 500.
    unexpected = []
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            code = getattr(outcome, "code", "")
            if code != "knowledge_version_conflict":
                unexpected.append(outcome)
    assert not unexpected, [f"{type(e).__name__}: {e}" for e in unexpected]

    with service.database.connect() as connection:
        rows = connection.execute(
            "SELECT version FROM knowledge_asset_versions WHERE asset_id = ?"
            " ORDER BY version",
            (asset["id"],),
        ).fetchall()
    versions = [int(row["version"]) for row in rows]

    # Numbers are unique and gap-free from 1, and the count matches the writers
    # that were not asked to retry.
    assert len(versions) == len(set(versions))
    assert versions == sorted(versions)
    assert versions[0] == 1
    assert versions == list(range(1, len(versions) + 1))
    assert len(versions) == 1 + WORKERS - len(
        [o for o in outcomes if getattr(o, "code", "") == "knowledge_version_conflict"]
    )

    detail = service.get_knowledge_asset(project_id, asset["id"])
    assert [item["version"] for item in detail["versions"]] == versions
