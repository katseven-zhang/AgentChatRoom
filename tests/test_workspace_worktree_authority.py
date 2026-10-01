"""Task #345: a Session worktree is caller-supplied text that later acts as a path.

Registering a workspace already rejected UNC and relative paths, but joining a
Room stored whatever the client sent. The git evidence capture and the permanent
Project delete both read that column as a filesystem root, so one Session could
point the server at another machine (UNC credential leak) or rewrite an unrelated
checkout's AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import (
    PROJECT_INSTRUCTIONS_BEGIN,
    project_instructions_path,
    write_project_coordination_instructions,
)
from tests.conftest import join_room_with_workspace


def _join(service, project, **overrides):
    values = {
        "agent_key": "builder-main",
        "name": "Builder",
        "client": "codex",
        "model": "test-model",
        "role": "executor",
    }
    values.update(overrides)
    return join_room_with_workspace(service, project, **values)


def _only_session_id(service, project_id):
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT id FROM agent_sessions WHERE project_id = ?", (project_id,)
        ).fetchone()
    return str(row["id"])


def _store_worktree(service, session_id, worktree):
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET worktree = ? WHERE id = ?",
            (worktree, session_id),
        )


@pytest.mark.parametrize(
    "worktree",
    [r"\\fileserver\share\repo", "//fileserver/share/repo", "../sibling", "packages/api"],
)
def test_join_room_rejects_a_worktree_the_server_cannot_own(
    service, project, worktree
):
    with pytest.raises(DomainError) as error:
        _join(service, project, worktree=worktree)
    assert error.value.code == "invalid_workspace"
    with service.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM agent_sessions"
        ).fetchone()["total"] == 0


def test_join_room_rejects_control_characters_in_branch(service, project):
    with pytest.raises(DomainError) as error:
        _join(service, project, branch="main\nsh")
    assert error.value.code == "invalid_agent"


def test_cleanup_scope_ignores_a_legacy_foreign_worktree(service, project, tmp_path):
    _join(service, project)
    session_id = _only_session_id(service, project["id"])
    victim = tmp_path / "victim-checkout"
    victim.mkdir()
    _store_worktree(service, session_id, str(victim))

    scope = service.checkout_cleanup_scope(project["id"])

    assert scope["paths"] == [str(project["root_path"])]
    assert str(victim) not in scope["paths"]
    assert scope["project_key"] == project["project_key"]


def test_cleanup_scope_keeps_a_worktree_inside_the_project(service, project, tmp_path):
    _join(service, project)
    session_id = _only_session_id(service, project["id"])
    nested = Path(project["root_path"]) / "worktrees" / "feature"
    _store_worktree(service, session_id, str(nested))

    assert service.checkout_cleanup_scope(project["id"])["paths"] == [
        str(project["root_path"]),
        str(nested),
    ]


def test_relative_reported_worktree_anchors_at_the_workspace_root(
    service, project, tmp_path, monkeypatch
):
    _join(service, project)
    session_id = _only_session_id(service, project["id"])
    _store_worktree(service, session_id, "sub")
    root = Path(project["root_path"])
    (root / "sub").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with service.database.connect() as connection:
        project_row = connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project["id"],)
        ).fetchone()
        agent_row = connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        trusted = service._trusted_worktree(connection, project_row, agent_row)

    assert trusted == (root / "sub").resolve()
    assert trusted != (elsewhere / "sub").resolve()


def test_permanent_delete_never_touches_a_foreign_checkout(
    settings, service, project, tmp_path
):
    _join(service, project)
    session_id = _only_session_id(service, project["id"])
    victim = tmp_path / "victim-checkout"
    victim.mkdir()
    # A real managed block: the cleanup only rewrites files it recognises, so a
    # hand-made marker would hide whether the foreign path was even visited.
    write_project_coordination_instructions(victim, project)
    victim_instructions = project_instructions_path(victim)
    before = victim_instructions.read_text(encoding="utf-8")
    assert PROJECT_INSTRUCTIONS_BEGIN in before
    _store_worktree(service, session_id, str(victim))

    with TestClient(create_app(settings)) as client:
        deleted = client.delete(f"/api/v1/projects/{project['id']}?permanent=true")
        assert deleted.status_code == 200

    assert victim_instructions.read_text(encoding="utf-8") == before
