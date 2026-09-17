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


def test_default_credential_name_auto_renamed_to_real_agent_name(
    quiet_service, project_dir
):
    """默认兜底凭据名（Agent 凭据）在 Agent 接入后自动更名为其真实名称，且重名递增序号。"""
    service = quiet_service
    project = service.create_project(root_path=str(project_dir), name="AutoRename")

    # 1. 首次签发：不指定真实名字，默认名为「Agent 凭据」
    issued1 = service.issue_agent_token(project["id"], name="Agent 凭据")
    cred1_id = issued1["credential"]["id"]

    # 2. Agent 以真实名称 "Hermes" 接入，凭据应自动更名为 "Hermes 凭据"
    joined1 = service.join_room(
        project["id"],
        software_key="hermes-1",
        name="Hermes",
        client="hermes",
        model="unknown",
        credential_id=cred1_id,
    )
    assert joined1["member_created"] is True

    cred1 = next(c for c in service.list_agent_tokens(project["id"]) if c["id"] == cred1_id)
    assert cred1["name"] == "Hermes 凭据"
    assert cred1["member_id"] == joined1["agent"]["member_id"]

    # 3. 再次签发另一个默认名为「Agent 凭据」的 Token
    issued2 = service.issue_agent_token(project["id"], name="Agent 凭据")
    cred2_id = issued2["credential"]["id"]

    # 4. 同一 Agent (或已有成员) 再次接入，凭据自动更名为递增序号 "Hermes 凭据 2"
    joined2 = service.join_room(
        project["id"],
        software_key="hermes-1",
        name="Hermes",
        client="hermes",
        model="unknown",
        credential_id=cred2_id,
    )
    assert joined2["member_created"] is False  # 成员已存在

    cred2 = next(c for c in service.list_agent_tokens(project["id"]) if c["id"] == cred2_id)
    assert cred2["name"] == "Hermes 凭据 2"
    assert cred2["member_id"] == joined1["agent"]["member_id"]

    # 5. 显式自定义名称的凭据不应被更名
    issued3 = service.issue_agent_token(project["id"], name="Custom Managed Token")
    cred3_id = issued3["credential"]["id"]
    service.join_room(
        project["id"],
        software_key="hermes-1",
        name="Hermes",
        client="hermes",
        model="unknown",
        credential_id=cred3_id,
    )
    cred3 = next(c for c in service.list_agent_tokens(project["id"]) if c["id"] == cred3_id)
    assert cred3["name"] == "Custom Managed Token"
    assert cred3["member_id"] == joined1["agent"]["member_id"]

    # 6. 直接调用 link_credential_member 关联默认凭据名时，也应自动更名
    issued4 = service.issue_agent_token(project["id"], name="Agent 凭据")
    cred4_id = issued4["credential"]["id"]
    linked_res = service.link_credential_member(project["id"], cred4_id, joined1["agent"]["member_id"])
    assert linked_res["linked"] is True
    assert linked_res["credential"]["name"] == "Hermes 凭据 3"
