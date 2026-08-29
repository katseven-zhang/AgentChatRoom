from __future__ import annotations

import sqlite3

from agentchatroom.database import Database, SCHEMA, SCHEMA_VERSION


def _schema_without_v9_reliability() -> str:
    return SCHEMA.replace(
        "    last_heartbeat TEXT NOT NULL,\n    left_at TEXT\n",
        "    last_heartbeat TEXT NOT NULL\n",
    ).replace(
        """CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(scope, request_id)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_records_created
ON idempotency_records(created_at);

""",
        "",
    )


def _schema_without_project_members() -> str:
    return (
        _schema_without_v9_reliability()
        .replace(
            """CREATE TABLE IF NOT EXISTS project_members (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    member_key TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'agent',
    role TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(project_id, member_key)
);

CREATE INDEX IF NOT EXISTS idx_project_members_project_status
ON project_members(project_id, status, updated_at DESC);

""",
            "",
        )
        .replace("    member_id TEXT REFERENCES project_members(id),\n", "")
    )


def test_database_schema_is_current(settings):
    database = Database(settings.database_path)
    database.initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        agent_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        lease_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(file_leases)")
        }
        report_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(work_reports)")
        }
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert "token_expires_at" in agent_columns
    assert "last_read_cursor" in agent_columns
    assert "left_at" in agent_columns
    assert "agent_key" in agent_columns
    assert "renewed_at" in lease_columns
    assert "ttl_seconds" in lease_columns
    assert "system_evidence_json" in report_columns
    assert "no_code_change_reason" in report_columns
    assert {
        "progress_percent",
        "current_step",
        "blocker_reason",
        "next_step",
        "execution_status",
        "verification_status",
        "integration_status",
    } <= task_columns
    assert {
        "task_handoffs",
        "task_integrations",
        "idempotency_records",
        "management_sessions",
        "project_members",
    } <= tables
    assert "member_id" in agent_columns


def _schema_without_task_state_dimensions() -> str:
    return (
        _schema_without_v9_reliability()
        .replace("    execution_status TEXT NOT NULL DEFAULT 'todo',\n", "")
        .replace(
            "    verification_status TEXT NOT NULL DEFAULT 'not_required',\n", ""
        )
        .replace("    integration_status TEXT NOT NULL DEFAULT 'pending',\n", "")
        .replace("    host_id TEXT REFERENCES hosts(id),\n", "")
        .replace("    workspace_id TEXT REFERENCES workspaces(id),\n", "")
        .replace(
            "    credential_id TEXT REFERENCES agent_credentials(id),\n", ""
        )
        .replace("    no_code_change_reason TEXT NOT NULL DEFAULT '',\n", "")
    )


def test_database_migrates_v1_to_current(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy_schema = (
        _schema_without_task_state_dimensions()
        .replace("    token_expires_at TEXT NOT NULL,\n", "")
        .replace("    renewed_at TEXT NOT NULL,\n", "")
        .replace("    system_evidence_json TEXT NOT NULL DEFAULT '{}',\n", "")
        .replace("    archived_at TEXT,\n", "")
        .replace("    last_read_cursor INTEGER NOT NULL DEFAULT 0,\n", "")
        .replace("    progress_percent INTEGER NOT NULL DEFAULT 0,\n", "")
        .replace("    current_step TEXT NOT NULL DEFAULT '',\n", "")
        .replace("    blocker_reason TEXT NOT NULL DEFAULT '',\n", "")
        .replace("    next_step TEXT NOT NULL DEFAULT '',\n", "")
        .replace("    ttl_seconds INTEGER NOT NULL DEFAULT 1800,\n", "")
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO schema_meta(version) VALUES (1)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        agent_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }

    assert version == SCHEMA_VERSION
    assert "token_expires_at" in agent_columns
    assert "last_read_cursor" in agent_columns


def test_database_migrates_v4_task_states_to_current(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(_schema_without_task_state_dimensions())
        connection.execute("INSERT INTO schema_meta(version) VALUES (4)")
        connection.execute(
            """
            INSERT INTO projects(
                id, project_key, name, root_path, created_at, updated_at
            ) VALUES ('project_1', 'project:key', 'Project', 'C:/project', 'now', 'now')
            """
        )
        for status in (
            "todo",
            "claimed",
            "in_progress",
            "blocked",
            "awaiting_review",
            "verified",
            "done",
            "cancelled",
        ):
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_id, title, acceptance_criteria_json,
                    status, created_at, updated_at
                ) VALUES (?, 'project_1', ?, '[\"passes\"]', ?, 'now', 'now')
                """,
                (f"task_{status}", status, status),
            )

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        states = {
            row["status"]: (
                row["execution_status"],
                row["verification_status"],
                row["integration_status"],
            )
            for row in connection.execute(
                """
                SELECT status, execution_status, verification_status, integration_status
                FROM tasks
                """
            )
        }
        assignment_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(task_assignments)")
        }

    assert version == SCHEMA_VERSION
    assert states == {
        "todo": ("todo", "not_required", "pending"),
        "claimed": ("claimed", "not_required", "pending"),
        "in_progress": ("in_progress", "not_required", "pending"),
        "blocked": ("blocked", "not_required", "pending"),
        "awaiting_review": ("completed", "pending", "pending"),
        "verified": ("completed", "approved", "pending"),
        "done": ("completed", "approved", "done"),
        "cancelled": ("cancelled", "not_required", "pending"),
    }
    assert {
        "assigned_by_session_id",
        "assigned_to_session_id",
        "responded_by_session_id",
        "target_role",
        "required_capability",
        "status",
    } <= assignment_columns


def test_database_migrates_v5_identity_tables_and_session_links(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy_schema = (
        _schema_without_v9_reliability()
        .replace("    host_id TEXT REFERENCES hosts(id),\n", "")
        .replace("    workspace_id TEXT REFERENCES workspaces(id),\n", "")
        .replace(
            "    credential_id TEXT REFERENCES agent_credentials(id),\n", ""
        )
        .replace("    no_code_change_reason TEXT NOT NULL DEFAULT '',\n", "")
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO schema_meta(version) VALUES (5)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert {"host_id", "workspace_id", "credential_id"} <= session_columns
    assert {"hosts", "workspaces", "agent_credentials"} <= tables


def test_database_migrates_v6_work_reports_to_structured_no_code_reason(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy_schema = _schema_without_v9_reliability().replace(
        "    no_code_change_reason TEXT NOT NULL DEFAULT '',\n", ""
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO schema_meta(version) VALUES (6)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        report_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(work_reports)")
        }

    assert version == SCHEMA_VERSION
    assert "no_code_change_reason" in report_columns


def test_database_migrates_v7_to_handoff_and_integration_tables(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(_schema_without_v9_reliability())
        connection.execute("INSERT INTO schema_meta(version) VALUES (7)")
        connection.execute("DROP TABLE task_handoffs")
        connection.execute("DROP TABLE task_integrations")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert {"task_handoffs", "task_integrations"} <= tables


def test_database_migrates_v8_to_idempotency_and_session_lifecycle(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(_schema_without_v9_reliability())
        connection.execute("INSERT INTO schema_meta(version) VALUES (8)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert "left_at" in session_columns
    assert {"idempotency_records", "management_sessions"} <= tables


def test_database_migrates_v9_to_persistent_management_sessions(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(_schema_without_v9_reliability())
        connection.execute("INSERT INTO schema_meta(version) VALUES (9)")
        connection.execute("DROP TABLE management_sessions")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert "management_sessions" in tables


def test_database_migrates_v10_to_project_members(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(_schema_without_project_members())
        connection.execute("INSERT INTO schema_meta(version) VALUES (10)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        credential_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_credentials)")
        }
        session_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }

    assert version == SCHEMA_VERSION
    assert "project_members" in tables
    assert "member_id" in credential_columns
    assert "member_id" in session_columns


def test_database_migrates_v11_to_stable_agent_identity(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy_schema = SCHEMA.replace(
        "    agent_key TEXT NOT NULL DEFAULT '',\n", ""
    ).replace(
        """CREATE INDEX IF NOT EXISTS idx_agent_sessions_project_agent_key
ON agent_sessions(project_id, agent_key, last_heartbeat DESC);

""",
        "",
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO schema_meta(version) VALUES (11)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        session_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(agent_sessions)")
        }

    assert version == SCHEMA_VERSION
    assert "agent_key" in session_columns
    assert "idx_agent_sessions_project_agent_key" in indexes


def test_database_migrates_v13_removes_project_deletion_markers(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(SCHEMA)
        connection.executescript(
            """
            CREATE TABLE project_deletion_markers (
                project_key TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                root_path TEXT NOT NULL,
                git_remote TEXT,
                logical_path TEXT NOT NULL DEFAULT '',
                deleted_at TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO schema_meta(version) VALUES (13)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert "project_deletion_markers" not in tables


def test_database_migrates_v15_project_keys_to_backend_generated_form(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            """
            INSERT INTO projects(
                id, project_key, name, root_path, git_remote, logical_path,
                settings_json, archived_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, '', '{}', NULL, ?, ?)
            """,
            [
                (
                    "project_1234567890abcdef1234",
                    "path:d:/example:",
                    "Example",
                    "D:/example",
                    "2026-08-29T00:00:00Z",
                    "2026-08-29T00:00:00Z",
                ),
                (
                    "project_abcdef1234567890abcd",
                    "prj_semantic-but-wrong",
                    "Malformed Prefix",
                    "D:/malformed-prefix",
                    "2026-08-29T00:00:00Z",
                    "2026-08-29T00:00:00Z",
                ),
            ],
        )
        connection.execute("INSERT INTO schema_meta(version) VALUES (15)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        project_keys = dict(
            connection.execute("SELECT id, project_key FROM projects").fetchall()
        )

    assert version == SCHEMA_VERSION
    assert project_keys == {
        "project_1234567890abcdef1234": "prj_1234567890abcdef1234",
        "project_abcdef1234567890abcd": "prj_abcdef1234567890abcd",
    }
