from __future__ import annotations

from dataclasses import replace
import os
import socket

import pytest

from agentchatroom import cli
from agentchatroom.cli import (
    main,
    parse_test_evidence,
    print_server_log,
    process_is_running,
    start_detached_server,
    stop_detached_server,
    validate_runtime_settings,
)


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_parse_test_evidence_preserves_commands_with_colons():
    assert parse_test_evidence(["python C:\\work\\test.py::0"]) == [
        {"command": "python C:\\work\\test.py", "exit_code": 0}
    ]


def test_request_json_sends_idempotency_key_only_for_mutating_requests(monkeypatch):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    assert cli.request_json(
        "http://127.0.0.1:8765",
        "POST",
        "/api/v1/example",
        {"value": 1},
        request_id="cli-write-1",
    ) == {"ok": True}
    assert cli.request_json(
        "http://127.0.0.1:8765",
        "GET",
        "/api/v1/example",
        request_id="ignored-for-read",
    ) == {"ok": True}

    write_headers = {key.lower(): value for key, value in requests[0][0].header_items()}
    read_headers = {key.lower(): value for key, value in requests[1][0].header_items()}
    assert write_headers["idempotency-key"] == "cli-write-1"
    assert "idempotency-key" not in read_headers


def test_detached_server_starts_and_stops(settings):
    port = available_port()
    started = start_detached_server(settings, "127.0.0.1", port)
    pid = started["pid"]
    try:
        assert process_is_running(pid)
        assert settings.database_path.exists()
        repeated = start_detached_server(settings, "127.0.0.1", port)
        assert repeated["already_running"] is True
        assert repeated["pid"] == pid
    finally:
        stopped = stop_detached_server(settings)
    assert stopped == {"stopped": True, "pid": pid}
    assert not process_is_running(pid)
    assert not (settings.data_dir / "server.pid").exists()


def test_stop_removes_stale_pid_file(settings):
    pid_path = settings.data_dir / "server.pid"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("99999999", encoding="ascii")

    result = stop_detached_server(settings)

    assert result["stopped"] is False
    assert result["reason"] == "not_running"
    assert not pid_path.exists()
    assert process_is_running(os.getpid())


def test_posix_process_check_treats_linux_zombie_as_stopped(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_linux_process_state", lambda _pid: "Z")

    def unexpected_kill(*_args):
        pytest.fail("A zombie process must not be probed as running")

    monkeypatch.setattr(cli.os, "kill", unexpected_kill)

    assert cli._posix_process_is_running(1234) is False


def test_serve_passes_configured_proxy_trust_to_uvicorn(monkeypatch, settings):
    configured = replace(
        settings,
        trusted_proxy_headers=False,
        trusted_proxy_ips="10.10.0.4,10.10.0.5",
    )
    captured = {}

    monkeypatch.setattr(cli, "load_settings", lambda _path=None: configured)
    monkeypatch.setattr(
        cli, "validate_runtime_settings", lambda _settings: {"valid": True}
    )
    monkeypatch.setattr(cli, "create_app", lambda _settings: object())
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda _app, **kwargs: captured.update(kwargs),
    )

    main(["serve"])

    assert captured["proxy_headers"] is False
    assert captured["forwarded_allow_ips"] == "10.10.0.4,10.10.0.5"


def test_serve_access_log_follows_environment_toggle(monkeypatch, settings):
    captured = {}

    monkeypatch.setattr(cli, "load_settings", lambda _path=None: settings)
    monkeypatch.setattr(
        cli, "validate_runtime_settings", lambda _settings: {"valid": True}
    )
    monkeypatch.setattr(cli, "create_app", lambda _settings: object())
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda _app, **kwargs: captured.update(kwargs),
    )

    monkeypatch.setenv("AGENTCHATROOM_ACCESS_LOG", "0")
    main(["serve"])
    assert captured["access_log"] is False

    monkeypatch.setenv("AGENTCHATROOM_ACCESS_LOG", "1")
    main(["serve"])
    assert captured["access_log"] is True

    monkeypatch.delenv("AGENTCHATROOM_ACCESS_LOG")
    main(["serve"])
    assert captured["access_log"] is True


def test_mcp_config_uses_current_runtime_and_configured_data_dir(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "runtime"))

    main(["mcp-config", "--format", "codex-toml"])

    output = capsys.readouterr().out
    assert "[mcp_servers.agentchatroom]" in output
    assert "agentchatroom.mcp_server" in output
    assert str(tmp_path / "runtime").replace("\\", "\\\\") in output


def test_mcp_config_explicit_data_dir_overrides_environment(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "wrong-runtime"))
    explicit_dir = tmp_path / "selected-runtime"

    main(
        [
            "mcp-config",
            "--format",
            "codex-toml",
            "--data-dir",
            str(explicit_dir),
        ]
    )

    output = capsys.readouterr().out
    assert str(explicit_dir.resolve()).replace("\\", "\\\\") in output
    assert str(tmp_path / "wrong-runtime").replace("\\", "\\\\") not in output


def test_mcp_config_prints_workbuddy_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "runtime"))

    main(["mcp-config", "--format", "workbuddy-json"])

    output = capsys.readouterr().out
    assert '"mcpServers"' in output
    assert '"agentchatroom"' in output


def test_mcp_config_prints_grok_toml(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "runtime"))

    main(["mcp-config", "--format", "grok-toml"])

    output = capsys.readouterr().out
    assert "[mcp_servers.agentchatroom]" in output
    assert "enabled = true" in output


def test_mcp_config_prints_streamable_http_codex_toml(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTCHATROOM_EXTERNAL_BASE_URL", "https://room.example.test")

    main(
        [
            "mcp-config",
            "--transport",
            "streamable-http",
            "--format",
            "codex-toml",
        ]
    )

    output = capsys.readouterr().out
    assert "url = \"https://room.example.test/mcp\"" in output
    assert 'bearer_token_env_var = "AGENTCHATROOM_AGENT_TOKEN"' in output


def test_config_check_reports_effective_sqlite_configuration(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "runtime"))

    main(["--json", "config-check"])

    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["checks"]["database"] == "sqlite"
    assert payload["checks"]["database_url"] == "not_required_for_sqlite"


def test_config_check_requires_server_secrets(monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_DATABASE_BACKEND", "postgresql")
    monkeypatch.setenv("AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED", "true")
    monkeypatch.delenv("AGENTCHATROOM_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_ADMIN_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="AGENTCHATROOM_DATABASE_URL"):
        main(["config-check"])


def test_validate_runtime_settings_never_returns_secret(monkeypatch, settings):
    token = "management-token-with-more-than-24-characters"
    monkeypatch.setenv(settings.management_token_env, token)
    protected = replace(settings, management_auth_required=True)

    result = validate_runtime_settings(protected)

    assert result["valid"] is True
    assert token not in str(result)


def test_print_server_log_reads_configured_data_dir(settings, capsys):
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "server.log").write_text(
        "first\nsecond\nthird\n", encoding="utf-8"
    )

    print_server_log(settings, lines=2)

    assert capsys.readouterr().out == "second\nthird\n"


def test_follow_log_returns_after_managed_service_stops(settings, capsys):
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "server.log").write_text("stopped\n", encoding="utf-8")

    print_server_log(settings, lines=1, follow=True)

    assert capsys.readouterr().out == "stopped\n"


def test_project_delete_requires_explicit_confirmation():
    with pytest.raises(SystemExit, match="requires --confirm"):
        main(["project-delete", "project_example"])


def test_work_report_cli_allows_explicit_no_code_reason(monkeypatch, capsys):
    captured = {}

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        captured.update(
            {
                "base": base,
                "method": method,
                "path": path,
                "body": body,
                "request_id": request_id,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(
        [
            "work-report",
            "project_example",
            "task_example",
            "Investigation complete",
            "--session-id",
            "agent_example",
            "--token",
            "session-token",
            "--test",
            "Manual evidence review::0",
            "--no-code-change-reason",
            "No repository changes were required",
        ]
    )

    assert captured["method"] == "POST"
    assert captured["body"]["files"] == []
    assert captured["body"]["no_code_change_reason"] == (
        "No repository changes were required"
    )
    assert '"ok": true' in capsys.readouterr().out


def test_task_get_cli_uses_exact_task_endpoint(monkeypatch, capsys):
    captured = {}

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        captured.update(
            {"base": base, "method": method, "path": path, "body": body}
        )
        return {"task": {"id": "task_example"}}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(["task-get", "project_example", "task_example"])

    assert captured["method"] == "GET"
    assert captured["path"].endswith("/tasks/task_example")
    assert '"task"' in capsys.readouterr().out


def test_project_member_cli_commands_use_shared_rest_contract(monkeypatch, capsys):
    calls = []

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        calls.append({"base": base, "method": method, "path": path, "body": body, "request_id": request_id})
        return {"ok": True}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(["--request-id", "member-list-1", "member-list", "project_example", "--active-only"])
    main([
        "member-create", "project_example", "grok-build", "Grok Build",
        "--kind", "agent", "--role", "executor", "--metadata-json", '{"vendor":"Tencent"}',
    ])
    main([
        "member-update", "project_example", "member_example", "--status", "suspended",
        "--role", "reviewer", "--metadata-json", '{"reason":"review"}',
    ])
    main(["member-revoke", "project_example", "member_example"])

    assert calls[0]["method"] == "GET"
    assert calls[0]["path"].endswith("/members?include_revoked=false")
    assert calls[1]["method"] == "POST"
    assert calls[1]["body"]["metadata"] == {"vendor": "Tencent"}
    assert calls[2]["method"] == "PATCH"
    assert calls[2]["body"]["status"] == "suspended"
    assert calls[3]["method"] == "DELETE"
    assert calls[3]["path"].endswith("/members/member_example")
    assert capsys.readouterr().out.count('"ok": true') == 4


def test_handoff_and_integration_cli_send_structured_payloads(monkeypatch, capsys):
    calls = []

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        calls.append(
            {
                "base": base,
                "method": method,
                "path": path,
                "body": body,
                "request_id": request_id,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(
        [
            "task-handoff",
            "project_example",
            "task_example",
            "Core implementation complete",
            "--from-session-id",
            "agent_first",
            "--token",
            "session-token",
            "--to-session-id",
            "agent_second",
            "--completed",
            "Service layer",
            "--pending",
            "Integration tests",
            "--file",
            "src/service.py",
            "--risk",
            "Compatibility",
            "--next-step",
            "Run integration tests",
        ]
    )
    main(
        [
            "integration-submit",
            "project_example",
            "task_example",
            "Merged and verified",
            "--session-id",
            "agent_second",
            "--token",
            "session-token",
            "--result",
            "done",
            "--file",
            "src/service.py",
            "--test",
            "pytest::0",
            "--commit",
            "abc123",
        ]
    )

    assert calls[0]["path"].endswith("/tasks/task_example/handoffs")
    assert calls[0]["body"]["completed_items"] == ["Service layer"]
    assert calls[0]["body"]["next_step"] == "Run integration tests"
    assert calls[1]["path"].endswith("/tasks/task_example/integrations")
    assert calls[1]["body"]["tests"] == [{"command": "pytest", "exit_code": 0}]
    assert calls[1]["body"]["commit_hash"] == "abc123"
    assert capsys.readouterr().out.count('"ok": true') == 2


def test_room_leave_cli_forwards_explicit_request_id(monkeypatch, capsys):
    captured = {}

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        captured.update(
            {
                "base": base,
                "method": method,
                "path": path,
                "body": body,
                "request_id": request_id,
            }
        )
        return {"ok": True, "result": {"agent": {"status": "offline"}}}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(
        [
            "--request-id",
            "cli-leave-1",
            "room-leave",
            "project_example",
            "--session-id",
            "agent_example",
            "--token",
            "session-token",
        ]
    )

    assert captured["method"] == "POST"
    assert captured["path"] == (
        "/api/v1/projects/project_example/agents/agent_example/leave"
    )
    assert captured["body"] == {"token": "session-token"}
    assert captured["request_id"] == "cli-leave-1"
    assert '"status": "offline"' in capsys.readouterr().out


def test_room_join_cli_forwards_stable_agent_key_and_model(monkeypatch, capsys):
    captured = {}

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        captured.update({"method": method, "path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(
        [
            "room-join",
            "project_example",
            "--agent-key",
            "codex-main",
            "--name",
            "Codex",
            "--client",
            "codex",
            "--model",
            "unknown",
        ]
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/projects/project_example/agents/join"
    assert captured["body"]["agent_key"] == "codex-main"
    assert captured["body"]["model"] == "unknown"
    assert '"ok": true' in capsys.readouterr().out


def test_message_post_cli_forwards_message_level_model(monkeypatch, capsys):
    captured = {}

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        captured.update(
            {
                "method": method,
                "path": path,
                "body": body,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    main(
        [
            "message-post",
            "project_example",
            "Review complete",
            "--session-id",
            "agent_example",
            "--token",
            "session-token",
            "--model-display-name",
            "WorkBuddy UI Model",
        ]
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/projects/project_example/messages"
    assert captured["body"]["model_display_name"] == "WorkBuddy UI Model"
    assert '"ok": true' in capsys.readouterr().out
