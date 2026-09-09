from __future__ import annotations

import sqlite3
import threading

import pytest

from agentchatroom.config import Settings
from agentchatroom.database import MIGRATIONS, Database, ensure_event_number_schema
from agentchatroom.services import AgentChatRoomService


def _event_project_seqs(service, project_id: str) -> list[int]:
    with service.database.connect() as connection:
        rows = connection.execute(
            "SELECT project_seq FROM events WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return [int(row["project_seq"]) for row in rows]


def _emit_events(service, project_id: str, count: int) -> None:
    with service.database.connect(write=True) as connection:
        for _ in range(count):
            service._emit(connection, project_id, "message.system", payload={})


def test_project_sequences_are_independent_and_dense(service, tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    first = service.create_project(root_path=str(tmp_path / "a"), name="Alpha")
    second = service.create_project(root_path=str(tmp_path / "b"), name="Beta")
    # 项目创建本身会产生事件：基线必须已经是从 1 开始的连续序号。
    baseline_first = _event_project_seqs(service, first["id"])
    baseline_second = _event_project_seqs(service, second["id"])
    assert baseline_first == list(range(1, len(baseline_first) + 1))
    assert baseline_second == list(range(1, len(baseline_second) + 1))

    _emit_events(service, first["id"], 3)
    _emit_events(service, second["id"], 2)
    _emit_events(service, first["id"], 1)

    first_seqs = _event_project_seqs(service, first["id"])
    second_seqs = _event_project_seqs(service, second["id"])
    assert first_seqs == baseline_first + list(
        range(len(baseline_first) + 1, len(baseline_first) + 5)
    )
    assert second_seqs == baseline_second + list(
        range(len(baseline_second) + 1, len(baseline_second) + 3)
    )


def test_snapshot_audit_and_task_history_carry_project_seq(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    service.post_message(
        project["id"],
        body="Numbered hello",
        session_id=executor["agent"]["id"],
        token=executor["token"],
        model_display_name="Model A",
    )
    # Room 动态、最近活动与管理 Tab 审计共用 list_events 投影。
    listed = service.list_events(project["id"], after=0)
    listed_message = next(
        event for event in listed["events"] if event["event_type"] == "message.message"
    )
    assert int(listed_message["project_seq"]) > 0
    assert all("project_seq" in event for event in listed["events"])


def test_task_history_items_expose_project_seq(service, project, joined_agents):
    executor, _reviewer = joined_agents
    created_task = service.create_task(
        project["id"],
        title="History numbering",
        description="Check the timeline carries project seq",
        acceptance_criteria=["Timeline carries the project-scoped event number"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    history = service.list_task_history(project["id"], created_task["task"]["id"])
    created = next(
        item for item in history["items"] if item["event_type"] == "task.created"
    )
    assert created["project_seq"] > 0
    assert created["event_id"] > 0


def test_event_sequence_survives_concurrent_writers(service, project):
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(5):
                with service.database.connect(write=True) as connection:
                    service._emit(connection, project["id"], "message.system", payload={})
        except Exception as exc:  # pragma: no cover - surfaced via assertions
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    seqs = _event_project_seqs(service, project["id"])
    assert len(seqs) == len(set(seqs))
    assert seqs == list(range(1, len(seqs) + 1))


def test_schema22_backfill_preserves_history_and_seeds_counter(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta(version) VALUES (21);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_session_id TEXT,
            task_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE event_number_sequences (
            project_id TEXT PRIMARY KEY,
            next_value INTEGER NOT NULL
        );
        INSERT INTO projects(id, name, created_at, updated_at) VALUES
            ('project-a', 'A', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z'),
            ('project-b', 'B', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');
        INSERT INTO events(project_id, event_type, payload_json, created_at) VALUES
            ('project-a', 'project.created', '{}', '2026-09-01T00:00:01Z'),
            ('project-b', 'project.created', '{}', '2026-09-01T00:00:02Z'),
            ('project-a', 'message.message', '{"body":"first"}', '2026-09-01T00:00:03Z'),
            ('project-a', 'message.message', '{"body":"second"}', '2026-09-01T00:00:04Z'),
            ('project-b', 'message.message', '{"body":"other"}', '2026-09-01T00:00:05Z');
        """
    )
    before = connection.execute(
        "SELECT id, event_type, payload_json, created_at FROM events ORDER BY id"
    ).fetchall()
    connection.commit()
    connection.close()

    migrated = sqlite3.connect(path)
    migrated.row_factory = sqlite3.Row
    # 与 initialize() 的真实顺序一致：先补列/索引（ensure），再回填（migration）。
    ensure_event_number_schema(migrated)
    migrated.executescript(MIGRATIONS[22])
    rows = migrated.execute(
        "SELECT id, project_id, project_seq, event_type, payload_json, created_at"
        " FROM events ORDER BY id"
    ).fetchall()
    counter = migrated.execute(
        "SELECT project_id, next_value FROM event_number_sequences ORDER BY project_id"
    ).fetchall()
    migrated.close()

    # 回填：各项目按 (project_id, id) 顺序从 1 连续递增，互不影响。
    seqs = {(row["project_id"], row["id"]): row["project_seq"] for row in rows}
    assert [seqs[("project-a", 1)], seqs[("project-a", 3)], seqs[("project-a", 4)]] == [1, 2, 3]
    assert [seqs[("project-b", 2)], seqs[("project-b", 5)]] == [1, 2]
    # append-only：既有字段与顺序零改动。
    assert [row["id"] for row in rows] == [row["id"] for row in before]
    for row, original in zip(rows, before):
        assert row["event_type"] == original["event_type"]
        assert row["payload_json"] == original["payload_json"]
        assert row["created_at"] == original["created_at"]
    # 计数器按项目种子化到 MAX(seq)+1。
    assert {row["project_id"]: row["next_value"] for row in counter} == {
        "project-a": 4,
        "project-b": 3,
    }


def test_migrated_database_continues_numbering_after_backfill(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta(version) VALUES (21);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_session_id TEXT,
            task_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        INSERT INTO projects(id, name, created_at, updated_at)
        VALUES ('project-a', 'A', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');
        INSERT INTO events(project_id, event_type, payload_json, created_at) VALUES
            ('project-a', 'project.created', '{}', '2026-09-01T00:00:01Z'),
            ('project-a', 'message.message', '{}', '2026-09-01T00:00:02Z');
        """
    )
    connection.commit()
    connection.close()

    service = AgentChatRoomService(
        Database(path), Settings(data_dir=tmp_path / "data")
    )
    service.initialize()
    with service.database.connect(write=True) as db_connection:
        service._emit(db_connection, "project-a", "message.system", payload={})
        service._emit(db_connection, "project-a", "message.system", payload={})

    seqs = _event_project_seqs(service, "project-a")
    assert seqs == [1, 2, 3, 4]
