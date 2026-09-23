from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentchatroom import mcp_server
from agentchatroom.bootstrap import (
    BOOTSTRAP_STATES,
    PROJECT_PATH_ENV,
    REQUIRED_ACTIONS,
    SOFTWARE_CLIENT_ENV,
    SOFTWARE_KEY_ENV,
    SOFTWARE_NAME_ENV,
    RuntimeBinding,
    bind_runtime_arguments,
    bootstrap_local_room,
    bootstrap_status_payload,
    configured_software_identity,
    contains_secret,
    discover_workspace_candidates,
    redact_runtime_value,
    workspace_path_from_file_uri,
)
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import register_checkout_project


def _configure_software(monkeypatch, *, key="boot-agent", name="Boot Agent", client="codex"):
    monkeypatch.setenv(SOFTWARE_KEY_ENV, key)
    monkeypatch.setenv(SOFTWARE_NAME_ENV, name)
    monkeypatch.setenv(SOFTWARE_CLIENT_ENV, client)


@pytest.fixture(autouse=True)
def isolate_bootstrap_runtime(monkeypatch):
    for key in (
        PROJECT_PATH_ENV,
        SOFTWARE_CLIENT_ENV,
        SOFTWARE_KEY_ENV,
        SOFTWARE_NAME_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    mcp_server.clear_runtime_binding()
    mcp_server._loaded_identity = None
    yield
    mcp_server.clear_runtime_binding()
    mcp_server._loaded_identity = None


def _call_tool(name: str, arguments: dict | None = None, context=None):
    return asyncio.run(
        mcp_server.mcp._tool_manager.call_tool(
            name,
            arguments or {},
            context=context,
        )
    )


def _register_project(service, project_dir):
    project = service.create_project(root_path=str(project_dir), name="Bootstrap Project")
    register_checkout_project(project_dir, project)
    return project


def test_bootstrap_states_have_exactly_one_required_action():
    assert "ready" in BOOTSTRAP_STATES
    assert set(REQUIRED_ACTIONS) == set(BOOTSTRAP_STATES) - {"ready"}
    ready = bootstrap_status_payload("ready")
    assert "required_action" not in ready
    for status, action in REQUIRED_ACTIONS.items():
        payload = bootstrap_status_payload(status)
        assert payload["required_action"] == action
        assert payload["conversation_synced"] is False


def test_configured_software_identity_rejects_placeholders(monkeypatch):
    monkeypatch.setenv(SOFTWARE_KEY_ENV, "<software-key>")
    monkeypatch.setenv(SOFTWARE_NAME_ENV, "Named")
    monkeypatch.setenv(SOFTWARE_CLIENT_ENV, "codex")
    assert configured_software_identity() is None


def test_workspace_path_from_file_uri_roundtrip(tmp_path):
    resolved = tmp_path.resolve()
    assert workspace_path_from_file_uri(resolved.as_uri()) == resolved
    assert workspace_path_from_file_uri("https://example.invalid/not-a-file") is None


def test_redact_runtime_value_omits_token_keys():
    redacted = redact_runtime_value(
        {"token": "secret-token-value", "session_id": "agent_1", "nested": {"access_token": "x"}}
    )
    encoded = json.dumps(redacted)
    assert "secret-token-value" not in encoded
    assert "token" not in redacted
    assert redacted["session_id"] == "agent_1"


def test_bind_runtime_arguments_allows_explicit_credentials_without_binding():
    forwarded = bind_runtime_arguments(
        ["project_id", "session_id", "token"],
        {
            "project_id": "project_explicit",
            "session_id": "agent_explicit",
            "token": "explicit-token",
        },
        None,
    )
    assert forwarded["token"] == "explicit-token"


def test_bind_runtime_arguments_requires_bootstrap_when_credentials_missing():
    with pytest.raises(DomainError) as error:
        bind_runtime_arguments(["project_id", "session_id", "token"], {}, None)
    assert error.value.code == "session_expired"
    assert error.value.details["required_action"] == "call_room_bootstrap"


def test_bind_runtime_arguments_does_not_inject_foreign_session_aliases():
    from agentchatroom.bootstrap import RuntimeBinding

    binding = RuntimeBinding(
        project_id="project_bound",
        session_id="agent_bound",
        token="bound-token",
        cursor=1,
        software_key="boot-agent",
        agent_key="member_bound",
        conversation_synced=True,
    )
    forwarded = bind_runtime_arguments(
        [
            "project_id",
            "token",
            "assigned_by_session_id",
            "assigned_to_session_id",
            "to_session_id",
        ],
        {},
        binding,
    )
    assert forwarded["assigned_by_session_id"] == "agent_bound"
    assert "assigned_to_session_id" not in forwarded
    assert "to_session_id" not in forwarded


def test_ready_bootstrap_creates_independent_session_and_hides_token(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    first = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    assert first.binding is not None
    token = first.binding.token
    assert first.public["status"] == "ready"
    assert first.public["conversation_synced"] is True
    assert first.public["connection"]["software_configured"] is True
    assert first.public["connection"]["process_connected"] is True
    assert first.public["connection"]["conversation_synced"] is True
    assert first.public["connection"]["room_session"] == "created"
    assert first.public["project"]["id"] == project["id"]
    assert "required_action" not in first.public
    assert contains_secret(first.public, token) is False
    encoded = json.dumps(first.public, ensure_ascii=False, default=str)
    assert token not in encoded
    assert "token" not in first.public
    assert "token" not in first.public.get("session", {})

    created = service.create_task(
        project["id"],
        title="Stay claimed across bootstrap",
        acceptance_criteria=["Ownership remains with the first conversation"],
        actor_session_id=first.binding.session_id,
        token=first.binding.token,
    )["task"]
    service.claim_task(
        project["id"], created["id"], first.binding.session_id, first.binding.token
    )
    lease = service.acquire_lease(
        project["id"],
        session_id=first.binding.session_id,
        token=first.binding.token,
        task_id=created["id"],
        path_pattern="src/bootstrap.py",
    )["lease"]
    before_status = service.get_task(project["id"], created["id"])["execution_status"]
    audit_before = {
        event["id"]
        for event in service.query_audit(project["id"], task_id=created["id"])["events"]
    }

    second = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    assert second.binding is not None
    assert second.binding.session_id != first.binding.session_id
    assert second.binding.token != token
    stored = service.get_task(project["id"], created["id"])
    assert stored["owner_session_id"] == first.binding.session_id
    assert stored["execution_status"] == before_status
    snapshot = service.snapshot(project["id"])
    online = [agent for agent in snapshot["agents"] if agent["status"] == "online"]
    assert {agent["id"] for agent in online} == {
        first.binding.session_id,
        second.binding.session_id,
    }
    transferred_lease = next(
        item for item in snapshot["leases"] if item["id"] == lease["id"]
    )
    assert transferred_lease["session_id"] == first.binding.session_id
    new_task_events = [
        event
        for event in service.query_audit(project["id"], task_id=created["id"])["events"]
        if event["id"] not in audit_before
        and event["event_type"] in {"task.claimed", "task.created"}
    ]
    assert new_task_events == []
    audit = json.dumps(service.query_audit(project["id"]), ensure_ascii=False, default=str)
    assert second.binding.token not in audit
    members = [
        member
        for member in service.list_project_members(project["id"])
        if member.get("metadata", {}).get("software_key") == "boot-agent"
        or member.get("member_key") == "software:boot-agent"
    ]
    assert len(members) == 1


def test_bootstrap_keeps_pending_assignment_and_handoff_with_original_session(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    first = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        cwd=project_dir,
    )
    assert first.binding is not None
    other = service.join_room(
        project["id"],
        software_key="other-agent",
        name="Other Agent",
        client="other-client",
        model="unknown",
    )
    assigned_task = service.create_task(
        project["id"],
        title="Pending assignment survives bootstrap",
        acceptance_criteria=["Target session remains stable"],
        actor_session_id=other["agent"]["id"],
        token=other["token"],
    )["task"]
    assigned = service.assign_task(
        project["id"],
        assigned_task["id"],
        assigned_by_session_id=other["agent"]["id"],
        token=other["token"],
        assigned_to_session_id=first.binding.session_id,
        note="Please take this after reconnect",
    )
    owned = service.create_task(
        project["id"],
        title="Pending handoff survives bootstrap",
        acceptance_criteria=["From session remains stable"],
        actor_session_id=other["agent"]["id"],
        token=other["token"],
    )["task"]
    service.claim_task(
        project["id"], owned["id"], first.binding.session_id, first.binding.token
    )
    requested = service.handoff_task(
        project["id"],
        owned["id"],
        from_session_id=first.binding.session_id,
        token=first.binding.token,
        to_session_id=other["agent"]["id"],
        summary="Continue after reconnect",
        next_step="Acknowledge the handoff",
    )

    second = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        cwd=project_dir,
    )
    assert second.binding is not None
    stored_assignment = service.get_task(project["id"], assigned_task["id"])
    assignment = next(
        item
        for item in stored_assignment["assignments"]
        if item["id"] == assigned["assignment"]["id"]
    )
    assert assignment["status"] == "pending"
    assert assignment["assigned_to_session_id"] == first.binding.session_id
    stored_handoff = service.get_task(project["id"], owned["id"])
    handoff = next(
        item
        for item in stored_handoff["handoffs"]
        if item["id"] == requested["handoff"]["id"]
    )
    assert handoff["status"] == "pending"
    assert handoff["from_session_id"] == first.binding.session_id
    assert stored_handoff["owner_session_id"] == first.binding.session_id


def test_bootstrap_does_not_create_a_room_without_registration(
    monkeypatch, service, tmp_path
):
    _configure_software(monkeypatch)
    empty = tmp_path / "empty-checkout"
    empty.mkdir()
    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        cwd=empty,
    )
    assert outcome.binding is None
    assert outcome.public["status"] == "project_not_registered"
    assert outcome.public["required_action"] == "create_or_open_project_in_web"
    assert service.list_projects() == []


def test_invalid_registration_and_ambiguous_workspaces(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    first = _register_project(service, project_dir)
    other = tmp_path / "other-checkout"
    other.mkdir()
    second = service.create_project(root_path=str(other), name="Other Room")
    register_checkout_project(other, second)

    ambiguous = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        workspace_roots=[project_dir, other],
        cwd=None,
    )
    assert ambiguous.public["status"] == "ambiguous_workspace"
    assert ambiguous.public["required_action"] == "open_one_workspace_folder"
    assert ambiguous.public["details"]["candidate_count"] == 2

    pinned_cannot_resolve_ambiguity = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        workspace_roots=[project_dir, other],
        explicit_project_path=project_dir,
    )
    assert pinned_cannot_resolve_ambiguity.public["status"] == "ambiguous_workspace"
    assert (
        pinned_cannot_resolve_ambiguity.public["required_action"]
        == "open_one_workspace_folder"
    )

    broken = tmp_path / "broken-checkout"
    broken.mkdir()
    target = broken / ".agentchatroom"
    target.mkdir()
    (target / "project.json").write_text("{not-json", encoding="utf-8")
    invalid = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        cwd=broken,
    )
    assert invalid.public["status"] == "registration_invalid"
    assert invalid.public["required_action"] == "recreate_checkout_registration_via_web"


def test_archived_project_is_room_unavailable(monkeypatch, service, project_dir):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    service.archive_project(project["id"])
    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        cwd=project_dir,
    )
    assert outcome.public["status"] == "room_unavailable"
    assert outcome.public["required_action"] == "restore_or_wait_for_room"


def test_mcp_room_bootstrap_injects_runtime_and_rejects_mismatch(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.chdir(project_dir)

    missing = mcp_server.room_bootstrap()
    assert missing["ok"] is True
    public = json.dumps(missing, ensure_ascii=False, default=str)
    binding = mcp_server.get_runtime_binding()
    assert binding is not None
    assert binding.token not in public
    assert missing["result"]["status"] == "ready"

    synced = _call_tool("room_sync")
    assert synced["ok"] is True
    assert binding.token not in json.dumps(synced, ensure_ascii=False, default=str)
    created = _call_tool(
        "task_create",
        {
            "title": "Injected task",
            "acceptance_criteria": ["Created without copying a token"],
        },
    )
    assert created["ok"] is True
    assert created["result"]["task"]["project_id"] == project["id"]
    # #168：未传 actor_session_id 时任务自动归属当前绑定的 Room 会话，
    # 协作时间线能显示真实操作者而不是 unknown。
    assert (
        created["result"]["task"]["created_by_session_id"]
        == binding.session_id
    )
    created_history = _call_tool(
        "task_history", {"task_id": created["result"]["task"]["id"]}
    )
    assert created_history["ok"] is True
    created_event = next(
        item
        for item in created_history["result"]["items"]
        if item["event_type"] == "task.created"
    )
    assert created_event["actor"]["name"] == "Boot Agent"
    assert created_event["actor"]["client"] == "codex"
    assert created_event["actor"]["session_id"] == binding.session_id

    mismatch = _call_tool("room_sync", {"project_id": "project_other"})
    assert mismatch["ok"] is False
    assert mismatch["error"]["code"] == "runtime_context_mismatch"

    session_mismatch = _call_tool("room_sync", {"session_id": "agent_other"})
    assert session_mismatch["error"]["code"] == "runtime_context_mismatch"

    token_mismatch = _call_tool("room_sync", {"token": "other-token-value-not-the-binding"})
    assert token_mismatch["error"]["code"] == "runtime_context_mismatch"


def test_mcp_task_update_cannot_change_another_sessions_task(
    monkeypatch, service, project_dir
):
    """#136 回归：MCP 只能以本运行时绑定的 Session 身份行动。

    工具层会自动注入绑定的会话凭据，因此调用方既不能匿名改写他人任务，也不能
    自带 management_authorized 自我提权；非 owner 绑定更新普通字段必须被拒绝。
    """
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    monkeypatch.setattr(mcp_server, "service", service)

    owner = service.join_room(
        project["id"],
        name="Codex",
        client="codex",
        model="unknown",
        member_id=service.create_project_member(
            project["id"], member_key="codex", name="Codex"
        )["member"]["id"],
    )
    intruder = service.join_room(
        project["id"],
        name="Grok Build",
        client="grok-build",
        model="unknown",
        member_id=service.create_project_member(
            project["id"], member_key="grok-build", name="Grok Build"
        )["member"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="MCP authority target",
        acceptance_criteria=["Only the owner may edit this task"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    service.claim_task(project["id"], task["id"], owner["agent"]["id"], owner["token"])

    # 本运行时绑定的是“非 owner”会话，工具调用必须被拒绝且无副作用。
    mcp_server.persist_runtime_binding(
        "stdio:local",
        RuntimeBinding(
            project_id=project["id"],
            session_id=intruder["agent"]["id"],
            token=intruder["token"],
            cursor=0,
            software_key="grok-build",
            agent_key="grok-build",
            conversation_synced=True,
        ),
    )

    denied = _call_tool(
        "task_update", {"task_id": task["id"], "title": "Intruder mutation"}
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "not_task_owner"
    stored = service.get_task(project["id"], task["id"])
    assert stored["title"] == "MCP authority target"
    assert stored["owner_session_id"] == owner["agent"]["id"]

    # MCP 的输入模型不暴露管理授权开关，调用方无法自我提权。
    tools = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}
    properties = tools["task_update"].parameters.get("properties", {})
    assert "management_authorized" not in properties


def test_mcp_call_tool_keeps_explicit_join_credentials_without_bootstrap(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    service.create_project(root_path=str(project_dir))
    monkeypatch.setattr(mcp_server, "service", service)
    joined = mcp_server.room_join(project_path=str(project_dir), model="unknown")["result"]
    assert mcp_server.get_runtime_binding() is None
    synced = _call_tool(
        "room_sync",
        {
            "project_id": joined["project"]["id"],
            "session_id": joined["agent"]["id"],
            "token": joined["token"],
        },
    )
    assert synced["ok"] is True


def test_auto_join_establishes_presence_without_conversation_binding(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    _register_project(service, project_dir)
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.setenv(PROJECT_PATH_ENV, str(project_dir))
    monkeypatch.chdir(project_dir)
    joined = mcp_server._auto_join_local_checkout()
    assert joined is not None
    assert joined["token"]
    assert mcp_server.get_runtime_binding() is None


def test_identity_not_configured_and_restart_required(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    missing = mcp_server.room_bootstrap()
    assert missing["ok"] is False
    assert missing["result"]["status"] == "identity_not_configured"
    assert missing["error"]["details"]["required_action"] == (
        "open_local_mcp_config_assistant"
    )

    _configure_software(monkeypatch)
    _register_project(service, project_dir)
    monkeypatch.chdir(project_dir)
    mcp_server._loaded_identity = ("other-key", "Other", "other")
    restart = mcp_server.room_bootstrap(model="unknown")
    assert restart["ok"] is False
    assert restart["result"]["status"] == "mcp_restart_required"
    assert restart["result"]["required_action"] == "restart_mcp_client_session"


def test_concurrent_streamable_sessions_do_not_share_runtime_bindings(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    first = _register_project(service, project_dir)
    other = tmp_path / "other-http-checkout"
    other.mkdir()
    second = service.create_project(root_path=str(other), name="Other HTTP Room")
    register_checkout_project(other, second)
    monkeypatch.setattr(mcp_server, "service", service)

    class FakeContext:
        def __init__(self, uris: list[str], session_id: str) -> None:
            async def list_roots():
                return SimpleNamespace(
                    roots=[SimpleNamespace(uri=uri) for uri in uris]
                )

            self.request_context = SimpleNamespace(
                session=SimpleNamespace(
                    list_roots=list_roots,
                    session_id=session_id,
                )
            )

    ctx_a = FakeContext([project_dir.resolve().as_uri()], "http-session-a")
    ctx_b = FakeContext([other.resolve().as_uri()], "http-session-b")
    monkeypatch.chdir(project_dir)
    boot_a = _call_tool(
        "room_bootstrap",
        {},
        context=ctx_a,
    )
    monkeypatch.chdir(other)
    boot_b = _call_tool(
        "room_bootstrap",
        {},
        context=ctx_b,
    )
    assert boot_a["ok"] is True
    assert boot_b["ok"] is True
    assert boot_a["result"]["project"]["id"] == first["id"]
    assert boot_b["result"]["project"]["id"] == second["id"]
    key_a = mcp_server.mcp_session_key(ctx_a)
    key_b = mcp_server.mcp_session_key(ctx_b)
    assert mcp_server.get_runtime_binding(key_a).project_id == first["id"]
    assert mcp_server.get_runtime_binding(key_b).project_id == second["id"]
    assert (
        mcp_server.get_runtime_binding(key_a).token
        != mcp_server.get_runtime_binding(key_b).token
    )

    sync_a = _call_tool(
        "room_sync",
        {},
        context=ctx_a,
    )
    sync_b = _call_tool(
        "room_sync",
        {},
        context=ctx_b,
    )
    assert sync_a["ok"] is True
    assert sync_b["ok"] is True
    assert sync_a["result"]["snapshot"]["project"]["id"] == first["id"]
    assert sync_b["result"]["snapshot"]["project"]["id"] == second["id"]
    mismatch = _call_tool(
        "room_sync",
        {"project_id": first["id"]},
        context=ctx_b,
    )
    assert mismatch["ok"] is False
    assert mismatch["error"]["code"] == "runtime_context_mismatch"


def test_session_leave_clears_runtime_binding(monkeypatch, service, project_dir):
    _configure_software(monkeypatch)
    _register_project(service, project_dir)
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.chdir(project_dir)
    assert mcp_server.room_bootstrap()["ok"] is True
    binding = mcp_server.get_runtime_binding()
    assert binding is not None
    left = _call_tool("session_leave")
    assert left["ok"] is True
    assert mcp_server.get_runtime_binding() is None
    expired = _call_tool("room_sync")
    assert expired["error"]["code"] == "session_expired"


def test_bootstrap_does_not_log_live_token(monkeypatch, service, project_dir):
    _configure_software(monkeypatch)
    _register_project(service, project_dir)
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = Capture()
    handler.setLevel(logging.DEBUG)
    loggers = [
        logging.getLogger("agentchatroom"),
        logging.getLogger("agentchatroom.bootstrap"),
        logging.getLogger("agentchatroom.mcp_server"),
    ]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        outcome = bootstrap_local_room(
            service,
            software_key="boot-agent",
            software_name="Boot Agent",
            client="codex",
            cwd=project_dir,
        )
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
    assert outcome.binding is not None
    combined = "\n".join(records)
    assert outcome.binding.token not in combined


def test_live_token_is_absent_from_public_docs_and_registration(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        cwd=project_dir,
    )
    token = outcome.binding.token
    root = Path(__file__).resolve().parents[1]
    for path in (
        root / "README.md",
        root / "AGENTS.md",
        project_dir / ".agentchatroom" / "project.json",
    ):
        text = path.read_text(encoding="utf-8")
        assert token not in text
    assert project["id"] not in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_mcp_workspace_roots_do_not_enter_the_wrong_room(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    first = _register_project(service, project_dir)
    other = tmp_path / "second-root"
    other.mkdir()
    second = service.create_project(root_path=str(other), name="Second")
    register_checkout_project(other, second)
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.chdir(project_dir)

    class FakeContext:
        def __init__(self, uris: list[str]) -> None:
            async def list_roots():
                return SimpleNamespace(
                    roots=[SimpleNamespace(uri=uri) for uri in uris]
                )

            self.request_context = SimpleNamespace(
                session=SimpleNamespace(list_roots=list_roots)
            )

    ambiguous = _call_tool(
        "room_bootstrap",
        {},
        context=FakeContext([project_dir.resolve().as_uri(), other.resolve().as_uri()]),
    )
    assert ambiguous["ok"] is False
    assert ambiguous["result"]["status"] == "ambiguous_workspace"

    selected = _call_tool(
        "room_bootstrap",
        {},
        context=FakeContext([project_dir.resolve().as_uri()]),
    )
    assert selected["ok"] is True
    assert selected["result"]["project"]["id"] == first["id"]
    assert selected["result"]["project"]["id"] != second["id"]


def test_discover_workspace_candidates_uses_workspace_evidence_first(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / ".agentchatroom").mkdir()
    (first / ".agentchatroom" / "project.json").write_text("{}", encoding="utf-8")
    (second / ".agentchatroom").mkdir()
    (second / ".agentchatroom" / "project.json").write_text("{}", encoding="utf-8")
    found = discover_workspace_candidates(
        workspace_roots=[first, second],
        cwd=second,
    )
    assert found == [first.resolve(), second.resolve()]
    assert discover_workspace_candidates(cwd=second) == [second.resolve()]
    assert discover_workspace_candidates() == []


def test_configured_project_path_is_fallback_not_override(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    pinned = _register_project(service, project_dir)
    other_dir = tmp_path / "other-workspace"
    other_dir.mkdir()
    other = service.create_project(root_path=str(other_dir), name="Other")
    register_checkout_project(other_dir, other)

    pinned_session = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    assert pinned_session.binding is not None
    pinned_session_id = pinned_session.binding.session_id

    selected = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        workspace_roots=[other_dir],
        cwd=other_dir,
        explicit_project_path=project_dir,
    )
    assert selected.binding is not None
    assert selected.binding.project_id == other["id"]
    assert selected.binding.project_id != pinned["id"]
    notices = selected.public.get("notices") or []
    assert any(
        notice.get("code") == "configured_project_path_ignored" for notice in notices
    )

    snapshot = service.snapshot(pinned["id"])
    online = [agent for agent in snapshot["agents"] if agent["status"] == "online"]
    assert [agent["id"] for agent in online] == [pinned_session_id]


def test_unregistered_workspace_never_uses_stale_configured_path(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    unregistered = tmp_path / "plain"
    unregistered.mkdir()
    selected = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=unregistered,
        explicit_project_path=project_dir,
    )
    assert selected.binding is None
    assert selected.public["status"] == "project_not_registered"


def test_workspace_evidence_wins_over_unresolvable_pin(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    selected = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
        explicit_project_path=tmp_path / "not-a-checkout",
    )
    assert selected.binding is not None
    assert selected.binding.project_id == project["id"]


def test_auto_join_prefers_cwd_checkout_over_pinned_path(
    monkeypatch, service, project_dir, tmp_path
):
    _configure_software(monkeypatch)
    pinned = _register_project(service, project_dir)
    other_dir = tmp_path / "cwd-workspace"
    other_dir.mkdir()
    other = service.create_project(root_path=str(other_dir), name="Cwd")
    register_checkout_project(other_dir, other)
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.setenv(PROJECT_PATH_ENV, str(project_dir))
    monkeypatch.chdir(other_dir)
    joined = mcp_server._auto_join_local_checkout()
    assert joined is not None
    assert joined["project"]["id"] == other["id"]
    assert joined["project"]["id"] != pinned["id"]


def test_bootstrap_ensures_missing_agents_coordination_block(
    monkeypatch, service, project_dir
):
    """老项目：登记早于托管块能力（或文件被删）时，绑定即补写。"""
    from agentchatroom.integrations import build_project_coordination_instructions

    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    instructions_path = project_dir / "AGENTS.md"
    instructions_path.unlink()

    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    assert outcome.public["status"] == "ready"
    restored = instructions_path.read_text(encoding="utf-8")
    assert "BEGIN AgentChatRoom managed coordination" in restored
    assert "END AgentChatRoom managed coordination" in restored
    assert "room_sync" in restored
    codes = [
        notice["code"]
        for notice in outcome.public.get("notices", [])
    ]
    assert "project_instructions_created" in codes

    second = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    second_codes = [
        notice["code"]
        for notice in second.public.get("notices", [])
        if notice["code"].startswith("project_instructions_")
    ]
    assert second_codes == []
    assert build_project_coordination_instructions(project) in restored


def test_bootstrap_updates_stale_agents_coordination_block(
    monkeypatch, service, project_dir
):
    """托管块内容落后于当前规则时，绑定即更新为最新生成内容。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    instructions_path = project_dir / "AGENTS.md"
    current = instructions_path.read_text(encoding="utf-8")
    instructions_path.write_text(
        current.replace("room_sync", "legacy-sync-marker"), encoding="utf-8"
    )

    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    assert outcome.public["status"] == "ready"
    codes = [
        notice["code"]
        for notice in outcome.public.get("notices", [])
    ]
    assert "project_instructions_updated" in codes
    restored = instructions_path.read_text(encoding="utf-8")
    assert "legacy-sync-marker" not in restored
    assert restored == current


def _notice_codes(outcome):
    return [notice["code"] for notice in outcome.public.get("notices", [])]


def test_bootstrap_notice_root_fallback_without_instructions_change(
    monkeypatch, service, project_dir
):
    """#179：仅 server root 兜底时，只报一次 server_project_root_registered。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    primed = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        cwd=project_dir,
    )
    assert primed.public["status"] == "ready"

    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        workspace_roots=[],
        selected_project_id=project["id"],
    )
    assert outcome.public["status"] == "ready"
    codes = _notice_codes(outcome)
    assert codes.count("server_project_root_registered") == 1
    assert not any(code.startswith("project_instructions_") for code in codes)


def test_bootstrap_notice_instructions_update_without_root_fallback(
    monkeypatch, service, project_dir
):
    """#179：仅 instructions 更新时，不得误报 server_project_root_registered。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    instructions_path = project_dir / "AGENTS.md"
    current = instructions_path.read_text(encoding="utf-8")
    instructions_path.write_text(
        current.replace("room_sync", "legacy-sync-marker"), encoding="utf-8"
    )

    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        workspace_roots=[project_dir],
        selected_project_id=project["id"],
    )
    assert outcome.public["status"] == "ready"
    codes = _notice_codes(outcome)
    assert codes.count("project_instructions_updated") == 1
    assert "server_project_root_registered" not in codes


def test_bootstrap_notice_root_fallback_and_instructions_update(
    monkeypatch, service, project_dir
):
    """#179：root 兜底与 instructions 更新同时发生时，各只报一次。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    instructions_path = project_dir / "AGENTS.md"
    current = instructions_path.read_text(encoding="utf-8")
    instructions_path.write_text(
        current.replace("room_sync", "legacy-sync-marker"), encoding="utf-8"
    )

    outcome = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        workspace_roots=[],
        selected_project_id=project["id"],
    )
    assert outcome.public["status"] == "ready"
    codes = _notice_codes(outcome)
    assert codes.count("server_project_root_registered") == 1
    assert codes.count("project_instructions_updated") == 1


def _bootstrap(service, project_dir, **overrides):
    arguments = {
        "software_key": "boot-agent",
        "software_name": "Boot Agent",
        "client": "codex",
        "model": "unknown",
        "cwd": project_dir,
    }
    arguments.update(overrides)
    return bootstrap_local_room(service, **arguments)


def test_bootstrap_restores_the_runtime_session_instead_of_creating_one(
    monkeypatch, service, project_dir
):
    """#136：同一运行时再次 bootstrap 恢复原 Session，不累积孤儿会话。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)

    first = _bootstrap(service, project_dir)
    assert first.binding is not None
    assert first.public["connection"]["room_session"] == "created"
    sessions_before = len(service.snapshot(project["id"])["agents"])

    restored = _bootstrap(service, project_dir, restore_binding=first.binding)

    assert restored.binding is not None
    assert restored.public["connection"]["room_session"] == "restored"
    assert restored.binding.session_id == first.binding.session_id
    assert restored.binding.token == first.binding.token
    assert "runtime_session_restored" in [
        notice["code"] for notice in restored.public.get("notices", [])
    ]
    assert len(service.snapshot(project["id"])["agents"]) == sessions_before
    # 恢复的是可用会话，而不是只连上不能写。
    posted = service.post_message(
        project["id"],
        body="restored session still writes",
        session_id=restored.binding.session_id,
        token=restored.binding.token,
        model_display_name="test",
    )
    assert posted["event_id"]


def test_bootstrap_restores_session_with_newly_selected_credential(
    monkeypatch, service, project_dir
):
    """#205: credential linking must not crash the #136 restore path."""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    first = _bootstrap(service, project_dir)
    assert first.binding is not None
    issued = service.issue_agent_token(project["id"], name="Agent Token")
    credential_id = issued["credential"]["id"]
    task = service.create_task(
        project["id"], title="Keep ownership across bootstrap",
        acceptance_criteria=["Original Session remains task owner"],
        actor_session_id=first.binding.session_id, token=first.binding.token,
    )["task"]
    service.claim_task(
        project["id"], task["id"], first.binding.session_id, first.binding.token
    )
    lease = service.acquire_lease(
        project["id"], session_id=first.binding.session_id,
        token=first.binding.token, task_id=task["id"], path_pattern="src/restore.py",
    )["lease"]
    session_count = len(service.snapshot(project["id"])["agents"])

    restored = _bootstrap(
        service,
        project_dir,
        restore_binding=first.binding,
        credential_id=credential_id,
    )

    assert restored.public["connection"]["room_session"] == "restored"
    assert restored.binding.session_id == first.binding.session_id
    assert len(service.snapshot(project["id"])["agents"]) == session_count
    assert service.get_task(project["id"], task["id"])["owner_session_id"] == first.binding.session_id
    assert any(
        item["id"] == lease["id"] and item["session_id"] == first.binding.session_id
        for item in service.snapshot(project["id"])["leases"]
    )
    credential = next(
        item for item in service.list_agent_tokens(project["id"])
        if item["id"] == credential_id
    )
    assert credential["member_id"] is not None


def test_bootstrap_rejects_restoring_with_credential_pinned_to_another_member(
    monkeypatch, service, project_dir
):
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    first = _bootstrap(service, project_dir)
    assert first.binding is not None
    other = service.create_project_member(
        project["id"], member_key="other-client", name="Other client"
    )["member"]
    credential = service.issue_agent_token(
        project["id"], name="Other member credential", member_id=other["id"]
    )["credential"]

    rejected = _bootstrap(
        service, project_dir,
        restore_binding=first.binding,
        credential_id=credential["id"],
    )
    assert rejected.public["status"] == "room_unavailable"
    assert rejected.public["details"]["code"] == "credential_already_linked"
    assert rejected.binding is None
    assert service.list_agent_tokens(project["id"])[0]["member_id"] == other["id"]


def test_restored_session_keeps_task_ownership_after_a_cleared_context(
    monkeypatch, service, project_dir
):
    """#136 报告场景：上下文被清空但运行时仍在，任务所有权不得被搁置。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    first = _bootstrap(service, project_dir)
    assert first.binding is not None
    task = service.create_task(
        project["id"],
        title="Survives a new conversation",
        acceptance_criteria=["Ownership is unchanged"],
        actor_session_id=first.binding.session_id,
        token=first.binding.token,
    )["task"]
    service.claim_task(
        project["id"], task["id"], first.binding.session_id, first.binding.token
    )

    restored = _bootstrap(service, project_dir, restore_binding=first.binding)

    assert restored.binding.session_id == first.binding.session_id
    assert (
        service.get_task(project["id"], task["id"])["owner_session_id"]
        == first.binding.session_id
    )
    released = service.release_task(
        project["id"],
        task["id"],
        reason_code="other",
        session_id=restored.binding.session_id,
        token=restored.binding.token,
    )
    assert released["released"] is True


def test_bootstrap_creates_a_new_session_when_the_previous_one_is_closed(
    monkeypatch, service, project_dir
):
    """旧 Session 真的失效时不得假装恢复，应新建并保持原任务不动。"""
    _configure_software(monkeypatch)
    project = _register_project(service, project_dir)
    first = _bootstrap(service, project_dir)
    binding = first.binding
    task = service.create_task(
        project["id"],
        title="Left behind",
        acceptance_criteria=["Owner stays the closed session until reclaim"],
        actor_session_id=first.binding.session_id,
        token=first.binding.token,
    )["task"]
    service.claim_task(project["id"], task["id"], binding.session_id, binding.token)
    service.leave_session(project["id"], binding.session_id, binding.token)

    second = _bootstrap(service, project_dir, restore_binding=binding)

    assert second.binding is not None
    assert second.binding.session_id != binding.session_id
    assert second.public["connection"]["room_session"] == "created"
    assert (
        service.get_task(project["id"], task["id"])["owner_session_id"]
        == binding.session_id
    )


def test_mcp_room_bootstrap_restores_the_same_session_for_a_live_runtime(
    monkeypatch, service, project_dir
):
    """#136：MCP 工具层的重复 bootstrap 同样走恢复路径（接线回归）。"""
    _configure_software(monkeypatch)
    _register_project(service, project_dir)
    monkeypatch.setattr(mcp_server, "service", service)
    monkeypatch.chdir(project_dir)

    first = _call_tool("room_bootstrap", {})
    assert first["ok"] is True
    assert first["result"]["connection"]["room_session"] == "created"
    binding = mcp_server.get_runtime_binding()
    assert binding is not None

    second = _call_tool("room_bootstrap", {})

    assert second["ok"] is True
    assert second["result"]["connection"]["room_session"] == "restored"
    assert second["result"]["session"]["id"] == binding.session_id
    restored_binding = mcp_server.get_runtime_binding()
    assert restored_binding is not None
    assert restored_binding.session_id == binding.session_id
    assert restored_binding.token == binding.token


def test_runtime_binding_restore_requires_the_same_project_and_identity():
    from agentchatroom.bootstrap import RuntimeBinding, restore_runtime_binding

    binding = RuntimeBinding(
        project_id="project_bound",
        session_id="agent_bound",
        token="bound-token",
        cursor=0,
        software_key="boot-agent",
        agent_key="member_bound",
        conversation_synced=True,
    )

    assert (
        restore_runtime_binding(
            None,
            binding,
            project={"id": "project_other", "name": "Other"},
            software_key="boot-agent",
            software_name="Boot Agent",
            client="codex",
        )
        is None
    )
    assert (
        restore_runtime_binding(
            None,
            binding,
            project={"id": "project_bound", "name": "Bound"},
            software_key="other-software",
            software_name="Other",
            client="codex",
        )
        is None
    )


def test_bind_runtime_arguments_derives_actor_session_id_with_token():
    """#168：认证型工具的 actor_session_id 由运行时绑定派生，调用方无需手工提供。"""
    from agentchatroom.bootstrap import RuntimeBinding

    binding = RuntimeBinding(
        project_id="project_bound",
        session_id="agent_bound",
        token="bound-token",
        cursor=1,
        software_key="boot-agent",
        agent_key="member_bound",
        conversation_synced=True,
    )
    forwarded = bind_runtime_arguments(
        [
            "project_id",
            "title",
            "acceptance_criteria",
            "actor_session_id",
            "token",
        ],
        {},
        binding,
    )
    assert forwarded["actor_session_id"] == "agent_bound"
    assert forwarded["token"] == "bound-token"


def test_bind_runtime_arguments_keeps_audit_actor_filter_readonly():
    """#168：audit_query 的 actor_session_id 是只读过滤器（无 token 参数），不得注入。"""
    from agentchatroom.bootstrap import RuntimeBinding

    binding = RuntimeBinding(
        project_id="project_bound",
        session_id="agent_bound",
        token="bound-token",
        cursor=1,
        software_key="boot-agent",
        agent_key="member_bound",
        conversation_synced=True,
    )
    forwarded = bind_runtime_arguments(
        [
            "project_id",
            "after",
            "before",
            "limit",
            "event_type",
            "actor_session_id",
            "task_id",
        ],
        {},
        binding,
    )
    assert "actor_session_id" not in forwarded


def test_bind_runtime_arguments_rejects_foreign_actor_session_id():
    """#168：显式提供他人 actor_session_id 必须被拒绝，无法冒名创建任务。"""
    from agentchatroom.bootstrap import RuntimeBinding

    binding = RuntimeBinding(
        project_id="project_bound",
        session_id="agent_bound",
        token="bound-token",
        cursor=1,
        software_key="boot-agent",
        agent_key="member_bound",
        conversation_synced=True,
    )
    with pytest.raises(DomainError) as error:
        bind_runtime_arguments(
            ["project_id", "actor_session_id", "token"],
            {"actor_session_id": "agent_foreign", "token": "bound-token"},
            binding,
        )
    assert error.value.code == "runtime_context_mismatch"
