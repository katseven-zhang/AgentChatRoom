from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, ContextManager, Iterator, Protocol

if TYPE_CHECKING:
    from .config import Settings


SCHEMA_VERSION = 18


class DatabaseBackend(Protocol):
    backend: str

    def initialize(self) -> None: ...

    def connect(self, *, write: bool = False) -> ContextManager[Any]: ...

    def close(self) -> None: ...

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    git_remote TEXT,
    logical_path TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hosts (
    id TEXT PRIMARY KEY,
    host_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    host_id TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    local_path TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    git_remote TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, host_id, local_path)
);

CREATE INDEX IF NOT EXISTS idx_workspaces_project_host
ON workspaces(project_id, host_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS project_members (
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

CREATE TABLE IF NOT EXISTS agent_credentials (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    member_id TEXT REFERENCES project_members(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    permissions_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_credentials_project
ON agent_credentials(project_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    member_id TEXT REFERENCES project_members(id),
    agent_key TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    client TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'executor',
    status TEXT NOT NULL DEFAULT 'online',
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    host_id TEXT REFERENCES hosts(id),
    workspace_id TEXT REFERENCES workspaces(id),
    credential_id TEXT REFERENCES agent_credentials(id),
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    token_hash TEXT NOT NULL,
    token_expires_at TEXT NOT NULL,
    last_read_cursor INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    left_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_project
ON agent_sessions(project_id, last_heartbeat DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'todo',
    execution_status TEXT NOT NULL DEFAULT 'todo',
    verification_status TEXT NOT NULL DEFAULT 'not_required',
    integration_status TEXT NOT NULL DEFAULT 'pending',
    owner_session_id TEXT REFERENCES agent_sessions(id),
    created_by_session_id TEXT REFERENCES agent_sessions(id),
    progress_percent INTEGER NOT NULL DEFAULT 0,
    current_step TEXT NOT NULL DEFAULT '',
    blocker_reason TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status
ON tasks(project_id, status, priority, created_at);

CREATE TABLE IF NOT EXISTS task_number_sequences (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    next_value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_intakes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    raw_description TEXT NOT NULL,
    target_member_id TEXT NOT NULL REFERENCES project_members(id),
    target_session_id TEXT REFERENCES agent_sessions(id),
    created_by_session_id TEXT REFERENCES agent_sessions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    formal_task_id TEXT REFERENCES tasks(id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_intakes_project_status
ON task_intakes(project_id, status, created_at);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id, depends_on_task_id),
    CHECK(task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS task_assignments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    assigned_by_session_id TEXT REFERENCES agent_sessions(id),
    assigned_to_session_id TEXT REFERENCES agent_sessions(id),
    responded_by_session_id TEXT REFERENCES agent_sessions(id),
    target_role TEXT NOT NULL DEFAULT '',
    required_capability TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    response_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    responded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_assignments_task
ON task_assignments(project_id, task_id, status, created_at);

CREATE TABLE IF NOT EXISTS task_handoffs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    to_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    requested_by_session_id TEXT REFERENCES agent_sessions(id),
    responded_by_session_id TEXT REFERENCES agent_sessions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    summary TEXT NOT NULL,
    completed_items_json TEXT NOT NULL DEFAULT '[]',
    pending_items_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    next_step TEXT NOT NULL,
    response_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    responded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_handoffs_task
ON task_handoffs(project_id, task_id, status, created_at);

CREATE TABLE IF NOT EXISTS file_leases (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    task_id TEXT REFERENCES tasks(id),
    path_pattern TEXT NOT NULL,
    mode TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ttl_seconds INTEGER NOT NULL DEFAULT 1800,
    expires_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    released_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_leases_active
ON file_leases(project_id, released_at, expires_at);

CREATE TABLE IF NOT EXISTS work_reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    summary TEXT NOT NULL,
    files_json TEXT NOT NULL,
    no_code_change_reason TEXT NOT NULL DEFAULT '',
    commit_hash TEXT NOT NULL DEFAULT '',
    tests_json TEXT NOT NULL,
    system_evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    reviewer_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    verdict TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_integrations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    integrator_session_id TEXT REFERENCES agent_sessions(id),
    result TEXT NOT NULL,
    summary TEXT NOT NULL,
    files_json TEXT NOT NULL DEFAULT '[]',
    commit_hash TEXT NOT NULL DEFAULT '',
    tests_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_integrations_task
ON task_integrations(project_id, task_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_session_id TEXT REFERENCES agent_sessions(id),
    task_id TEXT REFERENCES tasks(id),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_project_cursor
ON events(project_id, id);

CREATE TABLE IF NOT EXISTS idempotency_records (
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

CREATE TABLE IF NOT EXISTS management_sessions (
    session_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_management_sessions_expires
ON management_sessions(expires_at);

CREATE TABLE IF NOT EXISTS event_acknowledgements (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(event_id, session_id)
);

CREATE TABLE IF NOT EXISTS knowledge_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    current_version_id TEXT,
    created_by_session_id TEXT REFERENCES agent_sessions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_assets_project
ON knowledge_assets(project_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_knowledge_assets_kind
ON knowledge_assets(project_id, kind, created_at);

CREATE TABLE IF NOT EXISTS knowledge_asset_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES knowledge_assets(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_task_id TEXT REFERENCES tasks(id),
    source_report_id TEXT REFERENCES work_reports(id),
    source_review_id TEXT REFERENCES reviews(id),
    source_integration_id TEXT REFERENCES task_integrations(id),
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_by_session_id TEXT REFERENCES agent_sessions(id),
    supersedes_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, version)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_asset_versions_asset
ON knowledge_asset_versions(project_id, asset_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_knowledge_asset_versions_task
ON knowledge_asset_versions(project_id, source_task_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_reviews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES knowledge_assets(id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES knowledge_asset_versions(id) ON DELETE CASCADE,
    reviewer_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
    verdict TEXT NOT NULL,
    criteria_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_reviews_asset
ON knowledge_reviews(project_id, asset_id, created_at);
"""

MIGRATIONS = {
    2: """
        ALTER TABLE agent_sessions ADD COLUMN token_expires_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z';
        ALTER TABLE file_leases ADD COLUMN renewed_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z';
        ALTER TABLE work_reports ADD COLUMN system_evidence_json TEXT NOT NULL DEFAULT '{}';
    """,
    3: """
        ALTER TABLE projects ADD COLUMN archived_at TEXT;
        CREATE TABLE IF NOT EXISTS task_dependencies (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, depends_on_task_id),
            CHECK(task_id <> depends_on_task_id)
        );
        CREATE TABLE IF NOT EXISTS event_acknowledgements (
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(event_id, session_id)
        );
    """,
    4: """
        ALTER TABLE agent_sessions ADD COLUMN last_read_cursor INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tasks ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tasks ADD COLUMN current_step TEXT NOT NULL DEFAULT '';
        ALTER TABLE tasks ADD COLUMN blocker_reason TEXT NOT NULL DEFAULT '';
        ALTER TABLE tasks ADD COLUMN next_step TEXT NOT NULL DEFAULT '';
        ALTER TABLE file_leases ADD COLUMN ttl_seconds INTEGER NOT NULL DEFAULT 1800;
    """,
    5: """
        ALTER TABLE tasks ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'todo';
        ALTER TABLE tasks ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'not_required';
        ALTER TABLE tasks ADD COLUMN integration_status TEXT NOT NULL DEFAULT 'pending';
        UPDATE tasks SET
            execution_status = CASE status
                WHEN 'awaiting_review' THEN 'completed'
                WHEN 'verified' THEN 'completed'
                WHEN 'done' THEN 'completed'
                ELSE status
            END,
            verification_status = CASE status
                WHEN 'awaiting_review' THEN 'pending'
                WHEN 'verified' THEN 'approved'
                WHEN 'done' THEN 'approved'
                ELSE 'not_required'
            END,
            integration_status = CASE status
                WHEN 'done' THEN 'done'
                ELSE 'pending'
            END;
        CREATE TABLE IF NOT EXISTS task_assignments (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            assigned_by_session_id TEXT REFERENCES agent_sessions(id),
            assigned_to_session_id TEXT REFERENCES agent_sessions(id),
            responded_by_session_id TEXT REFERENCES agent_sessions(id),
            target_role TEXT NOT NULL DEFAULT '',
            required_capability TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            note TEXT NOT NULL DEFAULT '',
            response_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            responded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_assignments_task
        ON task_assignments(project_id, task_id, status, created_at);
    """,
    6: """
        CREATE TABLE IF NOT EXISTS hosts (
            id TEXT PRIMARY KEY,
            host_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            host_id TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            local_path TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '',
            git_remote TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, host_id, local_path)
        );
        CREATE INDEX IF NOT EXISTS idx_workspaces_project_host
        ON workspaces(project_id, host_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS agent_credentials (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            permissions_json TEXT NOT NULL DEFAULT '[]',
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_credentials_project
        ON agent_credentials(project_id, revoked_at, expires_at);
        ALTER TABLE agent_sessions ADD COLUMN host_id TEXT REFERENCES hosts(id);
        ALTER TABLE agent_sessions ADD COLUMN workspace_id TEXT REFERENCES workspaces(id);
        ALTER TABLE agent_sessions ADD COLUMN credential_id TEXT REFERENCES agent_credentials(id);
    """,
    7: """
        ALTER TABLE work_reports ADD COLUMN no_code_change_reason TEXT NOT NULL DEFAULT '';
    """,
    8: """
        CREATE TABLE IF NOT EXISTS task_handoffs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            from_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
            to_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
            requested_by_session_id TEXT REFERENCES agent_sessions(id),
            responded_by_session_id TEXT REFERENCES agent_sessions(id),
            status TEXT NOT NULL DEFAULT 'pending',
            summary TEXT NOT NULL,
            completed_items_json TEXT NOT NULL DEFAULT '[]',
            pending_items_json TEXT NOT NULL DEFAULT '[]',
            files_json TEXT NOT NULL DEFAULT '[]',
            risks_json TEXT NOT NULL DEFAULT '[]',
            next_step TEXT NOT NULL,
            response_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            responded_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_handoffs_task
        ON task_handoffs(project_id, task_id, status, created_at);
        CREATE TABLE IF NOT EXISTS task_integrations (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            integrator_session_id TEXT REFERENCES agent_sessions(id),
            result TEXT NOT NULL,
            summary TEXT NOT NULL,
            files_json TEXT NOT NULL DEFAULT '[]',
            commit_hash TEXT NOT NULL DEFAULT '',
            tests_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_integrations_task
        ON task_integrations(project_id, task_id, created_at);
    """,
    9: """
        ALTER TABLE agent_sessions ADD COLUMN left_at TEXT;
        CREATE TABLE IF NOT EXISTS idempotency_records (
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
    10: """
        CREATE TABLE IF NOT EXISTS management_sessions (
            session_hash TEXT PRIMARY KEY,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_management_sessions_expires
        ON management_sessions(expires_at);
    """,
    11: """
        CREATE TABLE IF NOT EXISTS project_members (
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
        CREATE INDEX IF NOT EXISTS idx_agent_credentials_member
        ON agent_credentials(project_id, member_id, revoked_at, expires_at);
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_member
        ON agent_sessions(project_id, member_id, created_at);
    """,
    12: """
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_project_agent_key
        ON agent_sessions(project_id, agent_key, last_heartbeat DESC);
    """,
    13: """SELECT 1;""",
    14: """DROP TABLE IF EXISTS project_deletion_markers;""",
    15: """
        CREATE TABLE IF NOT EXISTS knowledge_assets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            owner_kind TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            current_version_id TEXT,
            created_by_session_id TEXT REFERENCES agent_sessions(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_assets_project
        ON knowledge_assets(project_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_knowledge_assets_kind
        ON knowledge_assets(project_id, kind, created_at);
        CREATE TABLE IF NOT EXISTS knowledge_asset_versions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES knowledge_assets(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_task_id TEXT REFERENCES tasks(id),
            source_report_id TEXT REFERENCES work_reports(id),
            source_review_id TEXT REFERENCES reviews(id),
            source_integration_id TEXT REFERENCES task_integrations(id),
            source_event_ids_json TEXT NOT NULL DEFAULT '[]',
            created_by_session_id TEXT REFERENCES agent_sessions(id),
            supersedes_version_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(asset_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_asset_versions_asset
        ON knowledge_asset_versions(project_id, asset_id, version DESC);
        CREATE INDEX IF NOT EXISTS idx_knowledge_asset_versions_task
        ON knowledge_asset_versions(project_id, source_task_id, created_at);
        CREATE TABLE IF NOT EXISTS knowledge_reviews (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES knowledge_assets(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES knowledge_asset_versions(id) ON DELETE CASCADE,
            reviewer_session_id TEXT NOT NULL REFERENCES agent_sessions(id),
            verdict TEXT NOT NULL,
            criteria_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_reviews_asset
        ON knowledge_reviews(project_id, asset_id, created_at);
    """,
    16: """
        UPDATE projects
        SET project_key = 'prj_' || substr(id, 9)
        WHERE project_key <> 'prj_' || substr(id, 9);
    """,
    17: """
        CREATE TABLE IF NOT EXISTS task_intakes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            raw_description TEXT NOT NULL,
            target_member_id TEXT NOT NULL REFERENCES project_members(id),
            target_session_id TEXT REFERENCES agent_sessions(id),
            created_by_session_id TEXT REFERENCES agent_sessions(id),
            status TEXT NOT NULL DEFAULT 'pending',
            formal_task_id TEXT REFERENCES tasks(id),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_intakes_project_status
        ON task_intakes(project_id, status, created_at);
        UPDATE tasks
        SET task_number = (
            SELECT COUNT(*)
            FROM tasks earlier
            WHERE earlier.project_id = tasks.project_id
              AND (earlier.created_at < tasks.created_at
                   OR (earlier.created_at = tasks.created_at AND earlier.id <= tasks.id))
        )
        WHERE task_number IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_project_task_number
        ON tasks(project_id, task_number);
    """,
    18: """
        CREATE TABLE IF NOT EXISTS task_number_sequences (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            next_value INTEGER NOT NULL
        );
        INSERT INTO task_number_sequences(project_id, next_value)
        SELECT project_id, COALESCE(MAX(task_number), 0) + 1
        FROM tasks
        GROUP BY project_id;
    """,
}


def ensure_project_member_columns(connection: Any, *, postgres: bool = False) -> None:
    """Add v11 member links to databases whose base tables predate the model."""
    if postgres:
        rows = connection.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND ((table_name = 'agent_credentials' AND column_name = 'member_id')
                   OR (table_name = 'agent_sessions' AND column_name = 'member_id'))
            """
        ).fetchall()
        existing = {(row["table_name"], row["column_name"]) for row in rows}
    else:
        existing = {
            (table, row["name"])
            for table in ("agent_credentials", "agent_sessions")
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
    definitions = {
        ("agent_credentials", "member_id"): (
            "ALTER TABLE agent_credentials ADD COLUMN member_id TEXT REFERENCES project_members(id)"
        ),
        ("agent_sessions", "member_id"): (
            "ALTER TABLE agent_sessions ADD COLUMN member_id TEXT REFERENCES project_members(id)"
        ),
    }
    for key, statement in definitions.items():
        if key not in existing:
            connection.execute(statement)


def ensure_task_number_schema(connection: Any, *, postgres: bool = False) -> None:
    """Add the Project-scoped task number column/index to legacy databases."""
    if postgres:
        row = connection.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'tasks'
              AND column_name = 'task_number'
            """
        ).fetchone()
        exists = row is not None
    else:
        exists = any(
            row["name"] == "task_number"
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        )
    if not exists:
        connection.execute("ALTER TABLE tasks ADD COLUMN task_number INTEGER")
        connection.execute(
            """
            UPDATE tasks
            SET task_number = (
                SELECT COUNT(*)
                FROM tasks earlier
                WHERE earlier.project_id = tasks.project_id
                  AND (earlier.created_at < tasks.created_at
                       OR (earlier.created_at = tasks.created_at AND earlier.id <= tasks.id))
            )
            WHERE task_number IS NULL
            """
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_project_task_number
        ON tasks(project_id, task_number)
        """
    )


def ensure_agent_identity_columns(connection: Any, *, postgres: bool = False) -> None:
    """Add v12 stable Agent identity to existing databases before indexing it."""
    if postgres:
        row = connection.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'agent_sessions'
              AND column_name = 'agent_key'
            """
        ).fetchone()
        exists = row is not None
    else:
        exists = any(
            row["name"] == "agent_key"
            for row in connection.execute("PRAGMA table_info(agent_sessions)").fetchall()
        )
    if not exists:
        connection.execute(
            "ALTER TABLE agent_sessions ADD COLUMN agent_key TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_project_agent_key
        ON agent_sessions(project_id, agent_key, last_heartbeat DESC)
        """
    )


class Database:
    backend = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._active_connection: ContextVar[
            tuple[sqlite3.Connection, bool] | None
        ] = ContextVar(f"agentchatroom_connection_{id(self)}", default=None)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(write=True) as connection:
            connection.executescript(SCHEMA)
            ensure_project_member_columns(connection)
            ensure_agent_identity_columns(connection)
            ensure_task_number_schema(connection)
            row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
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
                    connection.execute("UPDATE schema_meta SET version = ?", (target,))
                    version = target

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        active = self._active_connection.get()
        if active is not None:
            connection, active_write = active
            if write and not active_write:
                raise RuntimeError("Cannot open a nested write inside a read transaction")
            yield connection
            return

        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if write:
            connection.execute("PRAGMA journal_mode = WAL")
        binding = self._active_connection.set((connection, write))
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._active_connection.reset(binding)
            connection.close()

    def close(self) -> None:
        return None


def create_database(settings: "Settings") -> DatabaseBackend:
    if settings.database_backend == "sqlite":
        return Database(settings.database_path)
    if settings.database_backend != "postgresql":
        raise ValueError(f"Unsupported database backend: {settings.database_backend}")

    database_url = os.getenv(settings.database_url_env, "").strip()
    if not database_url:
        raise ValueError(
            f"{settings.database_url_env} must contain a PostgreSQL connection URL"
        )
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError(
            f"{settings.database_url_env} must contain a postgresql:// connection URL"
        )

    from .postgres_database import PostgresDatabase

    return PostgresDatabase(
        database_url,
        pool_min_size=settings.database_pool_min_size,
        pool_max_size=settings.database_pool_max_size,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
