"""Tests for Task #166: Auto-syncing git_remote and project scope when a local path project initializes Git."""

from __future__ import annotations

import json
import subprocess
import pytest
from dataclasses import replace
from pathlib import Path

from agentchatroom import bootstrap as bootstrap_module
from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import (
    PROJECT_REGISTRATION_RELATIVE_PATH,
    _is_path_to_git_upgrade,
    load_checkout_registration,
    register_checkout_project,
    resolve_checkout_project_key,
)
from agentchatroom.services import AgentChatRoomService


@pytest.fixture()
def test_service(settings):
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def _init_git_with_remote(path: Path, remote_url: str):
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


def test_path_project_autosyncs_git_remote_and_scope_on_resolve(test_service, tmp_path):
    """Local path project later initialized with git + remote auto-heals during resolve and bootstrap."""
    service = test_service
    project_dir = tmp_path / "local_repo"
    project_dir.mkdir()

    # 1. Project created without git
    project = service.create_project(root_path=str(project_dir), name="LocalRepo")
    assert project["git_remote"] is None

    # 2. Registered in path mode
    reg_path = register_checkout_project(project_dir, project)
    assert reg_path.is_file()
    reg = load_checkout_registration(project_dir)
    assert reg["scope"]["kind"] == "path"

    # Verify initial resolve works in path mode
    key, present = resolve_checkout_project_key(project_dir)
    assert present is True
    assert key == project["project_key"]

    # 3. User initializes git and configures remote later
    remote_url = "https://github.com/test-org/local-repo.git"
    _init_git_with_remote(project_dir, remote_url)

    # 4. resolve_checkout_project_key detects path-to-git upgrade without conflict
    key_after, present_after = resolve_checkout_project_key(project_dir)
    assert present_after is True
    assert key_after == project["project_key"]

    # 5. resolve_project_for_join auto-syncs git_remote and does NOT throw project_registration_orphaned
    resolved = service.resolve_project_for_join(
        root_path=str(project_dir),
        registered_project_key=project["project_key"],
    )
    assert resolved["id"] == project["id"]
    assert resolved["git_remote"] == remote_url

    # Check database row was updated
    db_proj = service.get_project(project["id"])
    assert db_proj["git_remote"] == remote_url

    # Check project.updated event was emitted
    events = service.list_events(project["id"], after=0)["events"]
    updated_events = [e for e in events if e["event_type"] == "project.updated"]
    assert len(updated_events) >= 1
    assert updated_events[-1]["payload"]["git_remote"] == remote_url

    # 6. bootstrap_local_room succeeds and updates .agentchatroom/project.json scope to git
    outcome = bootstrap_local_room(
        service,
        software_key="boot-test",
        software_name="Boot Test",
        client="test",
        model="unknown",
        workspace_roots=[project_dir],
        explicit_project_path=project_dir,
    )
    assert outcome.binding is not None

    # Verify project.json was updated to git scope
    updated_reg = load_checkout_registration(project_dir)
    assert updated_reg["scope"]["kind"] == "git"
    assert "local-repo" in updated_reg["scope"]["identity"]


def test_get_project_autosyncs_git_remote_from_disk(test_service, tmp_path):
    """get_project checks disk and syncs git_remote if git was added post-creation."""
    service = test_service
    project_dir = tmp_path / "post_git_repo"
    project_dir.mkdir()

    project = service.create_project(root_path=str(project_dir), name="PostGit")
    assert project["git_remote"] is None

    remote_url = "https://github.com/test-org/post-git.git"
    _init_git_with_remote(project_dir, remote_url)

    # Calling get_project should detect git remote and update database
    fetched = service.get_project(project["id"])
    assert fetched["git_remote"] == remote_url


def test_resolve_project_for_join_without_registered_key_autosyncs(test_service, tmp_path):
    """Even without registered_project_key, matching active path project auto-heals when git is added."""
    service = test_service
    project_dir = tmp_path / "no_key_repo"
    project_dir.mkdir()

    project = service.create_project(root_path=str(project_dir), name="NoKey")
    assert project["git_remote"] is None

    remote_url = "https://github.com/test-org/no-key.git"
    _init_git_with_remote(project_dir, remote_url)

    resolved = service.resolve_project_for_join(root_path=str(project_dir))
    assert resolved["id"] == project["id"]
    assert resolved["git_remote"] == remote_url


def test_is_path_to_git_upgrade_safety():
    """_is_path_to_git_upgrade returns False if directory identity does not match."""
    # When identity matches:
    assert _is_path_to_git_upgrade(
        r"C:\my\project",
        {"kind": "path", "identity": r"c:\my\project"},
        {"kind": "git", "identity": "https://github.com/foo/bar"},
    ) is True

    # When registered scope is already git:
    assert _is_path_to_git_upgrade(
        r"C:\my\project",
        {"kind": "git", "identity": "https://github.com/old/repo"},
        {"kind": "git", "identity": "https://github.com/new/repo"},
    ) is False

    # When directory does not match registered identity:
    assert _is_path_to_git_upgrade(
        r"C:\different\project",
        {"kind": "path", "identity": r"c:\my\project"},
        {"kind": "git", "identity": "https://github.com/foo/bar"},
    ) is False
