from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app


def test_health_and_project_room_flow(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "ok"

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json()["database"]["schema_version"] >= 1

        public_config = client.get("/api/v1/config/public")
        assert public_config.status_code == 200
        assert public_config.json()["deployment_profile"] == settings.deployment_profile
        assert public_config.json()["domain"]["schema_version"] == 5
        assert public_config.json()["domain"]["agent_connection_statuses"] == [
            "connected",
            "disconnected",
        ]
        assert public_config.json()["domain"]["model_display_name_max_length"] == 160
        assert "member:write" in public_config.json()["domain"]["agent_permissions"]
        assert "completed" in public_config.json()["domain"][
            "task_execution_statuses"
        ]
        assert "approved" in public_config.json()["domain"][
            "task_verification_statuses"
        ]
        assert "integration:write" in public_config.json()["domain"][
            "agent_permissions"
        ]
        assert public_config.json()["domain"]["project_member_schema_version"] == 1
        assert set(public_config.json()["domain"]["project_member_statuses"]) == {
            "invited",
            "active",
            "suspended",
            "revoked",
        }
        assert public_config.json()["proxy"] == {
            "trusted_headers": settings.trusted_proxy_headers,
            "trusted_ips_configured": bool(settings.trusted_proxy_ips),
        }

        integration = client.get("/api/v1/integrations/mcp")
        assert integration.status_code == 200
        assert integration.json()["schema_version"] == 3
        assert integration.json()["transport"] == "stdio"
        assert integration.json()["generic_json"]["mcpServers"]["agentchatroom"][
            "env"
        ]["AGENTCHATROOM_DATA_DIR"] == str(settings.data_dir)
        assert "[mcp_servers.agentchatroom]" in integration.json()["codex_toml"]
        assert integration.json()["profiles"]["workbuddy"]["vendor"] == "Tencent"
        assert "[mcp_servers.agentchatroom]" in integration.json()["profiles"]["grok_build"]["config_text"]
        assert integration.json()["transports"]["streamable_http"]["enabled"] is True
        assert "bearer_token_env_var" in integration.json()["codex_streamable_http_toml"]
        assert "<paste-issued-agent-token>" in integration.json()["streamable_http_json_text"]

        created = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "API Project"},
        )
        assert created.status_code == 201
        project = created.json()

        project_integration = client.get(
            f"/api/v1/projects/{project['id']}/integrations/mcp"
        )
        assert project_integration.status_code == 200
        assert "project_key" not in project_integration.json()["project"]
        assert project["project_key"] not in project_integration.json()[
            "profiles"
        ]["workbuddy"]["project_memory_text"]
        assert "workbuddy-main" in project_integration.json()["profiles"][
            "workbuddy"
        ]["onboarding_prompts"]["local"]
        assert "<paste-issued-agent-token>" in project_integration.json()["profiles"][
            "workbuddy"
        ]["onboarding_prompts"]["http"]

        joined = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "codex-main",
                "name": "Codex",
                "client": "codex",
                "model": "unknown",
                "capabilities": {"mcp": True},
            },
        )
        assert joined.status_code == 201
        agent = joined.json()

        task = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "API task",
                "acceptance_criteria": ["Endpoint works"],
                "priority": 1,
            },
        )
        assert task.status_code == 201
        task_id = task.json()["task"]["id"]

        fetched = client.get(
            f"/api/v1/projects/{project['id']}/tasks/{task_id}"
        )
        assert fetched.status_code == 200
        assert fetched.json()["task"]["id"] == task_id

        claim = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task_id}/claim",
            json={"session_id": agent["agent"]["id"], "token": agent["token"]},
        )
        assert claim.status_code == 200
        assert claim.json()["task"]["status"] == "claimed"
        message = client.post(
            f"/api/v1/projects/{project['id']}/messages",
            json={
                "body": "Working now",
                "session_id": agent["agent"]["id"],
                "token": agent["token"],
                "model_display_name": "Codex UI Model",
                "task_id": task_id,
            },
        )
        assert message.status_code == 201

        snapshot = client.get(f"/api/v1/projects/{project['id']}/snapshot")
        assert snapshot.status_code == 200
        assert len(snapshot.json()["agents"]) == 1
        assert snapshot.json()["tasks"][0]["status"] == "claimed"

        events = client.get(
            f"/api/v1/projects/{project['id']}/events", params={"after": 0}
        )
        assert events.status_code == 200
        posted_event = next(
            event
            for event in events.json()["events"]
            if event["event_type"] == "message.message"
        )
        assert posted_event["payload"]["model_display_name"] == "Codex UI Model"

        synced = client.post(
            f"/api/v1/projects/{project['id']}/sync",
            json={
                "session_id": agent["agent"]["id"],
                "token": agent["token"],
                "after": agent["cursor"],
            },
        )
        assert synced.status_code == 200
        assert synced.json()["unread_count"] == 0

        configured = client.patch(
            f"/api/v1/projects/{project['id']}",
            json={
                "settings": {
                    "lease_conflict_policy": "advisory",
                    "roles": ["custom-role"],
                    "extensions": {},
                }
            },
        )
        assert configured.status_code == 200
        assert configured.json()["project"]["settings"]["roles"] == ["custom-role"]

        exported = client.get(f"/api/v1/projects/{project['id']}/export")
        assert exported.status_code == 200
        assert exported.headers["content-disposition"].startswith("attachment;")
        assert exported.json()["project"]["id"] == project["id"]


def test_api_project_member_management_and_token_link(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Members API"},
        ).json()
        created = client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={
                "member_key": "codex-main",
                "name": "Codex Main",
                "kind": "agent",
                "role": "coordinator",
                "metadata": {"team": "core"},
            },
        )
        assert created.status_code == 201
        member = created.json()["member"]
        assert member["schema_version"] == 1

        token = client.post(
            f"/api/v1/projects/{project['id']}/agent-tokens",
            json={
                "name": "Codex token",
                "member_id": member["id"],
                "permissions": ["room:join", "room:read"],
                "expires_in_seconds": 3600,
            },
        )
        assert token.status_code == 201
        assert token.json()["credential"]["member_id"] == member["id"]

        listed = client.get(f"/api/v1/projects/{project['id']}/members")
        assert listed.status_code == 200
        assert listed.json()["members"][0]["credential_count"] == 1

        updated = client.patch(
            f"/api/v1/projects/{project['id']}/members/{member['id']}",
            json={"status": "suspended", "role": "reviewer"},
        )
        assert updated.status_code == 200
        assert updated.json()["member"]["status"] == "suspended"

        revoked = client.delete(
            f"/api/v1/projects/{project['id']}/members/{member['id']}"
        )
        assert revoked.status_code == 200
        assert revoked.json()["member"]["status"] == "revoked"


def test_health_ready_hides_database_failures(settings, monkeypatch):
    app = create_app(settings)

    def broken_connect(*, write=False):
        raise RuntimeError("postgresql://secret.example.invalid/room")

    monkeypatch.setattr(app.state.service.database, "connect", broken_connect)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["database"] == {"status": "unavailable"}
    assert "secret.example" not in response.text


def test_admin_runtime_reports_effective_paths_and_redacts_log_values(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "server.log").write_text(
        "Authorization: Bearer acr.secret-value token=another-secret\n",
        encoding="utf-8",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/admin/runtime?lines=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["database_backend"] == "sqlite"
    assert payload["paths"]["log_path"].endswith("server.log")
    assert payload["log"]["returned"] == 1
    assert "secret-value" not in payload["log"]["lines"][0]
    assert "another-secret" not in payload["log"]["lines"][0]
    assert "[REDACTED]" in payload["log"]["lines"][0]

    effective = client.get("/api/v1/runtime/effective-config")
    assert effective.status_code == 200
    assert effective.json()["settings"]["database_backend"] == "sqlite"
    assert "log" not in effective.json()


def test_api_can_assign_and_acknowledge_task(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Assignment Project"},
        ).json()
        coordinator = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "coordinator-main",
                "name": "Coordinator",
                "client": "codex",
                "model": "unknown",
                "role": "coordinator",
            },
        ).json()
        worker = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "name": "Worker",
                "agent_key": "worker-main",
                "client": "grok-build",
                "model": "test-model",
                "role": "executor",
                "capabilities": {"python": True},
            },
        ).json()
        task = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "Implement adapter",
                "acceptance_criteria": ["Adapter passes"],
            },
        ).json()["task"]

        assigned = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/assignments",
            json={
                "assigned_by_session_id": coordinator["agent"]["id"],
                "token": coordinator["token"],
                "assigned_to_session_id": worker["agent"]["id"],
                "required_capability": "python",
                "note": "Please implement",
            },
        )
        assert assigned.status_code == 201
        assignment = assigned.json()["assignment"]
        assert assignment["status"] == "pending"

        acknowledged = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/assignments/"
            f"{assignment['id']}/acknowledge",
            json={
                "session_id": worker["agent"]["id"],
                "token": worker["token"],
                "response": "accepted",
                "note": "Starting now",
            },
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["assignment"]["status"] == "accepted"
        assert acknowledged.json()["task"]["owner_session_id"] == worker["agent"]["id"]
        assert acknowledged.json()["task"]["execution_status"] == "claimed"


def test_api_handoff_review_and_integration_are_explicit(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Lifecycle Project"},
        ).json()
        first = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "first-main",
                "name": "First",
                "client": "codex",
                "model": "unknown",
                "role": "executor",
            },
        ).json()
        second = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "second-main",
                "name": "Second",
                "client": "grok-build",
                "model": "test-model",
                "role": "executor",
            },
        ).json()
        task = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "Complete lifecycle",
                "acceptance_criteria": ["Lifecycle passes"],
            },
        ).json()["task"]
        client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/claim",
            json={"session_id": first["agent"]["id"], "token": first["token"]},
        )

        requested = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/handoffs",
            json={
                "to_session_id": second["agent"]["id"],
                "summary": "Initial investigation complete",
                "completed_items": ["Investigation"],
                "pending_items": ["Implementation"],
                "files": ["src/lifecycle.py"],
                "risks": ["Needs integration test"],
                "next_step": "Implement the lifecycle",
            },
        )
        assert requested.status_code == 201
        handoff_id = requested.json()["handoff"]["id"]
        accepted = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/handoffs/"
            f"{handoff_id}/acknowledge",
            json={
                "session_id": second["agent"]["id"],
                "token": second["token"],
                "response": "accepted",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["task"]["owner_session_id"] == second["agent"]["id"]

        report = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/reports",
            json={
                "session_id": second["agent"]["id"],
                "token": second["token"],
                "summary": "Lifecycle implemented",
                "files": ["src/lifecycle.py"],
                "tests": [{"command": "pytest lifecycle", "exit_code": 0}],
            },
        )
        assert report.status_code == 201
        review = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/reviews",
            json={
                "reviewer_session_id": first["agent"]["id"],
                "token": first["token"],
                "verdict": "approved",
                "criteria": [{"criterion": "Lifecycle passes", "status": "passed"}],
            },
        )
        assert review.status_code == 201

        direct_done = client.patch(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}",
            json={"status": "done"},
        )
        assert direct_done.status_code == 409
        assert direct_done.json()["error"]["code"] == "structured_transition_required"

        integrated = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/integrations",
            json={
                "integrator_session_id": first["agent"]["id"],
                "token": first["token"],
                "result": "done",
                "summary": "Merged and verified",
                "files": ["src/lifecycle.py"],
                "tests": [{"command": "pytest lifecycle", "exit_code": 0}],
                "commit_hash": "abc123",
            },
        )
        assert integrated.status_code == 201
        assert integrated.json()["task"]["status"] == "done"
        assert integrated.json()["task"]["integration_status"] == "done"

        snapshot = client.get(
            f"/api/v1/projects/{project['id']}/snapshot"
        ).json()
        stored = snapshot["tasks"][0]
        assert stored["handoffs"][0]["status"] == "accepted"
        assert stored["integrations"][0]["result"] == "done"


def test_api_allows_approved_task_handoff_before_integration(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Approved Handoff Project"},
        ).json()
        executor = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "executor-main",
                "name": "Executor",
                "client": "grok-build",
                "model": "test-model",
                "role": "executor",
            },
        ).json()
        reviewer = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "reviewer-main",
                "name": "Reviewer",
                "client": "codex",
                "model": "unknown",
                "role": "reviewer",
            },
        ).json()
        integrator = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "integrator-main",
                "name": "Integrator",
                "client": "codex",
                "model": "unknown",
                "role": "integrator",
            },
        ).json()
        task = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "Approved handoff",
                "acceptance_criteria": ["Behavior is verified"],
            },
        ).json()["task"]
        client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/claim",
            json={"session_id": executor["agent"]["id"], "token": executor["token"]},
        )
        client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/reports",
            json={
                "session_id": executor["agent"]["id"],
                "token": executor["token"],
                "summary": "Implementation complete",
                "files": ["src/approved.py"],
                "tests": [{"command": "pytest approved", "exit_code": 0}],
            },
        )
        review = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/reviews",
            json={
                "reviewer_session_id": reviewer["agent"]["id"],
                "token": reviewer["token"],
                "verdict": "approved",
                "criteria": [{"criterion": "Behavior is verified", "status": "passed"}],
            },
        )
        assert review.status_code == 201

        requested = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/handoffs",
            json={
                "from_session_id": executor["agent"]["id"],
                "token": executor["token"],
                "to_session_id": integrator["agent"]["id"],
                "summary": "Independent review approved the implementation",
                "pending_items": ["Integration"],
                "next_step": "Run integration tests",
            },
        )
        assert requested.status_code == 201
        handoff_id = requested.json()["handoff"]["id"]
        accepted = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/handoffs/"
            f"{handoff_id}/acknowledge",
            json={
                "session_id": integrator["agent"]["id"],
                "token": integrator["token"],
                "response": "accepted",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["task"]["execution_status"] == "completed"
        assert accepted.json()["task"]["verification_status"] == "approved"
        assert accepted.json()["task"]["integration_status"] == "pending"

        integrated = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/integrations",
            json={
                "integrator_session_id": integrator["agent"]["id"],
                "token": integrator["token"],
                "result": "done",
                "summary": "Integration tests passed",
                "files": ["src/approved.py"],
                "tests": [{"command": "pytest approved", "exit_code": 0}],
            },
        )
        assert integrated.status_code == 201
        assert integrated.json()["task"]["status"] == "done"


def test_api_accepts_no_code_work_report(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        ).json()
        agent = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "investigator-main",
                "name": "Investigator",
                "client": "grok-build",
                "model": "test-model",
            },
        ).json()
        task = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "Read-only investigation",
                "acceptance_criteria": ["Findings are documented"],
            },
        ).json()["task"]
        client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/claim",
            json={"session_id": agent["agent"]["id"], "token": agent["token"]},
        )

        response = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/reports",
            json={
                "session_id": agent["agent"]["id"],
                "token": agent["token"],
                "summary": "Investigation complete",
                "files": [],
                "tests": [{"command": "Manual evidence review", "exit_code": 0}],
                "no_code_change_reason": "The task only required source inspection",
            },
        )

        assert response.status_code == 201
        assert response.json()["execution_status"] == "completed"
        snapshot = client.get(
            f"/api/v1/projects/{project['id']}/snapshot"
        ).json()
        assert snapshot["reports"][0]["files"] == []
        assert snapshot["reports"][0]["no_code_change_reason"] == (
            "The task only required source inspection"
        )


def test_api_manages_agent_tokens_and_workspaces(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        ).json()
        issued = client.post(
            f"/api/v1/projects/{project['id']}/agent-tokens",
            json={
                "name": "Remote agents",
                "permissions": ["room:join", "room:read", "task:write"],
                "expires_in_seconds": 3600,
            },
        )
        assert issued.status_code == 201
        credential = issued.json()["credential"]
        token = issued.json()["token"]
        assert token.startswith(f"acr.{credential['id']}.")
        assert "token_hash" not in credential

        listed = client.get(
            f"/api/v1/projects/{project['id']}/agent-tokens"
        ).json()["credentials"]
        assert listed[0]["id"] == credential["id"]
        assert "token" not in listed[0]
        assert "token_hash" not in listed[0]

        registered = client.post(
            f"/api/v1/projects/{project['id']}/workspaces",
            json={
                "host_key": "desktop-fingerprint",
                "host_name": "Desktop",
                "local_path": "D:/work/project",
                "branch": "main",
            },
        )
        assert registered.status_code == 201
        assert registered.json()["host"]["host_key"] == "desktop-fingerprint"
        assert registered.json()["workspace"]["local_path"] == "D:/work/project"
        workspaces = client.get(
            f"/api/v1/projects/{project['id']}/workspaces"
        ).json()["workspaces"]
        assert workspaces[0]["host_name"] == "Desktop"

        audit = client.get(
            f"/api/v1/projects/{project['id']}/audit",
            params={"event_type": "credential.issued"},
        )
        assert audit.status_code == 200
        assert audit.json()["events"][0]["payload"]["credential_id"] == credential["id"]

        rotated = client.post(
            f"/api/v1/projects/{project['id']}/agent-tokens/{credential['id']}/rotate",
            json={"expires_in_seconds": 3600},
        )
        assert rotated.status_code == 201
        replacement = rotated.json()["credential"]
        assert rotated.json()["token"].startswith(f"acr.{replacement['id']}.")
        assert rotated.json()["previous_credential"]["active"] is False

        revoked = client.delete(
            f"/api/v1/projects/{project['id']}/agent-tokens/{replacement['id']}"
        )
        assert revoked.status_code == 200
        assert revoked.json()["credential"]["active"] is False


def test_api_returns_stable_domain_error(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        ).json()
        response = client.post(
            f"/api/v1/projects/{project['id']}/agents/missing/heartbeat",
            json={"token": "wrong"},
        )

        assert response.status_code == 401
        assert response.json() == {
            "error": {
                "code": "invalid_session_token",
                "message": "Agent session or token is invalid",
                "details": {},
            }
        }


def test_api_heartbeat_only_caches_explicit_idempotency_keys(settings, project_dir):
    app = create_app(settings)
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        ).json()
        joined = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "presence-main",
                "name": "Presence Agent",
                "client": "codex",
                "model": "unknown",
            },
        ).json()
        heartbeat_url = (
            f"/api/v1/projects/{project['id']}/agents/"
            f"{joined['agent']['id']}/heartbeat"
        )
        body = {"token": joined["token"]}

        responses = [client.post(heartbeat_url, json=body) for _ in range(5)]

        assert all(response.status_code == 200 for response in responses)
        assert all("x-request-id" in response.headers for response in responses)
        with app.state.service.database.connect() as connection:
            uncached_count = connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE operation = ?",
                ("session.heartbeat",),
            ).fetchone()[0]
        assert uncached_count == 0

        headers = {"Idempotency-Key": "api-heartbeat-1"}
        first = client.post(heartbeat_url, json=body, headers=headers)
        replay = client.post(heartbeat_url, json=body, headers=headers)

        assert first.status_code == 200
        assert replay.status_code == 200
        assert first.json()["idempotent_replay"] is False
        assert replay.json()["idempotent_replay"] is True
        with app.state.service.database.connect() as connection:
            cached_count = connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE operation = ?",
                ("session.heartbeat",),
            ).fetchone()[0]
        assert cached_count == 1


def test_api_idempotency_header_and_session_leave(settings, project_dir):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        ).json()
        joined = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "reliable-main",
                "name": "Reliable Agent",
                "client": "codex",
                "model": "unknown",
            },
        ).json()
        message_url = f"/api/v1/projects/{project['id']}/messages"
        body = {
            "body": "Exactly once",
            "session_id": joined["agent"]["id"],
            "token": joined["token"],
            "model_display_name": "Reliable Model",
        }
        headers = {"Idempotency-Key": "api-message-1"}

        first = client.post(message_url, json=body, headers=headers)
        replay = client.post(message_url, json=body, headers=headers)
        conflict = client.post(
            message_url,
            json={**body, "body": "Different"},
            headers=headers,
        )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert first.headers["x-request-id"] == "api-message-1"
        assert first.json()["event_id"] == replay.json()["event_id"]
        assert replay.json()["idempotent_replay"] is True
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        leave = client.post(
            f"/api/v1/projects/{project['id']}/agents/{joined['agent']['id']}/leave",
            json={"token": joined["token"]},
            headers={"Idempotency-Key": "api-leave-1"},
        )
        assert leave.status_code == 200
        assert leave.json()["agent"]["status"] == "offline"
        assert leave.json()["agent"]["left_at"] is not None


def test_management_auth_protects_remote_admin_api(
    monkeypatch, settings, project_dir
):
    admin_token = "test-management-token-with-adequate-length"
    monkeypatch.setenv("TEST_AGENTCHATROOM_ADMIN_TOKEN", admin_token)
    protected_settings = replace(
        settings,
        management_auth_required=True,
        management_token_env="TEST_AGENTCHATROOM_ADMIN_TOKEN",
    )
    with TestClient(create_app(protected_settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/config/public").status_code == 200
        assert client.get("/api/v1/integrations/mcp").status_code == 401
        assert client.get("/api/v1/projects").status_code == 401
        assert client.get("/api/v1/auth/status").json() == {
            "required": True,
            "authenticated": False,
        }
        rejected = client.post(
            "/api/v1/auth/login", json={"token": "wrong-token"}
        )
        assert rejected.status_code == 401

        logged_in = client.post(
            "/api/v1/auth/login", json={"token": admin_token}
        )
        assert logged_in.status_code == 200
        assert client.get("/api/v1/auth/status").json()["authenticated"] is True
        created = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        )
        assert created.status_code == 201

        logged_out = client.post("/api/v1/auth/logout")
        assert logged_out.status_code == 200
        assert client.get("/api/v1/projects").status_code == 401

        bearer = client.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert bearer.status_code == 200


def test_management_auth_requires_configured_secret(settings):
    protected_settings = replace(
        settings,
        management_auth_required=True,
        management_token_env="MISSING_AGENTCHATROOM_ADMIN_TOKEN",
    )
    with pytest.raises(ValueError, match="at least 24 characters"):
        create_app(protected_settings)


def test_management_auth_session_persists_across_app_instances(
    monkeypatch, settings
):
    admin_token = "test-management-token-with-adequate-length"
    monkeypatch.setenv("TEST_AGENTCHATROOM_ADMIN_TOKEN", admin_token)
    protected_settings = replace(
        settings,
        management_auth_required=True,
        management_token_env="TEST_AGENTCHATROOM_ADMIN_TOKEN",
    )

    with TestClient(create_app(protected_settings)) as first:
        logged_in = first.post("/api/v1/auth/login", json={"token": admin_token})
        assert logged_in.status_code == 200
        cookie = first.cookies.get(protected_settings.management_cookie_name)
        assert cookie

    with TestClient(create_app(protected_settings)) as second:
        second.cookies.set(protected_settings.management_cookie_name, cookie)
        status = second.get("/api/v1/auth/status")
        assert status.status_code == 200
        assert status.json() == {"required": True, "authenticated": True}

        logged_out = second.post("/api/v1/auth/logout")
        assert logged_out.status_code == 200

    with TestClient(create_app(protected_settings)) as third:
        third.cookies.set(protected_settings.management_cookie_name, cookie)
        assert third.get("/api/v1/projects").status_code == 401


def test_api_can_archive_and_permanently_delete_projects(settings, tmp_path):
    archived_path = tmp_path / "archived"
    deleted_path = tmp_path / "deleted"
    archived_path.mkdir()
    deleted_path.mkdir()

    with TestClient(create_app(settings)) as client:
        archived = client.post(
            "/api/v1/projects", json={"root_path": str(archived_path)}
        ).json()
        deleted = client.post(
            "/api/v1/projects", json={"root_path": str(deleted_path)}
        ).json()

        archived_registration = archived_path / ".agentchatroom" / "project.json"
        deleted_registration = deleted_path / ".agentchatroom" / "project.json"
        assert archived_registration.is_file()
        assert deleted_registration.is_file()

        archived_response = client.delete(f"/api/v1/projects/{archived['id']}")
        deleted_response = client.delete(
            f"/api/v1/projects/{deleted['id']}", params={"permanent": True}
        )

        assert archived_response.json()["archived"] is True
        assert deleted_response.json()["deleted"] is True
        assert archived_registration.is_file()
        assert not deleted_registration.exists()
        assert deleted_response.json()["project_registration"] == {
            "removed": 1,
            "cleanup_errors": [],
        }
        assert client.get(
            f"/api/v1/projects/{deleted['id']}/snapshot"
        ).status_code == 404


def test_agent_join_requires_stable_key_and_model_but_accepts_unknown(
    settings, project_dir
):
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Identity Project"},
        ).json()
        endpoint = f"/api/v1/projects/{project['id']}/agents/join"

        missing_key = client.post(
            endpoint,
            json={"name": "Agent", "client": "generic", "model": "unknown"},
        )
        missing_model = client.post(
            endpoint,
            json={
                "agent_key": "agent-main",
                "name": "Agent",
                "client": "generic",
            },
        )
        accepted = client.post(
            endpoint,
            json={
                "agent_key": "agent-main",
                "name": "Agent",
                "client": "generic",
                "model": "unknown",
            },
        )

        assert missing_key.status_code == 422
        assert missing_model.status_code == 422
        assert accepted.status_code == 201
        assert accepted.json()["agent"]["agent_key"] == "agent-main"
