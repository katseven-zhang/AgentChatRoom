"""Tests for Task #165: Multi-project credential bundle ID extraction and placeholder renaming."""

from __future__ import annotations

import pytest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agentchatroom import bootstrap as bootstrap_module
from agentchatroom import mcp_server
from agentchatroom.services import DEFAULT_AGENT_PERMISSIONS, AgentChatRoomService
from agentchatroom.database import Database
from mcp.server.auth.middleware.auth_context import auth_context_var


@pytest.fixture()
def test_service(settings):
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def test_credential_bundle_extracts_id_and_renames(test_service, tmp_path):
    """In credential_bundle mode, room_bootstrap extracts selected_credential_id and renames placeholder token."""
    service = test_service
    project = service.create_project(root_path=str(tmp_path), name="BundleProject")
    issued = service.issue_agent_token(
        project["id"], name="BundleProject · 通用（标准 MCP） HTTP"
    )
    credential_id = issued["credential"]["id"]
    service.register_workspace(
        project["id"],
        host_key="host:bundle-agent",
        host_name="Bundle Agent",
        local_path=str(tmp_path),
    )

    provider = mcp_server._bound_service_provider.set(lambda: service)
    roots = mcp_server._bootstrap_workspace_roots.set([tmp_path])
    identity = mcp_server._bootstrap_http_identity.set(
        ("bundle-agent", "Bundle Agent", "bundle-agent")
    )
    access = SimpleNamespace(
        access_token=SimpleNamespace(
            token="bundle_token_placeholder",
            client_id="bundle-test",
            scopes=sorted(DEFAULT_AGENT_PERMISSIONS),
            claims={
                "credential_bundle": True,
                "project_credentials": {
                    project["id"]: {
                        "name": "BundleProject",
                        "token": issued["token"],
                        "credential_id": credential_id,
                    }
                },
                "software_key": "bundle-agent",
                "software_name": "Bundle Agent",
                "software_client": "bundle-agent",
            },
        )
    )
    auth = auth_context_var.set(access)
    try:
        res = mcp_server.room_bootstrap(project_name="BundleProject")
        assert res["ok"] is True, res

        tokens = service.list_agent_tokens(project["id"])
        tok = next(t for t in tokens if t["id"] == credential_id)
        assert tok["name"] == "Bundle Agent 凭据"
        assert tok["member_id"] is not None
    finally:
        mcp_server.clear_runtime_binding()
        auth_context_var.reset(auth)
        mcp_server._bootstrap_http_identity.reset(identity)
        mcp_server._bootstrap_workspace_roots.reset(roots)
        mcp_server._bound_service_provider.reset(provider)


@pytest.mark.parametrize(
    "placeholder_name",
    [
        "Agent 凭据",
        "Agent Token",
        "Agent 凭据 2",
        "agentchatroom · 通用（标准 MCP） HTTP",
        "my_proj · 通用（标准 MCP） HTTP",
        "通用（标准 MCP）",
        "Standard MCP",
    ],
)
def test_default_credential_name_patterns(test_service, tmp_path, placeholder_name):
    """Various placeholder formats are correctly recognized and renamed on join_room."""
    service = test_service
    project = service.create_project(root_path=str(tmp_path), name="PatternProj")
    issued = service.issue_agent_token(project["id"], name=placeholder_name)
    cid = issued["credential"]["id"]

    joined = service.join_room(
        project["id"],
        software_key="pattern-agent",
        name="Pattern Agent",
        client="test",
        model="unknown",
        credential_id=cid,
    )
    tokens = service.list_agent_tokens(project["id"])
    tok = next(t for t in tokens if t["id"] == cid)
    assert tok["name"] == "Pattern Agent 凭据"
    assert tok["member_id"] == joined["agent"]["member_id"]


def test_already_linked_placeholder_credential_heals_name(test_service, tmp_path):
    """When a credential was previously linked but kept a placeholder name, re-linking heals it."""
    service = test_service
    project = service.create_project(root_path=str(tmp_path), name="HealProj")
    member = service.create_project_member(
        project["id"], member_key="heal-bot", name="Heal Bot"
    )["member"]
    issued = service.issue_agent_token(
        project["id"],
        name="HealProj · 通用（标准 MCP） HTTP",
        member_id=member["id"],
    )
    cid = issued["credential"]["id"]

    res = service.link_credential_member(project["id"], cid, member["id"])
    assert res["linked"] is True
    assert res["credential"]["name"] == "Heal Bot 凭据"


@pytest.mark.parametrize(
    "custom_name",
    [
        "Alice · Custom HTTP",
        "QA · 私有生产 HTTP",
        "my_proj · Custom HTTP",
        "自定义 · 直接 HTTP MCP",
    ],
)
def test_custom_credential_names_are_never_renamed(test_service, tmp_path, custom_name):
    """#165 退回回归：非占位的自定义凭据名必须原样保留，join/link 不得静默改名。

    旧模式 `.+? · .*HTTP` 把任意「X · Y HTTP」都判成占位名；收紧后只有含
    向导明确标记（通用（标准 MCP）/Standard MCP）或 Agent 凭据/Token 系列
    的名称才会被重命名。
    """
    service = test_service
    project = service.create_project(root_path=str(tmp_path), name="KeepNameProj")
    issued = service.issue_agent_token(project["id"], name=custom_name)
    cid = issued["credential"]["id"]

    joined = service.join_room(
        project["id"],
        software_key="keep-name-agent",
        name="Keep Name Agent",
        client="test",
        model="unknown",
        credential_id=cid,
    )
    tokens = service.list_agent_tokens(project["id"])
    tok = next(t for t in tokens if t["id"] == cid)
    assert tok["name"] == custom_name
    assert joined["agent"]["name"] == "Keep Name Agent"


def test_custom_credential_name_survives_member_link(test_service, tmp_path):
    """#165 退回回归：link_credential_member 对自定义名同样不重命名。"""
    service = test_service
    project = service.create_project(root_path=str(tmp_path), name="LinkKeepProj")
    member = service.create_project_member(
        project["id"], member_key="link-keep-bot", name="Link Keep Bot"
    )["member"]
    issued = service.issue_agent_token(project["id"], name="QA · 私有生产 HTTP")
    cid = issued["credential"]["id"]

    res = service.link_credential_member(project["id"], cid, member["id"])
    assert res["linked"] is True
    assert res["credential"]["name"] == "QA · 私有生产 HTTP"
    assert res["credential"]["member_id"] == member["id"]
