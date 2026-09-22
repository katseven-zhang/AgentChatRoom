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
    # #169: public event objects must not present two conflicting numbers —
    # top-level id is the project_seq, never the physical global AUTOINCREMENT.
    for event in listed["events"]:
        assert int(event["id"]) == int(event["project_seq"])


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
    assert created["event_id"] == created["project_seq"]
    assert created["event_id"] > 0
    assert created["internal_id"] > 0
    # Dual-track guard: citable number is always project_seq.
    for item in history["items"]:
        assert int(item["event_id"]) == int(item["project_seq"])


def test_write_event_id_matches_list_and_history_project_seq(
    service, project, joined_agents
):
    """前后端同一事件编号一致：post 返回的 event_id = list/history 的 project_seq。"""
    executor, _reviewer = joined_agents
    posted = service.post_message(
        project["id"],
        body="Same number everywhere",
        session_id=executor["agent"]["id"],
        token=executor["token"],
        model_display_name="Model A",
    )
    listed = service.list_events(project["id"], after=0)["events"]
    match = next(e for e in listed if e["project_seq"] == posted["event_id"])
    assert match["project_seq"] == posted["event_id"]
    assert posted["cursor"] == posted["event_id"]

    task = service.create_task(
        project["id"],
        title="Cross-surface number",
        description="History must cite the same project_seq",
        acceptance_criteria=["Same citable number"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    history = service.list_task_history(project["id"], task["id"])
    assert history["items"]
    assert all(item["event_id"] == item["project_seq"] for item in history["items"])
    assert history["cursor"] == history["next_after"] or history["next_after"] >= 0

    audit = service.query_audit(project["id"], after=0, limit=1000)
    audit_match = next(e for e in audit["events"] if e["project_seq"] == posted["event_id"])
    assert audit_match["project_seq"] == posted["event_id"]
    assert audit["cursor"] == audit["events"][-1]["project_seq"]


def test_project_seq_cursors_paginate_independently(service, tmp_path):
    """跨项目编号独立；after/cursor 在 project_seq 空间分页正确。"""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    first = service.create_project(root_path=str(tmp_path / "a"), name="Alpha")
    second = service.create_project(root_path=str(tmp_path / "b"), name="Beta")
    _emit_events(service, first["id"], 5)
    _emit_events(service, second["id"], 3)

    page1 = service.list_events(first["id"], after=0, limit=2)
    assert [e["project_seq"] for e in page1["events"]] == [1, 2]
    assert page1["cursor"] == 2

    page2 = service.list_events(first["id"], after=page1["cursor"], limit=10)
    assert all(e["project_seq"] > 2 for e in page2["events"])
    assert page2["events"][0]["project_seq"] == 3

    # Same numeric after in another project only advances that project's seq space.
    other = service.list_events(second["id"], after=2, limit=10)
    assert all(e["project_seq"] > 2 for e in other["events"])
    # create_project emits baseline events too; dense seq from 1 means after=2
    # returns exactly the tail of THIS project's sequence.
    second_all = [
        e["project_seq"] for e in service.list_events(second["id"], after=0)["events"]
    ]
    assert [e["project_seq"] for e in other["events"]] == [
        seq for seq in second_all if seq > 2
    ]
    assert second_all[-1] >= 3

    # latest_cursor is project-scoped project_seq, not a global id.
    latest_a = service.list_events(first["id"], after=0)["latest_cursor"]
    latest_b = service.list_events(second["id"], after=0)["latest_cursor"]
    assert latest_a == len(_event_project_seqs(service, first["id"]))
    assert latest_b == len(_event_project_seqs(service, second["id"]))


def test_acknowledge_resolves_project_seq_and_rejects_unknown(
    service, project, joined_agents, tmp_path
):
    """message_acknowledge 按 project_seq 解析；不存在/跨项目 → event_not_found。"""
    from agentchatroom.errors import DomainError

    sender, receiver = joined_agents
    message = service.post_message(
        project["id"],
        session_id=sender["agent"]["id"],
        token=sender["token"],
        body="Please ack by project_seq",
        model_display_name="Model A",
        requires_ack=True,
    )
    ok = service.acknowledge_event(
        project["id"],
        message["event_id"],
        receiver["agent"]["id"],
        receiver["token"],
    )
    assert ok["acknowledged"] is True
    assert ok["acknowledged_event_id"] == message["event_id"]

    with pytest.raises(DomainError) as missing:
        service.acknowledge_event(
            project["id"],
            999999,
            receiver["agent"]["id"],
            receiver["token"],
        )
    assert missing.value.code == "event_not_found"

    # A project_seq that exists only in another project must not resolve here.
    (tmp_path / "other").mkdir()
    other = service.create_project(root_path=str(tmp_path / "other"), name="Other")
    _emit_events(service, other["id"], 8)
    only_in_other = len(_event_project_seqs(service, other["id"]))
    with pytest.raises(DomainError) as cross:
        service.acknowledge_event(
            project["id"],
            only_in_other,
            receiver["agent"]["id"],
            receiver["token"],
        )
    # Only fail as not-found when this project has not yet reached that seq.
    if only_in_other > len(_event_project_seqs(service, project["id"])):
        assert cross.value.code == "event_not_found"
    else:
        # Seq number exists locally; must fail for missing requires_ack or unknown,
        # never silently ack the wrong event from another project.
        assert cross.value.code in {
            "event_not_found",
            "acknowledgement_not_required",
        }


def test_room_sync_after_uses_project_seq(service, project, joined_agents):
    executor, _reviewer = joined_agents
    posted = service.post_message(
        project["id"],
        body="Sync after cursor",
        session_id=executor["agent"]["id"],
        token=executor["token"],
        model_display_name="Model A",
    )
    synced = service.room_sync(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        after=posted["event_id"] - 1,
    )
    assert synced["cursor"] >= posted["event_id"]
    assert any(e["project_seq"] == posted["event_id"] for e in synced["events"])
    assert all(
        e["project_seq"] > posted["event_id"] - 1 for e in synced["events"]
    )


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


def test_migration_25_rewrites_knowledge_source_event_ids_to_project_seq(tmp_path):
    """#169: schema 25 converts knowledge source_event_ids from global id → project_seq."""
    import json as _json

    from agentchatroom.database import SCHEMA_VERSION

    assert SCHEMA_VERSION >= 25
    path = tmp_path / "legacy_knowledge.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta(version) VALUES (24);
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
            created_at TEXT NOT NULL,
            project_seq INTEGER
        );
        CREATE TABLE knowledge_assets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL
        );
        CREATE TABLE knowledge_asset_versions (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL DEFAULT '[]',
            body TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'decision',
            status TEXT NOT NULL DEFAULT 'candidate',
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            created_by_session_id TEXT
        );
        INSERT INTO projects(id, name, created_at, updated_at)
        VALUES ('project-a', 'A', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');
        INSERT INTO events(id, project_id, event_type, payload_json, created_at, project_seq) VALUES
            (10, 'project-a', 'message.message', '{}', '2026-09-01T00:00:01Z', 1),
            (11, 'project-a', 'message.message', '{}', '2026-09-01T00:00:02Z', 2),
            (99, 'project-b', 'message.message', '{}', '2026-09-01T00:00:03Z', 1);
        INSERT INTO knowledge_assets(id, project_id) VALUES ('ka-1', 'project-a');
        INSERT INTO knowledge_asset_versions(
            id, asset_id, source_event_ids_json, body, summary, title, kind,
            status, tags_json, created_at, created_by_session_id
        ) VALUES (
            'kav-1', 'ka-1', '[10,11]', 'body', 'sum', 't', 'decision',
            'candidate', '[]', '2026-09-01T00:00:00Z', NULL
        );
        """
    )
    connection.commit()
    connection.close()

    migrated = sqlite3.connect(path)
    migrated.row_factory = sqlite3.Row
    # Portable path shared by SQLite and PostgreSQL (#169).
    from agentchatroom.database import migrate_knowledge_source_event_ids

    migrated.executescript(MIGRATIONS[25])
    migrate_knowledge_source_event_ids(migrated)
    row = migrated.execute(
        "SELECT source_event_ids_json FROM knowledge_asset_versions WHERE id = 'kav-1'"
    ).fetchone()
    migrated.close()
    assert _json.loads(row["source_event_ids_json"]) == [1, 2]


def test_migration_25_sql_is_backend_portable():
    """#169: MIGRATIONS[25] must not use SQLite-only json_each/json_group_array."""
    from agentchatroom.database import MIGRATIONS as _M
    from agentchatroom.postgres_database import postgres_script

    script = _M[25]
    assert "json_each" not in script
    assert "json_group_array" not in script
    # postgres_script is a pure string rewrite; must not fail on migration 25.
    converted = postgres_script(script)
    assert "json_each" not in converted
    assert "json_group_array" not in converted


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
