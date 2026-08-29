from __future__ import annotations

import json
import os
import subprocess

import pytest

from agentchatroom import mcp_server
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import (
    checkout_scope,
    load_checkout_registration,
    project_registration_path,
    register_checkout_project,
    remove_checkout_project_registration,
    resolve_checkout_project_key,
)


def test_checkout_registration_persists_backend_generated_project_key(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir), name="Registered")
    path = register_checkout_project(project_dir, project)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert project["project_key"].startswith("prj_")
    assert "path:" not in project["project_key"]
    assert "workbuddy" not in project["project_key"].lower()
    assert document == {
        "schema_version": 1,
        "registrations": [
            {
                "logical_path": "",
                "project_key": project["project_key"],
                "scope": {
                    "kind": "path",
                    "identity": os.path.normcase(str(project_dir.resolve())),
                    "logical_path": "",
                },
            }
        ],
    }
    serialized = path.read_text(encoding="utf-8")
    assert project["id"] not in serialized
    assert "session" not in serialized.lower()
    assert "token" not in serialized.lower()


def test_checkout_scope_is_detected_by_backend(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert checkout_scope(plain)["kind"] == "path"

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", "git@example.invalid:team/repo.git"],
        check=True,
        capture_output=True,
    )
    scope = checkout_scope(repository)
    assert scope == {
        "kind": "git",
        "identity": "https://example.invalid/team/repo",
        "logical_path": "",
    }


def test_checkout_registration_rejects_a_file_copied_from_another_scope(
    service, tmp_path
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    project = service.create_project(root_path=str(first))
    source = register_checkout_project(first, project)
    target = project_registration_path(second)
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(DomainError) as conflict:
        resolve_checkout_project_key(second)

    assert conflict.value.code == "project_registration_scope_conflict"


def test_checkout_registration_supports_logical_subprojects_and_removal(
    service, project_dir
):
    api = service.create_project(
        root_path=str(project_dir), logical_path="packages/api"
    )
    web = service.create_project(
        root_path=str(project_dir), logical_path="packages/web"
    )
    register_checkout_project(project_dir, api)
    register_checkout_project(project_dir, web)

    assert api["project_key"] != web["project_key"]
    assert load_checkout_registration(
        project_dir, logical_path="packages/api"
    )["project_key"] == api["project_key"]
    assert remove_checkout_project_registration(
        project_dir,
        project_key=api["project_key"],
        logical_path="packages/api",
    )
    assert load_checkout_registration(project_dir, logical_path="packages/api") is None
    assert load_checkout_registration(project_dir, logical_path="packages/web") is not None


def test_local_mcp_join_creates_then_joins_one_room(monkeypatch, service, project_dir):
    monkeypatch.setattr(mcp_server, "service", service)

    first = mcp_server.room_join(
        str(project_dir), "first-main", "First Agent", "generic-client", "unknown"
    )
    second = mcp_server.room_join(
        str(project_dir), "second-main", "Second Agent", "generic-client", "unknown"
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["result"]["project"]["id"] == first["result"]["project"]["id"]
    assert len(service.list_projects()) == 1
    registration = load_checkout_registration(project_dir)
    assert registration["project_key"] == first["result"]["project"]["project_key"]


def test_orphaned_checkout_registration_does_not_recreate_deleted_project(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    project = service.create_project(root_path=str(project_dir))
    register_checkout_project(project_dir, project)
    service.delete_project(project["id"])

    response = mcp_server.room_join(
        str(project_dir), "trae-main", "Trae", "trae", "unknown"
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "project_registration_orphaned"
    assert service.list_projects() == []


def test_backend_refreshes_registration_after_project_key_migration(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    project = service.create_project(root_path=str(project_dir))
    path = register_checkout_project(project_dir, project)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["registrations"][0]["project_key"] = "legacy-agent-named-key"
    path.write_text(json.dumps(document), encoding="utf-8")

    joined = mcp_server.room_join(
        str(project_dir), "codex-main", "Codex", "codex", "unknown"
    )

    assert joined["ok"] is True
    assert joined["result"]["project"]["id"] == project["id"]
    assert load_checkout_registration(project_dir)["project_key"] == project["project_key"]


def test_mcp_schema_does_not_accept_agent_supplied_project_key():
    room_join = mcp_server.mcp._tool_manager.get_tool("room_join")

    assert "Never supply, infer, or replace a project_key" in mcp_server.MCP_INSTRUCTIONS
    assert ".agentchatroom/project.json" in mcp_server.MCP_INSTRUCTIONS
    assert "project_key" not in room_join.parameters["properties"]
    assert "logical_path" in room_join.parameters["properties"]
