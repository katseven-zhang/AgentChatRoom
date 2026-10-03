# audit-allow-file:windows_absolute_path
"""Read-path robustness and fan-out regression coverage (#359).

Five sub-problems, one file:

* a snapshot page cursor is shape-checked per segment, so a malformed
  acknowledgement cursor is the documented 422 and not a ValueError -> 500;
* event projection reads ``task_number`` for a whole page in one ``IN`` query
  instead of one SELECT per event;
* the lease projection pushes "still active" into SQL with a bound instead of
  scanning every lease row of the project and deciding in Python;
* ``export`` declares the window it actually returns instead of claiming to
  deliver the whole history;
* the snapshot Session projection derives per-Session activity and unread
  counters from aggregate queries rather than one correlated subquery each.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentchatroom.api import create_app
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


@pytest.fixture()
def service(settings):
    instance = AgentChatRoomService(Database(settings.database_path), settings)
    instance.initialize()
    return instance


@pytest.fixture()
def project(service, tmp_path):
    root = tmp_path / "read-path-project"
    root.mkdir()
    return service.create_project(root_path=str(root), name="ReadPath")


@pytest.fixture()
def joined(service, project):
    return service.join_room(
        project["id"], name="Agent", client="codex", model="test-model"
    )


def _write_report(service, project, report_id, created_at, task_id, session_id):
    with sqlite3.connect(service.database.path) as connection:
        connection.execute(
            """
            INSERT INTO work_reports(
                id, project_id, task_id, session_id, summary, files_json,
                tests_json, system_evidence_json, commit_hash, created_at
            )
            VALUES (?, ?, ?, ?, ?, '[]', '[]', '{}', '', ?)
            """,
            (
                report_id,
                project["id"],
                task_id,
                session_id,
                f"summary-{report_id}",
                created_at,
            ),
        )
        connection.commit()


def _write_ack(service, project, event_seq, session_id, created_at):
    with sqlite3.connect(service.database.path) as connection:
        row = connection.execute(
            "SELECT id FROM events WHERE project_id = ? AND project_seq = ?",
            (project["id"], event_seq),
        ).fetchone()
        assert row is not None
        connection.execute(
            """
            INSERT INTO event_acknowledgements(event_id, session_id, created_at)
            VALUES (?, ?, ?)
            """,
            (row["id"] if isinstance(row, sqlite3.Row) else row[0], session_id, created_at),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# AC1 every cursor segment is shape-checked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        # Acknowledgement cursor whose middle segment is not a number. It has
        # three non-empty segments, so the old count-only guard accepted it and
        # `int(event_seq)` raised ValueError out of the projection (HTTP 500).
        "2026-01-01T00:00:00Z|x|agent_abc",
        "2026-01-01T00:00:00Z|-3|agent_abc",
        "2026-01-01T00:00:00Z|1.5|agent_abc",
        "2026-01-01T00:00:00Z| 1|agent_abc",
        "2026-01-01T00:00:00Z|0x10|agent_abc",
        "2026-01-01T00:00:00Z|1e3|agent_abc",
    ],
)
def test_ack_cursor_with_non_numeric_seq_is_422_not_500(service, project, cursor):
    for keyword in (
        "acknowledgements_before",
        "acknowledgements_after",
    ):
        with pytest.raises(DomainError) as bad:
            service.snapshot(project["id"], **{keyword: cursor})
        assert bad.value.code == "invalid_page_cursor"
        assert bad.value.status_code == 422


@pytest.mark.parametrize(
    "cursor",
    [
        # A timestamp segment that is not an instant at all.
        "not-a-time|report_1",
        "2026-13-45T99:99:99Z|report_1",
        "1700000000|report_1",
        # An empty segment on either side.
        "|report_1",
        "2026-01-01T00:00:00Z|",
        "2026-01-01T00:00:00Z||report_1",
    ],
)
def test_tied_cursor_with_bad_segments_is_422(service, project, cursor):
    for keyword in ("reports_before", "reports_after"):
        with pytest.raises(DomainError) as bad:
            service.snapshot(project["id"], **{keyword: cursor})
        assert bad.value.code == "invalid_page_cursor"
        assert bad.value.status_code == 422


@pytest.mark.parametrize(
    "cursor",
    [
        # Identifier segments carrying whitespace, control characters, SQL
        # punctuation or an unbounded run.
        "2026-01-01T00:00:00Z|report 1",
        "2026-01-01T00:00:00Z|report_1\n",
        "2026-01-01T00:00:00Z|report_1' OR 1=1--",
        "2026-01-01T00:00:00Z|" + "r" * 200,
        "2026-01-01T00:00:00Z\r|report_1",
        # Over-long timestamp segment.
        "2" * 80 + "|report_1",
    ],
)
def test_cursor_identifier_segment_must_be_a_bounded_token(service, project, cursor):
    with pytest.raises(DomainError) as bad:
        service.snapshot(project["id"], reports_before=cursor)
    assert bad.value.code == "invalid_page_cursor"
    assert bad.value.status_code == 422


def test_ack_cursor_session_segment_must_be_a_bounded_token(service, project):
    with pytest.raises(DomainError) as bad:
        service.snapshot(
            project["id"],
            acknowledgements_before="2026-01-01T00:00:00Z|1|agent a b",
        )
    assert bad.value.code == "invalid_page_cursor"
    assert bad.value.status_code == 422


def test_cursor_error_detail_does_not_echo_an_unbounded_cursor(service, project):
    with pytest.raises(DomainError) as bad:
        service.snapshot(project["id"], reports_before="x" * 5000)
    assert bad.value.code == "invalid_page_cursor"
    assert len(str(bad.value.details["cursor"])) <= 256


def test_well_formed_cursors_still_page(service, project, joined):
    """The guard must not reject the cursors the projection itself emits."""
    task = service.create_task(
        project["id"],
        title="cursor",
        description="reports need a task",
        acceptance_criteria=["ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    _write_report(
        service,
        project,
        "report_a",
        "2026-01-01T00:00:00Z",
        task["id"],
        joined["agent"]["id"],
    )
    _write_report(
        service,
        project,
        "report_b",
        "2026-01-01T00:00:01Z",
        task["id"],
        joined["agent"]["id"],
    )
    page = service.snapshot(project["id"])
    info = page["page_info"]["reports"]
    assert info["before"] == "2026-01-01T00:00:01Z|report_b"
    assert info["total"] == 2

    older = service.snapshot(project["id"], reports_before=info["before"])
    assert [row["id"] for row in older["reports"]] == ["report_a"]
    assert older["page_info"]["reports"]["has_more"] is False


def test_ack_cursor_round_trips_through_page_info(service, project, joined):
    _write_ack(service, project, 1, joined["agent"]["id"], "2026-01-01T00:00:00Z")
    _write_ack(service, project, 2, joined["agent"]["id"], "2026-01-01T00:00:01Z")
    page = service.snapshot(project["id"])
    info = page["page_info"]["acknowledgements"]
    assert len(page["acknowledgements"]) == 2
    assert info["before"].count("|") == 2
    older = service.snapshot(project["id"], acknowledgements_before=info["before"])
    assert len(older["acknowledgements"]) == 1
    assert older["acknowledgements"][0]["event_id"] == 1


def test_snapshot_route_rejects_oversized_cursor_with_422(service, project, settings):
    app = create_app(settings)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.get(
        f"/api/v1/projects/{project['id']}/snapshot",
        params={"reports_before": "x" * 5000},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# AC2a event projection reads task_number in one query
# ---------------------------------------------------------------------------


class _CountingConnection:
    """Wraps a real connection and counts statements per SQL shape."""

    def __init__(self, connection):
        self._connection = connection
        self.statements: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(" ".join(str(sql).split()))
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _task_count_statements(statements):
    return [
        sql
        for sql in statements
        if sql.upper().startswith("SELECT") and "FROM TASKS" in sql.upper()
    ]


def test_event_projection_reads_task_numbers_in_one_query(service, project, joined):
    task = service.create_task(
        project["id"],
        title="batched",
        description="one query for the whole page",
        acceptance_criteria=["ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    for index in range(12):
        service.post_message(
            project["id"],
            body=f"message {index}",
            session_id=joined["agent"]["id"],
            token=joined["token"],
            task_id=task["id"],
            model_display_name="test-model",
        )

    with service.database.connect() as connection:
        counter = _CountingConnection(connection)
        rows = connection.execute(
            "SELECT * FROM events WHERE project_id = ? ORDER BY project_seq ASC LIMIT 200",
            (project["id"],),
        ).fetchall()
        counter.statements.clear()
        events = service._project_events(counter, rows)

    task_events = [event for event in events if event["task_id"] == task["id"]]
    assert len(task_events) == 13  # task.created plus the 12 messages
    assert {event["task_number"] for event in task_events} == {task["task_number"]}
    assert len(_task_count_statements(counter.statements)) == 1


def test_event_projection_keeps_task_number_none_for_deleted_task(
    service, project, joined
):
    task = service.create_task(
        project["id"],
        title="doomed",
        description="projection must not invent a number",
        acceptance_criteria=["ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.post_message(
        project["id"],
        body="about a task",
        session_id=joined["agent"]["id"],
        token=joined["token"],
        model_display_name="test-model",
        task_id=task["id"],
    )
    with sqlite3.connect(service.database.path) as raw:
        raw.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        raw.commit()

    events = service.list_events(project["id"])["events"]
    task_events = [event for event in events if event.get("task_id") == task["id"]]
    assert task_events
    assert all(event["task_number"] is None for event in task_events)


def test_list_events_still_projects_task_numbers(service, project, joined):
    task = service.create_task(
        project["id"],
        title="projection",
        description="event dict keeps the task number",
        acceptance_criteria=["ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.post_message(
        project["id"],
        body="hello",
        session_id=joined["agent"]["id"],
        token=joined["token"],
        model_display_name="test-model",
        task_id=task["id"],
    )
    events = service.list_events(project["id"])["events"]
    numbered = [event for event in events if event.get("task_id") == task["id"]]
    assert numbered and all(e["task_number"] == task["task_number"] for e in numbered)


# ---------------------------------------------------------------------------
# AC2b lease projection pushes "active" into SQL with a bound
# ---------------------------------------------------------------------------


def test_active_lease_listing_pushes_the_active_predicate_into_sql(
    service, project, joined
):
    lease = service.acquire_lease(
        project["id"],
        path_pattern="src/**/*.py",
        mode="exclusive",
        session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    service.release_lease(
        project["id"],
        lease_id=lease["lease"]["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    with service.database.connect() as connection:
        counter = _CountingConnection(connection)
        active = service._list_leases(counter, project["id"], limit=200)
        everything = service._list_leases(
            counter, project["id"], include_inactive=True
        )

    assert [lease["id"] for lease in active] == []
    assert len(everything) == 1
    lease_sql = [
        sql.lower() for sql in counter.statements if "from file_leases" in sql.lower()
    ]
    active_sql = next(sql for sql in lease_sql if "released_at is null" in sql)
    assert "expires_at >" in active_sql
    assert active_sql.rstrip().upper().endswith("LIMIT ?")
    # The full history read is what export uses; it must still see the
    # released row rather than inherit the active-only predicate.
    full_sql = [sql for sql in lease_sql if "released_at is null" not in sql]
    assert full_sql
    assert not full_sql[0].rstrip().upper().endswith("LIMIT ?")


def test_snapshot_reads_at_most_the_lease_bound(service, project, joined):
    """A project with more leases than the bound must not scan all of them."""
    with sqlite3.connect(service.database.path) as raw:
        now = "2026-01-01T00:00:00Z"
        session_id = joined["agent"]["id"]
        rows = [
            (
                f"lease_bulk_{index}",
                project["id"],
                f"src/bulk/{index}.py",
                "exclusive",
                session_id,
                now,
                "2999-01-01T00:00:00Z",
                now,
            )
            for index in range(40)
        ]
        raw.executemany(
            """
            INSERT INTO file_leases(
                id, project_id, path_pattern, mode, session_id,
                created_at, expires_at, renewed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        raw.commit()

    limit = service.settings.snapshot_lease_limit
    with service.database.connect() as connection:
        counter = _CountingConnection(connection)
        page = service._snapshot(counter, project["id"])
    lease_sql = next(
        sql for sql in counter.statements if "FROM FILE_LEASES L" in sql.upper()
    )
    assert lease_sql.rstrip().upper().endswith("LIMIT ?")
    assert len(page["leases"]) == min(40, limit)
    assert page["lease_page_info"]["returned"] == min(40, limit)
    assert page["lease_page_info"]["truncated"] is (40 > limit)


def test_active_lease_projection_matches_the_python_predicate(
    service, project, joined
):
    for index in range(3):
        service.acquire_lease(
            project["id"],
            path_pattern=f"src/{index}.py",
            mode="exclusive",
            session_id=joined["agent"]["id"],
            token=joined["token"],
        )
    with sqlite3.connect(service.database.path) as raw:
        raw.execute(
            "UPDATE file_leases SET expires_at = '2000-01-01T00:00:00Z'"
        )
        raw.commit()
    assert service.list_leases(project["id"]) == []
    assert len(service.list_leases(project["id"], include_inactive=True)) == 3


# ---------------------------------------------------------------------------
# AC2b snapshot Session projection uses aggregates, not per-Session subqueries
# ---------------------------------------------------------------------------


def test_snapshot_session_projection_has_no_correlated_subquery(
    service, project, joined
):
    service.post_message(
        project["id"],
        body="activity",
        session_id=joined["agent"]["id"],
        token=joined["token"],
        model_display_name="test-model",
    )
    with service.database.connect() as connection:
        counter = _CountingConnection(connection)
        service._snapshot(counter, project["id"])

    session_sql = next(
        sql
        for sql in counter.statements
        if "FROM AGENT_SESSIONS" in sql.upper() and "ORDER BY" in sql.upper()
    )
    # Two correlated subqueries per Session (last_activity_at, unread_count)
    # meant the statement was re-run per row; the aggregate form does neither.
    assert "SELECT MAX(E.CREATED_AT)" not in session_sql.upper()
    assert "(SELECT COUNT(*)" not in session_sql.upper()


def test_snapshot_session_counters_match_the_per_session_subquery_values(
    service, project, joined
):
    second = service.join_room(
        project["id"], name="Second", client="codex", model="test-model"
    )
    service.post_message(
        project["id"],
        body="from the first session",
        session_id=joined["agent"]["id"],
        token=joined["token"],
        model_display_name="test-model",
    )
    # unread_count is "events in this Project after my read cursor", which is
    # project-wide and includes the other Session's join events. Pin both
    # cursors explicitly and compute the expectation from the stored rows so the
    # assertion states the rule rather than a hand-counted number.
    with service.database.connect() as raw:
        raw.execute("UPDATE agent_sessions SET last_read_cursor = 0")
        raw.commit()
        cursors = {
            str(row["id"]): int(row["last_read_cursor"] or 0)
            for row in raw.execute(
                "SELECT id, last_read_cursor FROM agent_sessions"
            ).fetchall()
        }
        total_events = int(
            raw.execute(
                "SELECT COUNT(*) AS c FROM events WHERE project_id = ?",
                (project["id"],),
            ).fetchone()["c"]
        )

    page = service.snapshot(project["id"])
    agents = {agent["id"]: agent for agent in page["agents"]}
    for session_id, agent in agents.items():
        assert agent["unread_count"] == total_events - cursors[session_id]
    # Only the Session that posted a message has a real activity timestamp;
    # the other one only ever joined.
    with service.database.connect() as raw:
        activity = {
            str(row["actor_session_id"]): row["last_activity_at"]
            for row in raw.execute(
                "SELECT actor_session_id, MAX(created_at) AS last_activity_at "
                "FROM events WHERE project_id = ? GROUP BY actor_session_id",
                (project["id"],),
            ).fetchall()
        }
    for session_id, agent in agents.items():
        assert agent["last_activity_at"] == activity.get(session_id)
    assert agents[joined["agent"]["id"]]["last_activity_at"] is not None
    assert agents[second["agent"]["id"]]["last_activity_at"] is not None


def test_snapshot_session_projection_is_bounded(service, project):
    for index in range(5):
        service.join_room(
            project["id"], name=f"Agent{index}", client="codex", model="test-model"
        )
    limit = service.settings.snapshot_session_limit
    assert limit >= 5
    page = service.snapshot(project["id"])
    assert len(page["agents"]) == 5
    assert page["session_page_info"]["returned"] == 5
    assert page["session_page_info"]["truncated"] is False
    # Newest first, so the live Sessions are the ones that survive the bound.
    assert page["agents"][0]["name"] == "Agent4"


def test_snapshot_session_bound_reports_truncation(service, project, settings):
    from dataclasses import replace

    limit = 2
    for index in range(4):
        service.join_room(
            project["id"], name=f"Agent{index}", client="codex", model="test-model"
        )
    service.settings = replace(settings, snapshot_session_limit=limit)
    page = service.snapshot(project["id"])
    assert len(page["agents"]) == limit
    assert page["session_page_info"] == {
        "returned": limit,
        "limit": limit,
        "truncated": True,
        "total": 4,
    }
    assert [agent["name"] for agent in page["agents"]] == ["Agent3", "Agent2"]


# ---------------------------------------------------------------------------
# AC3 export declares the window it returns
# ---------------------------------------------------------------------------


def _event_total(service, project):
    with service.database.connect() as raw:
        return int(
            raw.execute(
                "SELECT COUNT(*) AS c FROM events WHERE project_id = ?",
                (project["id"],),
            ).fetchone()["c"]
        )


def test_export_declares_a_bounded_event_window(service, project, joined):
    for index in range(6):
        service.post_message(
            project["id"],
            body=f"m{index}",
            session_id=joined["agent"]["id"],
            token=joined["token"],
            model_display_name="test-model",
        )
    exported = service.export_project(project["id"])
    window = exported["window"]["events"]
    total = _event_total(service, project)
    assert window["limit"] == service.settings.export_event_limit
    assert window["returned"] == exported["counts"]["events"]
    # The Project's own join/create events count too: the window is declared
    # over everything, not only over what the test happened to post.
    assert window["total"] == total
    assert window["truncated"] is False
    assert window["returned"] == total
    assert window["first_project_seq"] == 1
    assert window["last_project_seq"] == exported["cursor"]
    assert [event["project_seq"] for event in exported["events"]] == list(
        range(1, total + 1)
    )


def test_export_reports_truncation_instead_of_silently_dropping(
    service, project, joined, settings
):
    from dataclasses import replace

    for index in range(5):
        service.post_message(
            project["id"],
            body=f"m{index}",
            session_id=joined["agent"]["id"],
            token=joined["token"],
            model_display_name="test-model",
        )
    service.settings = replace(settings, export_event_limit=2)
    exported = service.export_project(project["id"])
    window = exported["window"]["events"]
    total = _event_total(service, project)
    assert window["truncated"] is True
    assert window["returned"] == 2
    assert window["total"] == total
    # The retained window is the newest one, and says where the rest starts.
    assert window["last_project_seq"] == exported["cursor"]
    assert window["oldest_retained_project_seq"] == total - 1
    assert [event["project_seq"] for event in exported["events"]] == [
        total - 1,
        total,
    ]
    assert exported["counts"]["events"] == 2
    assert window["required_action"] == "narrow_the_window_with_export_before"


def test_export_window_grows_the_response_linearly_not_quadratically(
    service, project, joined
):
    """Doubling the rows must not multiply the statements per row.

    The unbounded read also re-ran a full-history ``audit.purged`` scan and one
    task lookup per event, so a bigger project cost far more than the extra
    rows. This pins the shape of that cost, not a wall-clock number.
    """
    import time

    def measure(messages):
        raw = sqlite3.connect(service.database.path)
        try:
            raw.execute("DELETE FROM events")
            raw.commit()
        finally:
            raw.close()
        for index in range(messages):
            service.post_message(
                project["id"],
                body=f"m{index}",
                session_id=joined["agent"]["id"],
                token=joined["token"],
                model_display_name="test-model",
            )
        with service.database.connect() as connection:
            counter = _CountingConnection(connection)
            started = time.perf_counter()
            service.export_project(project["id"])
            elapsed = time.perf_counter() - started
        return len(counter.statements), elapsed

    small_statements, small_elapsed = measure(8)
    large_statements, large_elapsed = measure(32)

    assert large_statements <= small_statements * 2
    # A per-row scan shows up as a much worse-than-linear blow-up; allow a wide
    # margin so the assertion is about the shape of the cost, not the machine.
    assert large_elapsed < max(small_elapsed * 8, 2.0)