"""Review batch: identity drift pinning for unlinked credentials."""

from __future__ import annotations

import pytest
from dataclasses import replace

from agentchatroom import bootstrap as bootstrap_module
from agentchatroom import mcp_server
from agentchatroom.services import DEFAULT_AGENT_PERMISSIONS, AgentChatRoomService
from agentchatroom.database import Database
from types import SimpleNamespace
from mcp.server.auth.middleware.auth_context import auth_context_var


@pytest.fixture()
def quiet_service(settings):
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def test_unlinked_credential_pins_identity_on_first_bootstrap(
    quiet_service, project_dir
):
    """首次 bootstrap 按头钉定身份；漂移身份被拒；原身份不受影响；事件只追加一次。"""
    service = quiet_service
    project = service.create_project(root_path=str(project_dir), name="Pinning")
    issued = service.issue_agent_token(project["id"], name="Pinned")
    credential_id = issued["credential"]["id"]
    service.register_workspace(
        project["id"],
        host_key="host:pinned-http",
        host_name="Pinned HTTP",
        local_path=str(project_dir),
    )

    def with_identity(key, name, fn):
        provider = mcp_server._bound_service_provider.set(lambda: service)
        roots = mcp_server._bootstrap_workspace_roots.set([project_dir])
        identity = mcp_server._bootstrap_http_identity.set((key, name, key))
        access = SimpleNamespace(
            access_token=SimpleNamespace(
                token=issued["token"],
                client_id="pinning-test",
                scopes=sorted(DEFAULT_AGENT_PERMISSIONS),
                claims={
                    "project_id": project["id"],
                    "credential_id": credential_id,
                    "software_key": key,
                    "software_name": name,
                    "software_client": key,
                },
            )
        )
        auth = auth_context_var.set(access)
        try:
            return fn()
        finally:
            # The bootstrap binds this process's runtime context to the test
            # project; clear it so later tests with explicit project ids don't
            # trip runtime_context_mismatch.
            mcp_server.clear_runtime_binding()
            auth_context_var.reset(auth)
            mcp_server._bootstrap_http_identity.reset(identity)
            mcp_server._bootstrap_workspace_roots.reset(roots)
            mcp_server._bound_service_provider.reset(provider)

    # First bootstrap as identity A pins the unlinked credential to member A.
    first = with_identity("pinned-http", "Pinned HTTP", mcp_server.room_bootstrap)
    assert first["ok"] is True, first

    credentials = service.list_agent_tokens(project["id"])
    cred_row = next(c for c in credentials if c["id"] == credential_id)
    assert cred_row["member_id"], "credential must be pinned on first bootstrap"
    members = service.list_project_members(project["id"])
    pinned_member = next(
        m for m in members if m["id"] == cred_row["member_id"]
    )
    assert pinned_member["name"] == "Pinned HTTP"

    # Drift: the same credential with a different identity is rejected.
    drifted = with_identity("impostor-http", "Impostor HTTP", mcp_server.room_bootstrap)
    assert drifted["ok"] is False
    details = (drifted.get("result") or {}).get("details") or {}
    assert details.get("code") == "software_identity_mismatch", drifted

    # The pinned identity keeps working.
    again = with_identity("pinned-http", "Pinned HTTP", mcp_server.room_bootstrap)
    assert again["ok"] is True, again

    # Append-only: exactly one credential-linked event records the pinning.
    linked_events = [
        e
        for e in service.list_events(project["id"], after=0)["events"]
        if e["event_type"] == "agent.credential_linked"
    ]
    assert len(linked_events) == 1


def test_bootstrap_local_room_rejects_drifted_identity_for_linked_credential(
    quiet_service, project_dir, tmp_path
):
    """服务层：已钉定凭据用不同 software 身份再 bootstrap → software_identity_mismatch。"""
    service = quiet_service
    root = tmp_path
    project = service.create_project(root_path=str(root), name="Drift")
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    issued = service.issue_agent_token(
        project["id"], name="Linked", member_id=member["id"]
    )
    credential = {"id": issued["credential"]["id"], "token": issued["token"]}
    service.register_workspace(
        project["id"],
        host_key="host:opencode",
        host_name="OpenCode",
        local_path=str(root),
    )

    outcome = bootstrap_module.bootstrap_local_room(
        service,
        database_first=True,
        software_key="opencode",
        software_name="OpenCode",
        client="opencode",
        model="unknown",
        workspace_roots=[root],
        credential_id=credential["id"],
    )
    assert outcome.binding is not None

    drifted = bootstrap_module.bootstrap_local_room(
        service,
        database_first=True,
        software_key="impostor",
        software_name="Impostor",
        client="impostor",
        model="unknown",
        workspace_roots=[root],
        credential_id=credential["id"],
    )
    assert drifted.binding is None
    details = (drifted.public.get("details") or {}).get("code")
    assert details == "software_identity_mismatch"
