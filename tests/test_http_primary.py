from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentchatroom import mcp_server
from agentchatroom.api import create_app
from agentchatroom.bootstrap import (
    bootstrap_local_room,
    match_registered_projects,
)
from agentchatroom.credential_bundle import (
    CredentialBundleError,
    decode_project_credential_bundle,
    encode_project_credential_bundle,
)
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import register_checkout_project


def _clear_http_mcp_context() -> None:
    mcp_server.clear_runtime_binding()
    mcp_server._bound_service_provider.set(None)
    mcp_server._bootstrap_workspace_roots.set(None)
    mcp_server._bootstrap_http_identity.set(None)
    try:
        from mcp.server.auth.middleware.auth_context import auth_context_var

        auth_context_var.set(None)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_http_mcp_context():
    _clear_http_mcp_context()
    yield
    _clear_http_mcp_context()


def _boot(service, *, key, name, client, **kwargs):
    kwargs.setdefault("database_first", True)
    return bootstrap_local_room(
        service,
        software_key=key,
        software_name=name,
        client=client,
        model="unknown",
        **kwargs,
    )


def test_http_identity_recovers_legacy_utf8_header_bytes():
    name = "通用（标准 MCP）"
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(
                headers={
                    mcp_server.SOFTWARE_KEY_HEADER: "standard-mcp-installation",
                    mcp_server.SOFTWARE_NAME_HEADER: name.encode("utf-8").decode("latin-1"),
                    mcp_server.SOFTWARE_CLIENT_HEADER: "standard-mcp",
                }
            )
        )
    )

    assert mcp_server._software_identity_from_http(context) == (
        "standard-mcp-installation",
        name,
        "standard-mcp",
    )


def test_db_root_match_does_not_need_checkout_file(service, project_dir):
    project = service.create_project(root_path=str(project_dir), name="HTTP Room")
    assert not (project_dir / ".agentchatroom" / "project.json").exists()
    matched = match_registered_projects(service, [project_dir])
    assert len(matched) == 1
    assert matched[0][0]["id"] == project["id"]
    outcome = _boot(
        service,
        key="http-agent",
        name="HTTP Agent",
        client="http-client",
        workspace_roots=[project_dir],
        cwd=None,
        explicit_project_path=None,
    )
    assert outcome.binding is not None
    assert outcome.binding.project_id == project["id"]
    assert outcome.public["status"] == "ready"


def test_db_match_succeeds_when_client_path_is_absent_on_server(service, tmp_path):
    real = tmp_path / "server-copy"
    real.mkdir()
    project = service.create_project(root_path=str(real), name="Cloud Room")
    ghost = tmp_path / "client-machine" / "workspace"
    assert not ghost.exists()
    service.register_workspace(
        project["id"],
        host_key="host:cloud-client",
        host_name="Cloud Client",
        local_path=str(ghost),
    )
    outcome = _boot(
        service,
        key="cloud-agent",
        name="Cloud Agent",
        client="http-client",
        workspace_roots=[ghost],
        cwd=None,
        explicit_project_path=None,
    )
    assert outcome.binding is not None
    assert outcome.binding.project_id == project["id"]
    assert outcome.public["project"]["id"] == project["id"]


def test_unregistered_roots_return_required_action(service, tmp_path):
    missing = tmp_path / "never-registered"
    missing.mkdir()
    outcome = _boot(
        service,
        key="lost-agent",
        name="Lost Agent",
        client="http-client",
        workspace_roots=[missing],
        cwd=None,
        explicit_project_path=None,
    )
    assert outcome.binding is None
    assert outcome.public["status"] == "project_not_registered"
    assert outcome.public["required_action"] == "create_or_open_project_in_web"


def test_bundle_project_name_must_match_advertised_workspace_roots(service, tmp_path):
    root_a = tmp_path / "bundle-room-a"
    root_b = tmp_path / "bundle-room-b"
    root_a.mkdir()
    root_b.mkdir()
    project_a = service.create_project(root_path=str(root_a), name="Bundle A")
    project_b = service.create_project(root_path=str(root_b), name="Bundle B")

    mismatched = _boot(
        service,
        key="bundle-agent",
        name="Bundle Agent",
        client="bundle-client",
        workspace_roots=[root_a],
        cwd=None,
        selected_project_id=project_b["id"],
    )
    assert mismatched.binding is None
    assert mismatched.public["status"] == "project_workspace_mismatch"
    assert mismatched.public["required_action"] == (
        "open_matching_workspace_or_select_matching_project"
    )
    assert mismatched.public["details"] == {
        "selected_project": "Bundle B",
        "workspace_matches_selected_project": False,
    }
    assert service.snapshot(project_b["id"])["agents"] == []

    matched = _boot(
        service,
        key="bundle-agent",
        name="Bundle Agent",
        client="bundle-client",
        workspace_roots=[root_b],
        cwd=None,
        selected_project_id=project_b["id"],
    )
    assert matched.binding is not None
    assert matched.binding.project_id == project_b["id"]
    assert service.list_workspaces(project_b["id"])[0]["local_path"] == str(
        root_b.resolve()
    )

    no_roots = _boot(
        service,
        key="bundle-no-roots",
        name="Bundle Without Roots",
        client="bundle-client",
        workspace_roots=[],
        cwd=None,
        selected_project_id=project_a["id"],
    )
    assert no_roots.binding is not None
    assert no_roots.binding.project_id == project_a["id"]
    workspace = service.list_workspaces(project_a["id"])[0]
    assert workspace["local_path"] == str(root_a.resolve())
    assert no_roots.public["notices"][0]["code"] == "server_project_root_registered"


def test_bundle_without_roots_fails_when_server_project_root_is_unavailable(
    service, tmp_path
):
    root = tmp_path / "removed-root"
    root.mkdir()
    project = service.create_project(root_path=str(root), name="Removed Root")
    root.rmdir()

    outcome = _boot(
        service,
        key="bundle-agent",
        name="Bundle Agent",
        client="bundle-client",
        workspace_roots=[],
        cwd=None,
        selected_project_id=project["id"],
    )

    assert outcome.binding is None
    assert outcome.public["status"] == "project_workspace_mismatch"
    assert outcome.public["details"]["server_project_root_available"] is False
    assert service.snapshot(project["id"])["agents"] == []


def test_dual_room_writes_do_not_leak(service, tmp_path):
    room_a = tmp_path / "room-a"
    room_b = tmp_path / "room-b"
    room_a.mkdir()
    room_b.mkdir()
    project_a = service.create_project(root_path=str(room_a), name="Room A")
    project_b = service.create_project(root_path=str(room_b), name="Room B")
    boot_a = _boot(
        service,
        key="agent-a",
        name="Agent A",
        client="client-a",
        workspace_roots=[room_a],
        cwd=None,
    )
    boot_b = _boot(
        service,
        key="agent-b",
        name="Agent B",
        client="client-b",
        workspace_roots=[room_b],
        cwd=None,
    )
    assert boot_a.binding is not None and boot_b.binding is not None
    assert boot_a.binding.project_id == project_a["id"]
    assert boot_b.binding.project_id == project_b["id"]

    posted = service.post_message(
        project_a["id"],
        body="secret-from-room-a",
        session_id=boot_a.binding.session_id,
        token=boot_a.binding.token,
        model_display_name="test-model",
    )
    task = service.create_task(
        project_a["id"],
        title="only-in-a",
        acceptance_criteria=["done"],
        actor_session_id=boot_a.binding.session_id,
        token=boot_a.binding.token,
    )
    lease = service.acquire_lease(
        project_a["id"],
        session_id=boot_a.binding.session_id,
        token=boot_a.binding.token,
        path_pattern="src/secret.py",
        reason="room-a-only",
    )
    snapshot_b = service.snapshot(project_b["id"])
    encoded_b = json.dumps(snapshot_b, ensure_ascii=False)
    assert "secret-from-room-a" not in encoded_b
    assert task["task"]["id"] not in {
        item["id"] for item in snapshot_b.get("tasks") or []
    }
    assert lease["lease"]["id"] not in {
        item["id"] for item in snapshot_b.get("leases") or []
    }
    events_b = service.list_events(project_b["id"])
    event_blob = json.dumps(events_b, ensure_ascii=False)
    assert "secret-from-room-a" not in event_blob
    assert posted["event_id"] not in {event["id"] for event in events_b["events"]}


def test_http_bound_unregistered_roots_include_http_action(monkeypatch, service, tmp_path):
    missing = tmp_path / "plain"
    missing.mkdir()
    provider = mcp_server._bound_service_provider.set(lambda: service)
    roots = mcp_server._bootstrap_workspace_roots.set([missing])
    identity = mcp_server._bootstrap_http_identity.set(
        ("http-agent", "HTTP Agent", "http-client")
    )
    monkeypatch.setattr(mcp_server, "service", service)
    try:
        result = mcp_server.room_bootstrap()
    finally:
        mcp_server._bound_service_provider.reset(provider)
        mcp_server._bootstrap_workspace_roots.reset(roots)
        mcp_server._bootstrap_http_identity.reset(identity)
    assert result["ok"] is False
    assert result["result"]["status"] == "project_not_registered"
    assert result["result"]["required_action"] == "create_or_open_project_in_web"
    assert "http_correct_action" in (result["error"].get("details") or {})
    assert "Web UI" in result["error"]["details"]["http_correct_action"]


def test_stdio_roots_still_outrank_cwd_and_env(service, project_dir, tmp_path, monkeypatch):
    register_checkout_project(
        project_dir, service.create_project(root_path=str(project_dir), name="Pinned")
    )
    other = tmp_path / "other"
    other.mkdir()
    other_project = service.create_project(root_path=str(other), name="Other")
    register_checkout_project(other, other_project)
    selected = _boot(
        service,
        key="stdio-agent",
        name="Stdio Agent",
        client="codex",
        workspace_roots=[other],
        cwd=project_dir,
        explicit_project_path=project_dir,
        database_first=False,
    )
    assert selected.binding is not None
    assert selected.binding.project_id == other_project["id"]


def test_http_duplicate_workspace_path_is_ambiguous(service, tmp_path):
    shared = tmp_path / "client-workspace"
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        project = service.create_project(root_path=str(root), name=name)
        service.register_workspace(project["id"], host_key=name, host_name=name,
                                   local_path=str(shared))
    result = _boot(service, key="review", name="Review", client="test",
                   workspace_roots=[shared])
    assert result.binding is None
    assert result.public["status"] == "ambiguous_workspace"


def test_stdio_does_not_bypass_corrupt_registration_via_database(service, project_dir):
    project = service.create_project(root_path=str(project_dir), name="Local")
    register_checkout_project(project_dir, project)
    (project_dir / ".agentchatroom" / "project.json").write_text("{broken", encoding="utf-8")
    result = _boot(service, key="review", name="Review", client="test",
                   workspace_roots=[project_dir], database_first=False)
    assert result.binding is None
    assert result.public["status"] == "registration_invalid"


def test_http_mixed_registered_and_unknown_roots_fail_closed(service, project_dir, tmp_path):
    service.create_project(root_path=str(project_dir), name="Known")
    result = _boot(service, key="review", name="Review", client="test",
                   workspace_roots=[project_dir, tmp_path / "unknown"])
    assert result.binding is None
    assert result.public["status"] == "ambiguous_workspace"


def _parse_tool(result) -> dict:
    for item in result.content:
        if isinstance(item, types.TextContent):
            return json.loads(item.text)
    raise AssertionError("tool result had no text payload")


def test_project_credential_bundle_round_trip_and_validation():
    entries = [
        {"name": "Project A", "token": "acr.credential_a.secret_a"},
        {"name": "项目 B", "token": "acr.credential_b.secret_b"},
    ]
    bundle = encode_project_credential_bundle(entries)
    assert bundle.startswith("acrb.v1.")
    assert decode_project_credential_bundle(bundle) == entries
    with pytest.raises(CredentialBundleError):
        encode_project_credential_bundle([entries[0], {**entries[1], "name": "project a"}])
    with pytest.raises(CredentialBundleError):
        decode_project_credential_bundle(bundle + "=")


def test_http_identity_rejects_headers_that_conflict_with_linked_token(monkeypatch):
    access = mcp_server.AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=["room:join"],
        claims={
            "software_key": "linked-key",
            "software_name": "Linked Agent",
            "software_client": "linked-client",
        },
    )
    monkeypatch.setattr(mcp_server, "get_access_token", lambda: access)
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(
                headers={
                    mcp_server.SOFTWARE_KEY_HEADER: "different-key",
                    mcp_server.SOFTWARE_NAME_HEADER: "Different Agent",
                    mcp_server.SOFTWARE_CLIENT_HEADER: "different-client",
                }
            )
        )
    )

    with pytest.raises(DomainError) as mismatch:
        mcp_server._software_identity_from_http(context)

    assert mismatch.value.code == "software_identity_mismatch"
    assert mismatch.value.status_code == 403
    assert mismatch.value.details["required_action"] == (
        "restore_linked_software_identity_or_issue_unlinked_token"
    )


@pytest.mark.asyncio
async def test_bundle_reports_configured_names_and_inactive_project_tokens(
    service, tmp_path
):
    issued = {}
    entries = []
    for label in ("Project A", "Project B", "Project C"):
        root = tmp_path / label.replace(" ", "-").lower()
        root.mkdir()
        project = service.create_project(root_path=str(root), name=label)
        credential = service.issue_agent_token(project["id"], name=f"{label} HTTP")
        issued[label] = credential
        entries.append({"name": label, "token": credential["token"]})
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_credentials SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", issued["Project A"]["credential"]["id"]),
        )
    service.revoke_agent_token(
        issued["Project B"]["credential"]["project_id"],
        issued["Project B"]["credential"]["id"],
    )

    access = await mcp_server.AgentCredentialTokenVerifier(service).verify_token(
        encode_project_credential_bundle(entries)
    )
    assert access is not None
    assert sorted(access.claims["unavailable_project_credentials"]) == [
        "Project A",
        "Project B",
    ]

    with pytest.raises(DomainError) as missing_name:
        mcp_server._bundle_project_for_name(access, "")
    assert missing_name.value.code == "project_credential_name_required"
    assert missing_name.value.details["available_project_names"] == ["Project C"]
    assert missing_name.value.details["configured_project_names"] == [
        "Project A",
        "Project B",
        "Project C",
    ]

    with pytest.raises(DomainError) as expired:
        mcp_server._bundle_project_for_name(access, "Project A")
    assert expired.value.code == "project_credential_expired"
    assert expired.value.details["required_action"] == "renew_project_token"

    with pytest.raises(DomainError) as revoked:
        mcp_server._bundle_project_for_name(access, "Project B")
    assert revoked.value.code == "project_credential_revoked"
    assert revoked.value.details["required_action"] == (
        "issue_new_project_token_and_update_bundle"
    )

    inactive_only = await mcp_server.AgentCredentialTokenVerifier(
        service
    ).verify_token(encode_project_credential_bundle(entries[:2]))
    assert inactive_only is not None
    assert inactive_only.expires_at is None
    assert inactive_only.scopes == []


@pytest.mark.asyncio
async def test_optional_bundle_roots_are_used_when_client_advertises_them(tmp_path):
    advertised = tmp_path / "advertised-root"
    advertised.mkdir()

    class RootsSession:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(roots=object())
        )

        async def list_roots(self):
            return types.ListRootsResult(
                roots=[types.Root(uri=advertised.resolve().as_uri())]
            )

    roots = await mcp_server.collect_mcp_workspace_roots(
        SimpleNamespace(session=RootsSession()), required=False
    )
    assert roots == [advertised.resolve()]

    no_roots_session = SimpleNamespace(
        client_params=SimpleNamespace(
            capabilities=SimpleNamespace(roots=None)
        ),
        list_roots=lambda: pytest.fail("roots/list must not be called"),
    )
    assert await mcp_server.collect_mcp_workspace_roots(
        SimpleNamespace(session=no_roots_session), required=False
    ) == []


@pytest.mark.asyncio
async def test_bundle_rejects_tokens_from_different_software_identities(
    service, tmp_path
):
    entries = []
    for index, software_key in enumerate(("software-a", "software-b"), start=1):
        root = tmp_path / f"identity-{index}"
        root.mkdir()
        project = service.create_project(root_path=str(root), name=f"Identity {index}")
        member = service.create_project_member(
            project["id"],
            member_key=f"software:{software_key}",
            name=f"Software {index}",
            kind="software_agent",
            role="executor",
            metadata={"software_key": software_key, "client": software_key},
        )["member"]
        issued = service.issue_agent_token(
            project["id"], name=f"Identity {index}", member_id=member["id"]
        )
        entries.append({"name": f"Identity {index}", "token": issued["token"]})

    access = await mcp_server.AgentCredentialTokenVerifier(service).verify_token(
        encode_project_credential_bundle(entries)
    )
    assert access is None


@pytest.mark.asyncio
async def test_bundle_uses_its_single_linked_identity_for_unlinked_project_token(
    service, tmp_path
):
    linked_root = tmp_path / "linked-identity"
    unlinked_root = tmp_path / "unlinked-identity"
    linked_root.mkdir()
    unlinked_root.mkdir()
    linked_project = service.create_project(root_path=str(linked_root), name="Linked")
    unlinked_project = service.create_project(root_path=str(unlinked_root), name="Unlinked")
    member = service.create_project_member(
        linked_project["id"],
        member_key="software:shared-software",
        name="Shared Software",
        kind="software_agent",
        role="executor",
        metadata={"software_key": "shared-software", "client": "shared-client"},
    )["member"]
    linked = service.issue_agent_token(
        linked_project["id"], name="Linked", member_id=member["id"]
    )
    unlinked = service.issue_agent_token(unlinked_project["id"], name="Unlinked")

    access = await mcp_server.AgentCredentialTokenVerifier(service).verify_token(
        encode_project_credential_bundle(
            [
                {"name": "Linked", "token": linked["token"]},
                {"name": "Unlinked", "token": unlinked["token"]},
            ]
        )
    )

    assert access is not None
    assert access.claims["software_key"] == "shared-software"
    assert access.claims["software_name"] == "Shared Software"
    assert access.claims["software_client"] == "shared-client"


@pytest.mark.asyncio
async def test_streamable_http_room_bootstrap_with_token_and_rejects_invalid(
    settings, project_dir
):
    app = create_app(settings)
    project = app.state.service.create_project(root_path=str(project_dir), name="HTTP Primary")
    credential = app.state.service.issue_agent_token(
        project["id"], name="HTTP bootstrap"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    url = f"http://127.0.0.1:{port}{settings.mcp_http_path}"

    async def list_roots(_context):
        return types.ListRootsResult(
            roots=[types.Root(uri=Path(project_dir).resolve().as_uri(), name="project")]
        )

    headers = {
        "Authorization": f"Bearer {credential['token']}",
        "X-AgentChatRoom-Software-Key": "http-bootstrap",
        "X-AgentChatRoom-Software-Name": "HTTP Bootstrap",
        "X-AgentChatRoom-Software-Client": "http-bootstrap",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_client:
            unauthorized = await http_client.post(
                url,
                headers={
                    "Authorization": "Bearer <invalid-test-credential>",
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "no-token", "version": "1"},
                    },
                },
            )
            assert unauthorized.status_code == 401

            async with streamable_http_client(url, http_client=http_client) as (no_root_r, no_root_w, _):
                async with ClientSession(no_root_r, no_root_w) as no_roots:
                    await no_roots.initialize()
                    refused = _parse_tool(await no_roots.call_tool("room_bootstrap", {}))
                    assert not refused["ok"]
                    assert refused["error"]["code"] == "workspace_roots_unavailable"

            async with streamable_http_client(url, http_client=http_client) as (
                read_stream,
                write_stream,
                _session_id,
            ):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    list_roots_callback=list_roots,
                    client_info=types.Implementation(name="http-bootstrap", version="1"),
                ) as session:
                    await session.initialize()
                    payload = _parse_tool(await session.call_tool("room_bootstrap", {}))
                    assert payload["ok"] is True
                    assert payload["result"]["status"] == "ready"
                    assert payload["result"]["project"]["id"] == project["id"]
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    assert "room_bootstrap" in names
                    assert "room_sync" in names
                    other_dir = Path(project_dir).parent / "other-http"
                    other_dir.mkdir()
                    other = app.state.service.create_project(root_path=str(other_dir), name="Other HTTP")
                    other_credential = app.state.service.issue_agent_token(other["id"], name="Other")

                    async def other_roots(_context):
                        return types.ListRootsResult(roots=[types.Root(uri=other_dir.as_uri())])

                    other_headers = {**headers, "Authorization": f"Bearer {other_credential['token']}"}
                    async with httpx.AsyncClient(headers=other_headers, timeout=10.0) as second_http:
                        async with streamable_http_client(url, http_client=second_http) as (r2, w2, _):
                            async with ClientSession(r2, w2, list_roots_callback=other_roots) as second:
                                await second.initialize()
                                boot_b = _parse_tool(await second.call_tool("room_bootstrap", {}))
                                assert boot_b["ok"]
                                assert boot_b["result"]["project"]["id"] == other["id"]
                                for client, own, forbidden, marker in (
                                    (session, project, other, "wire-room-a"),
                                    (second, other, project, "wire-room-b"),
                                ):
                                    for tool, arguments in (
                                        ("message_post", {"body": marker, "model_display_name": "test"}),
                                        ("task_create", {"title": marker, "acceptance_criteria": ["done"]}),
                                        ("lease_acquire", {"path_pattern": marker + ".py", "reason": marker}),
                                    ):
                                        posted = _parse_tool(await client.call_tool(tool, arguments))
                                        assert posted["ok"], posted
                                        rejected = _parse_tool(await client.call_tool(tool, {**arguments, "project_id": forbidden["id"]}))
                                        assert not rejected["ok"], rejected
                                    assert marker in json.dumps(app.state.service.list_events(own["id"]))
                                    assert marker not in json.dumps(app.state.service.list_events(forbidden["id"]))
                                # Wrong-project roots invalidate this connection, and
                                # cannot leave its old runtime binding usable.
                                async def wrong_roots(_context):
                                    return types.ListRootsResult(roots=[types.Root(uri=other_dir.as_uri())])
                                session._list_roots_callback = wrong_roots
                                failed = _parse_tool(await session.call_tool("room_bootstrap", {}))
                                assert not failed["ok"]
                                denied = _parse_tool(await session.call_tool("message_post", {"body": "must-not-write", "model_display_name": "test"}))
                                assert not denied["ok"]
                                alive = _parse_tool(await second.call_tool("room_sync", {}))
                                assert alive["ok"]
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_http_transport_presence_keeps_idle_session_online_and_closes_on_disconnect(
    settings, project_dir
):
    tuned = replace(
        settings,
        heartbeat_timeout_seconds=1,
        presence_keepalive_interval_seconds=0.05,
        mcp_http_session_idle_timeout_seconds=0.5,
    )
    app = create_app(tuned)
    assert app.state.mcp_server.session_manager.session_idle_timeout == 0.5
    project = app.state.service.create_project(
        root_path=str(project_dir), name="HTTP Presence"
    )
    credential = app.state.service.issue_agent_token(
        project["id"], name="HTTP Presence"
    )
    bundle = encode_project_credential_bundle(
        [{"name": project["name"], "token": credential["token"]}]
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    url = f"http://127.0.0.1:{port}{tuned.mcp_http_path}"
    headers = {
        "Authorization": f"Bearer {bundle}",
        "X-AgentChatRoom-Software-Key": "http-presence",
        "X-AgentChatRoom-Software-Name": "HTTP Presence",
        "X-AgentChatRoom-Software-Client": "http-presence",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    payload = _parse_tool(
                        await session.call_tool(
                            "room_bootstrap", {"project_name": project["name"]}
                        )
                    )
                    assert payload["ok"], payload
                    session_id = payload["result"]["session"]["id"]
                    with app.state.service.database.connect(write=True) as connection:
                        connection.execute(
                            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
                            ("1970-01-01T00:00:00Z", session_id),
                        )
                    await asyncio.sleep(0.2)
                    agent = next(
                        item for item in app.state.service.snapshot(project["id"])["agents"]
                        if item["id"] == session_id
                    )
                    assert agent["status"] == "online"

            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                agent = next(
                    item for item in app.state.service.snapshot(project["id"])["agents"]
                    if item["id"] == session_id
                )
                if agent["status"] == "offline":
                    break
                await asyncio.sleep(0.05)
            assert agent["status"] == "offline"
    finally:
        server.should_exit = True
        await server_task
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_abandoned_http_transport_is_reaped_and_stops_presence(
    settings, project_dir
):
    tuned = replace(
        settings,
        heartbeat_timeout_seconds=1,
        presence_keepalive_interval_seconds=0.05,
        mcp_http_session_idle_timeout_seconds=0.3,
    )
    app = create_app(tuned)
    project = app.state.service.create_project(
        root_path=str(project_dir), name="Abandoned HTTP Presence"
    )
    credential = app.state.service.issue_agent_token(
        project["id"], name="Abandoned HTTP Presence"
    )
    bundle = encode_project_credential_bundle(
        [{"name": project["name"], "token": credential["token"]}]
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    url = f"http://127.0.0.1:{port}{tuned.mcp_http_path}"
    headers = {
        "Authorization": f"Bearer {bundle}",
        "X-AgentChatRoom-Software-Key": "abandoned-http",
        "X-AgentChatRoom-Software-Name": "Abandoned HTTP",
        "X-AgentChatRoom-Software-Client": "test-http",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            initialized = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "abandoning-client", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text
            transport_session_id = initialized.headers["mcp-session-id"]
            transport_headers = {"mcp-session-id": transport_session_id}
            notified = await client.post(
                url,
                headers=transport_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notified.status_code == 202, notified.text
            booted = await client.post(
                url,
                headers=transport_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "room_bootstrap",
                        "arguments": {"project_name": project["name"]},
                    },
                },
            )
            assert booted.status_code == 200, booted.text
            tool_payload = json.loads(booted.json()["result"]["content"][0]["text"])
            assert tool_payload["ok"], tool_payload
            room_session_id = tool_payload["result"]["session"]["id"]
            # Intentionally omit the MCP DELETE request used by compliant clients.

        deadline = asyncio.get_running_loop().time() + 3
        agent = None
        while asyncio.get_running_loop().time() < deadline:
            agent = next(
                item
                for item in app.state.service.snapshot(project["id"])["agents"]
                if item["id"] == room_session_id
            )
            if agent["status"] == "offline":
                break
            await asyncio.sleep(0.05)
        assert agent is not None
        assert agent["status"] == "offline"
        assert mcp_server.get_runtime_binding(
            f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{transport_session_id}"
        ) is None
    finally:
        server.should_exit = True
        await server_task
        _clear_http_mcp_context()


@pytest.mark.asyncio
async def test_one_http_mcp_credential_bundle_keeps_parallel_projects_isolated(
    settings, tmp_path
):
    app = create_app(settings)
    roots: dict[str, Path] = {}
    projects: dict[str, dict] = {}
    issued_credentials: dict[str, dict] = {}
    credentials: list[dict[str, str]] = []
    for label in ("Project A", "Project B"):
        root = tmp_path / label.replace(" ", "-").lower()
        root.mkdir()
        project = app.state.service.create_project(root_path=str(root), name=label)
        issued = app.state.service.issue_agent_token(project["id"], name=f"{label} HTTP")
        roots[label] = root
        projects[label] = project
        credentials.append({"name": label, "token": issued["token"]})
        issued_credentials[label] = issued
        assert app.state.service.list_project_members(project["id"]) == []
    bundle = encode_project_credential_bundle(credentials)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    url = f"http://127.0.0.1:{port}{settings.mcp_http_path}"
    headers = {
        "Authorization": f"Bearer {bundle}",
        "X-AgentChatRoom-Software-Key": "shared-http-agent",
        "X-AgentChatRoom-Software-Name": "Shared HTTP Agent",
        "X-AgentChatRoom-Software-Client": "shared-http-agent",
    }

    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_a:
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as http_b:
                async with streamable_http_client(url, http_client=http_a) as (ra, wa, _):
                    async with ClientSession(ra, wa) as session_a:
                        await session_a.initialize()
                        boot_a = await session_a.call_tool(
                            "room_bootstrap", {"project_name": "Project A"}
                        )
                        parsed_a = _parse_tool(boot_a)
                        assert parsed_a["ok"], parsed_a
                        async with streamable_http_client(url, http_client=http_b) as (rb, wb, _):
                            async with ClientSession(rb, wb) as session_b:
                                await session_b.initialize()
                                boot_b = await session_b.call_tool(
                                    "room_bootstrap", {"project_name": "Project B"}
                                )
                                parsed_b = _parse_tool(boot_b)
                                assert parsed_b["ok"], parsed_b
                                assert parsed_a["result"]["project"]["id"] == projects["Project A"]["id"]
                                assert parsed_b["result"]["project"]["id"] == projects["Project B"]["id"]
                                for project in projects.values():
                                    members = app.state.service.list_project_members(project["id"])
                                    assert len(members) == 1
                                    assert members[0]["name"] == "Shared HTTP Agent"
                                    assert members[0]["metadata"]["software_key"] == "shared-http-agent"

                                posted_a, posted_b = await asyncio.gather(
                                    session_a.call_tool(
                                        "message_post",
                                        {"body": "bundle-a", "model_display_name": "test"},
                                    ),
                                    session_b.call_tool(
                                        "message_post",
                                        {"body": "bundle-b", "model_display_name": "test"},
                                    ),
                                )
                                assert _parse_tool(posted_a)["ok"]
                                assert _parse_tool(posted_b)["ok"]
                                assert "bundle-a" in json.dumps(
                                    app.state.service.list_events(projects["Project A"]["id"])
                                )
                                assert "bundle-a" not in json.dumps(
                                    app.state.service.list_events(projects["Project B"]["id"])
                                )
                                cross_project = _parse_tool(
                                    await session_a.call_tool(
                                        "message_post",
                                        {
                                            "project_id": projects["Project B"]["id"],
                                            "body": "must-not-cross",
                                            "model_display_name": "test",
                                        },
                                    )
                                )
                                assert not cross_project["ok"]
                                rebound = _parse_tool(
                                    await session_a.call_tool(
                                        "room_bootstrap", {"project_name": "Project B"}
                                    )
                                )
                                assert not rebound["ok"]
                                assert rebound["error"]["code"] == "project_session_rebind_forbidden"
                                still_a = _parse_tool(await session_a.call_tool("room_sync", {}))
                                assert still_a["ok"]
                                app.state.service.revoke_agent_token(
                                    projects["Project A"]["id"],
                                    issued_credentials["Project A"]["credential"]["id"],
                                )
                                revoked_a = _parse_tool(
                                    await session_a.call_tool(
                                        "message_post",
                                        {"body": "revoked-must-fail", "model_display_name": "test"},
                                    )
                                )
                                assert not revoked_a["ok"]
                                assert _parse_tool(await session_b.call_tool("room_sync", {}))["ok"]
    finally:
        server.should_exit = True
        await server_task
        _clear_http_mcp_context()


def _initialize_request(request_id: int, client_name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1"},
        },
    }


def _http_tool_payload(response: httpx.Response) -> dict:
    return json.loads(response.json()["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_expired_http_session_reports_recovery_and_reclaims_unfinished_task(
    settings, project_dir
):
    """#116: 过期 transport 保持规范 404 且可识别；新会话恢复原 Project 并可同身份 reclaim。"""
    tuned = replace(
        settings,
        heartbeat_timeout_seconds=0.5,
        presence_keepalive_interval_seconds=0.05,
        mcp_http_session_idle_timeout_seconds=0.3,
    )
    app = create_app(tuned)
    project = app.state.service.create_project(
        root_path=str(project_dir), name="Expired Session"
    )
    credential = app.state.service.issue_agent_token(
        project["id"], name="Expired Session"
    )
    bundle = encode_project_credential_bundle(
        [{"name": project["name"], "token": credential["token"]}]
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    url = f"http://127.0.0.1:{port}{tuned.mcp_http_path}"
    headers = {
        "Authorization": f"Bearer {bundle}",
        "X-AgentChatRoom-Software-Key": "expired-http",
        "X-AgentChatRoom-Software-Name": "Expired HTTP",
        "X-AgentChatRoom-Software-Client": "test-http",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    request_ids = iter(range(100, 200))

    async def open_transport(client: httpx.AsyncClient) -> str:
        initialized = await client.post(
            url, json=_initialize_request(next(request_ids), "expiring-client")
        )
        assert initialized.status_code == 200, initialized.text
        transport_id = initialized.headers["mcp-session-id"]
        notified = await client.post(
            url,
            headers={"mcp-session-id": transport_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notified.status_code == 202, notified.text
        return transport_id

    async def call_tool(
        client: httpx.AsyncClient, transport_id: str, name: str, arguments: dict
    ) -> httpx.Response:
        return await client.post(
            url,
            headers={"mcp-session-id": transport_id},
            json={
                "jsonrpc": "2.0",
                "id": next(request_ids),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )

    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            stale_transport = await open_transport(client)
            booted = _http_tool_payload(
                await call_tool(
                    client,
                    stale_transport,
                    "room_bootstrap",
                    {"project_name": project["name"]},
                )
            )
            assert booted["ok"], booted

            created = _http_tool_payload(
                await call_tool(
                    client,
                    stale_transport,
                    "task_create",
                    {
                        "title": "#116 expiry probe",
                        "description": "owned by the session that expires",
                        "acceptance_criteria": ["an expired session keeps its task"],
                    },
                )
            )
            assert created["ok"], created
            task_id = created["result"]["task"]["id"]
            claimed = _http_tool_payload(
                await call_tool(
                    client, stale_transport, "task_claim", {"task_id": task_id}
                )
            )
            assert claimed["ok"], claimed

            # Exceed the configured idle timeout without any request at all.
            await asyncio.sleep(0.6)

            expired = await client.post(
                url,
                headers={"mcp-session-id": stale_transport},
                json={"jsonrpc": "2.0", "id": 90, "method": "tools/list"},
            )
            assert expired.status_code == 404, expired.text
            assert expired.headers.get("x-agentchatroom-mcp-session") == "expired"
            error = expired.json()["error"]
            assert error["message"] == "Session not found"
            assert error["data"]["code"] == "mcp_session_expired"
            assert error["data"]["required_action"] == "reconnect_mcp_session"
            assert error["data"]["reconnect_hint"]

            fresh_transport = await open_transport(client)
            assert fresh_transport != stale_transport
            rebound = _http_tool_payload(
                await call_tool(
                    client,
                    fresh_transport,
                    "room_bootstrap",
                    {"project_name": project["name"]},
                )
            )
            assert rebound["ok"], rebound
            assert rebound["result"]["project"]["name"] == project["name"]
            assert (
                rebound["result"]["session"]["id"] != booted["result"]["session"]["id"]
            )

            reclaimed = None
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                reclaimed = _http_tool_payload(
                    await call_tool(
                        client,
                        fresh_transport,
                        "task_claim",
                        {"task_id": task_id, "reclaim": True},
                    )
                )
                if reclaimed["ok"]:
                    break
                await asyncio.sleep(0.1)
            assert reclaimed is not None and reclaimed["ok"], reclaimed
            snapshot = app.state.service.snapshot(project["id"])
            task_state = next(
                item for item in snapshot["tasks"] if item["id"] == task_id
            )
            assert task_state["owner_session_id"] == rebound["result"]["session"]["id"]
    finally:
        server.should_exit = True
        await server_task
        _clear_http_mcp_context()
