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


def test_ready_bootstrap_is_idempotent_and_hides_token(
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
    assert first.public["connection"]["room_session"] in {"restored", "replaced"}
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
        acceptance_criteria=["Ownership transfers without extra task events"],
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
    assert stored["owner_session_id"] == second.binding.session_id
    assert stored["execution_status"] == before_status
    snapshot = service.snapshot(project["id"])
    online = [agent for agent in snapshot["agents"] if agent["status"] == "online"]
    assert len(online) == 1
    assert online[0]["id"] == second.binding.session_id
    transferred_lease = next(
        item for item in snapshot["leases"] if item["id"] == lease["id"]
    )
    assert transferred_lease["session_id"] == second.binding.session_id
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


def test_bootstrap_transfers_pending_assignment_and_handoff(
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
        acceptance_criteria=["Target session is transferred"],
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
        acceptance_criteria=["From session is transferred"],
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
    assert assignment["assigned_to_session_id"] == second.binding.session_id
    stored_handoff = service.get_task(project["id"], owned["id"])
    handoff = next(
        item
        for item in stored_handoff["handoffs"]
        if item["id"] == requested["handoff"]["id"]
    )
    assert handoff["status"] == "pending"
    assert handoff["from_session_id"] == second.binding.session_id
    assert stored_handoff["owner_session_id"] == second.binding.session_id


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

    overridden = bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        workspace_roots=[project_dir, other],
        explicit_project_path=project_dir,
    )
    assert overridden.public["status"] == "ready"
    assert overridden.public["project"]["id"] == first["id"]

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

    mismatch = _call_tool("room_sync", {"project_id": "project_other"})
    assert mismatch["ok"] is False
    assert mismatch["error"]["code"] == "runtime_context_mismatch"

    session_mismatch = _call_tool("room_sync", {"session_id": "agent_other"})
    assert session_mismatch["error"]["code"] == "runtime_context_mismatch"

    token_mismatch = _call_tool("room_sync", {"token": "other-token-value-not-the-binding"})
    assert token_mismatch["error"]["code"] == "runtime_context_mismatch"


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

    monkeypatch.chdir(project_dir)
    boot_a = _call_tool(
        "room_bootstrap",
        {},
        context=FakeContext([project_dir.resolve().as_uri()], "http-session-a"),
    )
    monkeypatch.chdir(other)
    boot_b = _call_tool(
        "room_bootstrap",
        {},
        context=FakeContext([other.resolve().as_uri()], "http-session-b"),
    )
    assert boot_a["ok"] is True
    assert boot_b["ok"] is True
    assert boot_a["result"]["project"]["id"] == first["id"]
    assert boot_b["result"]["project"]["id"] == second["id"]
    assert mcp_server.get_runtime_binding("mcp:http-session-a").project_id == first["id"]
    assert mcp_server.get_runtime_binding("mcp:http-session-b").project_id == second["id"]
    assert (
        mcp_server.get_runtime_binding("mcp:http-session-a").token
        != mcp_server.get_runtime_binding("mcp:http-session-b").token
    )

    sync_a = _call_tool(
        "room_sync",
        {},
        context=FakeContext([], "http-session-a"),
    )
    sync_b = _call_tool(
        "room_sync",
        {},
        context=FakeContext([], "http-session-b"),
    )
    assert sync_a["ok"] is True
    assert sync_b["ok"] is True
    assert sync_a["result"]["snapshot"]["project"]["id"] == first["id"]
    assert sync_b["result"]["snapshot"]["project"]["id"] == second["id"]
    mismatch = _call_tool(
        "room_sync",
        {"project_id": first["id"]},
        context=FakeContext([], "http-session-b"),
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


def test_discover_workspace_candidates_prefer_explicit_override(tmp_path):
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
        explicit_project_path=first,
    )
    assert found == [first.resolve()]
