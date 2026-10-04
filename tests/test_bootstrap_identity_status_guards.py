"""#293 guard: identity and credential failures keep their own bootstrap status.

``bootstrap_local_room`` used to fold every non-session ``DomainError`` into
``room_unavailable`` with ``details`` reduced to ``{"code": ...}``. The
``software_identity_mismatch`` repair guidance was therefore discarded, and the
surviving status told the Agent to wait for a room that was never going to
appear. ``mcp_server.room_bootstrap`` compounded this by attaching
workspace-root advice to an identity failure.

These tests pin the fixed behaviour: dedicated statuses, the original
message/details/status_code, and no root advice for identity failures.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var

from agentchatroom import mcp_server
from agentchatroom.bootstrap import REQUIRED_ACTIONS, bootstrap_local_room
from agentchatroom.database import Database
from agentchatroom.project_registration import register_checkout_project
from agentchatroom.services import DEFAULT_AGENT_PERMISSIONS, AgentChatRoomService


@pytest.fixture()
def quiet_service(settings):
    """Short heartbeat so a stale Session cannot outlive the assertions."""
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def _boot(service, **extra):
    return bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        **extra,
    )


def _boot_as(service, software_key, **extra):
    """Bootstrap under a specific software identity."""
    return bootstrap_local_room(
        service,
        software_key=software_key,
        software_name=software_key.title(),
        client=software_key,
        model="unknown",
        **extra,
    )


def test_identity_mismatch_is_not_reported_as_room_unavailable(quiet_service, tmp_path):
    """AC1/AC2: a drifted software identity gets its own actionable status."""
    service = quiet_service
    root = tmp_path
    project = service.create_project(root_path=str(root), name="Drift")
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    issued = service.issue_agent_token(
        project["id"], name="Linked", member_id=member["id"]
    )
    credential_id = issued["credential"]["id"]
    service.register_workspace(
        project["id"],
        host_key="host:opencode",
        host_name="OpenCode",
        local_path=str(root),
    )

    first = _boot_as(
        service,
        "opencode",
        database_first=True,
        workspace_roots=[root],
        credential_id=credential_id,
    )
    assert first.binding is not None, "the first bootstrap must pin the credential"

    drifted = _boot_as(
        service,
        "impostor",
        database_first=True,
        workspace_roots=[root],
        credential_id=credential_id,
    )

    assert drifted.binding is None
    # Not the "wait for the room" bucket: that sends an Agent into a poll loop.
    assert drifted.public["status"] == "identity_mismatch"
    assert drifted.public["status"] != "room_unavailable"
    assert drifted.public["required_action"] == REQUIRED_ACTIONS["identity_mismatch"]
    assert (
        drifted.public["required_action"]
        == "restore_linked_software_identity_or_issue_unlinked_token"
    )
    # AC2: the operational repair guidance survives the translation.
    details = drifted.public["details"]
    assert details["code"] == "software_identity_mismatch"
    assert details["status_code"] == 403
    assert "pinned to a different software" in details["message"]


def test_credential_conflict_keeps_its_own_status(quiet_service, project_dir):
    """AC1/AC2: a credential pinned to another member is a conflict."""
    service = quiet_service
    project = service.create_project(root_path=str(project_dir), name="Conflict")
    register_checkout_project(project_dir, project)

    first = _boot(service, workspace_roots=[project_dir], cwd=project_dir)
    assert first.binding is not None

    other = service.create_project_member(
        project["id"], member_key="other-client", name="Other client"
    )["member"]
    credential = service.issue_agent_token(
        project["id"], name="Other member credential", member_id=other["id"]
    )["credential"]

    conflict = _boot(
        service,
        workspace_roots=[project_dir],
        cwd=project_dir,
        restore_binding=first.binding,
        credential_id=credential["id"],
    )

    assert conflict.binding is None
    assert conflict.public["status"] == "credential_conflict"
    assert conflict.public["status"] != "room_unavailable"
    assert conflict.public["required_action"] == REQUIRED_ACTIONS["credential_conflict"]
    details = conflict.public["details"]
    assert details["code"] == "credential_already_linked"
    assert details["status_code"] == 409
    assert details["message"]
    # The pin is left alone rather than silently re-pointed at the restoring member.
    assert service.list_agent_tokens(project["id"])[0]["member_id"] == other["id"]


def _mcp_room_bootstrap(service, project_dir, project_id, token, identity):
    """Drive ``mcp_server.room_bootstrap`` as the bound HTTP MCP transport would.

    ``_authorize_remote`` rejects a credential whose claims do not name the
    Project, so the access token needs the same shape the transport supplies:
    an ``access_token`` carrying ``project_id`` and ``credential_id``.
    """
    credential_id = identity[3]
    key, name, client = identity[:3]
    provider = mcp_server._bound_service_provider.set(lambda: service)
    roots = mcp_server._bootstrap_workspace_roots.set([project_dir])
    http_identity = mcp_server._bootstrap_http_identity.set((key, name, client))
    access = SimpleNamespace(
        access_token=SimpleNamespace(
            token=token,
            client_id="group-f-guard",
            scopes=sorted(DEFAULT_AGENT_PERMISSIONS),
            claims={
                "project_id": project_id,
                "credential_id": credential_id,
                "software_key": key,
                "software_name": name,
                "software_client": client,
            },
        )
    )
    auth = auth_context_var.set(access)
    try:
        return mcp_server.room_bootstrap()
    finally:
        # Bootstrap binds this process's runtime context; clear it so later
        # tests with explicit project ids do not trip runtime_context_mismatch.
        mcp_server.clear_runtime_binding()
        auth_context_var.reset(auth)
        mcp_server._bootstrap_http_identity.reset(http_identity)
        mcp_server._bootstrap_workspace_roots.reset(roots)
        mcp_server._bound_service_provider.reset(provider)


def test_identity_mismatch_omits_workspace_root_advice(service, tmp_path):
    """AC3: root advice is only meaningful for a workspace problem."""
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Pinned")
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    issued = service.issue_agent_token(
        project["id"], name="Linked", member_id=member["id"]
    )
    credential_id = issued["credential"]["id"]
    service.register_workspace(
        project["id"],
        host_key="host:opencode",
        host_name="OpenCode",
        local_path=str(project_dir),
    )

    # The first bootstrap pins the credential to this software identity.
    first = _mcp_room_bootstrap(
        service,
        project_dir,
        project["id"],
        issued["token"],
        ("opencode", "OpenCode", "opencode", credential_id),
    )
    assert first["ok"] is True, first

    drifted = _mcp_room_bootstrap(
        service,
        project_dir,
        project["id"],
        issued["token"],
        ("impostor", "Impostor", "impostor", credential_id),
    )

    assert drifted["ok"] is False
    assert drifted["result"]["status"] == "identity_mismatch"
    assert drifted["result"]["required_action"] == (
        "restore_linked_software_identity_or_issue_unlinked_token"
    )
    # Root advice would push the Agent to re-open folders instead of repairing
    # its identity, which is the actual problem.
    assert "http_correct_action" not in (drifted["error"].get("details") or {})
    assert "http_correct_action" not in (drifted["result"].get("details") or {})
