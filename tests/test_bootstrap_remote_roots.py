"""Bootstrap matching for roots the server cannot see, and restore-time notices."""
# audit-allow-file:windows_absolute_path

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from agentchatroom import mcp_server
from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.project_registration import (
    project_registration_path,
    register_checkout_project,
)


def _boot(service, root, **extra):
    return bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        **extra,
    )


def test_invisible_windows_root_uses_the_database_project_path(service, tmp_path):
    project_dir = tmp_path / "room"
    other_dir = tmp_path / "other"
    project_dir.mkdir()
    other_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Remote")
    other = service.create_project(root_path=str(other_dir), name="Other")
    service.register_workspace(
        project["id"],
        host_key="host:remote",
        host_name="Remote",
        local_path=r"Z:\MissingRoom\Proj",
    )
    service.register_workspace(
        other["id"],
        host_key="host:other",
        host_name="Other",
        local_path=r"Z:\OtherRoom\Proj",
    )

    outcome = _boot(
        service,
        project_dir,
        workspace_roots=[r"z:/missingroom/proj"],
        database_first=True,
    )

    assert outcome.public["status"] == "ready"
    assert outcome.public["project"]["id"] == project["id"]
    recorded = [item["local_path"] for item in service.list_workspaces(project["id"])]
    server_cwd = str(Path.cwd())
    assert recorded
    assert not any(
        "missingroom" in item.casefold() and server_cwd.casefold() in item.casefold()
        for item in recorded
    )
    assert any(Path(item).resolve() == project_dir.resolve() for item in recorded)


def test_equivalent_windows_roots_on_two_projects_stay_ambiguous(service, tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = service.create_project(root_path=str(first_dir), name="First")
    second = service.create_project(root_path=str(second_dir), name="Second")
    for project in (first, second):
        service.register_workspace(
            project["id"],
            host_key=f"host:{project['id']}",
            host_name="Shared",
            local_path=r"Z:\SharedRoom\Proj",
        )

    outcome = _boot(
        service,
        first_dir,
        workspace_roots=[r"Z:/sharedroom/proj"],
        database_first=True,
    )

    assert outcome.public["status"] == "ambiguous_workspace"
    assert outcome.binding is None


def test_subproject_bootstrap_does_not_rewrite_registration_twice(service, tmp_path):
    repository = tmp_path / "repository"
    api_dir = repository / "packages" / "api"
    api_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    project = service.create_project(root_path=str(api_dir), name="API")
    register_checkout_project(api_dir, project)
    registration = project_registration_path(api_dir)

    first = _boot(service, api_dir, cwd=api_dir)
    assert first.public["status"] == "ready"
    body = registration.read_bytes()
    second = _boot(service, api_dir, cwd=api_dir)
    assert second.public["status"] == "ready"
    assert registration.read_bytes() == body
    assert "project_registration_refreshed" not in [
        notice["code"] for notice in second.public.get("notices") or []
    ]


def test_restored_bootstrap_reports_the_checkout_write(service, tmp_path):
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Restore")
    register_checkout_project(project_dir, project)
    first = _boot(service, project_dir, cwd=project_dir)
    assert first.binding is not None
    instructions = project_dir / "AGENTS.md"
    original = instructions.read_text(encoding="utf-8")
    instructions.write_text(original.replace("room_sync", "legacy-sync-marker"), encoding="utf-8")

    restored = _boot(
        service,
        project_dir,
        cwd=project_dir,
        restore_binding=first.binding,
    )

    codes = [notice["code"] for notice in restored.public.get("notices") or []]
    assert restored.public["connection"]["room_session"] == "restored"
    assert "runtime_session_restored" in codes
    assert "project_instructions_updated" in codes
    assert codes != ["runtime_session_restored"]
    assert "legacy-sync-marker" not in instructions.read_text(encoding="utf-8")


def test_bootstrap_does_not_query_sessions_by_hand():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "agentchatroom" / "bootstrap.py"
    ).read_text(encoding="utf-8")
    assert "SELECT member_id FROM agent_sessions" not in source
    assert "verify_session(" in source


def test_http_identity_mismatch_omits_workspace_root_advice(monkeypatch, service, tmp_path):
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Pinned")
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    credential = service.issue_agent_token(
        project["id"], name="Linked", member_id=member["id"]
    )["credential"]
    provider = mcp_server._bound_service_provider.set(lambda: service)
    roots = mcp_server._bootstrap_workspace_roots.set([project_dir])
    identity = mcp_server._bootstrap_http_identity.set(
        ("impostor", "Impostor", "impostor")
    )
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.setattr(
        mcp_server,
        "get_access_token",
        lambda: SimpleNamespace(claims={"credential_id": credential["id"]}),
    )
    try:
        result = mcp_server.room_bootstrap()
    finally:
        mcp_server._bound_service_provider.reset(provider)
        mcp_server._bootstrap_workspace_roots.reset(roots)
        mcp_server._bootstrap_http_identity.reset(identity)

    assert result["ok"] is False
    assert result["result"]["status"] == "identity_mismatch"
    assert result["result"]["required_action"] == (
        "restore_linked_software_identity_or_issue_unlinked_token"
    )
    assert "http_correct_action" not in (result["error"].get("details") or {})
    assert "http_correct_action" not in (result["result"].get("details") or {})
