from __future__ import annotations

import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.config import Settings, load_settings
from agentchatroom.contracts import REMOTE_COORDINATOR_SCOPES
from agentchatroom.remote_auth import secret_hash
from agentchatroom.remote_mcp import create_remote_app
from agentchatroom.errors import DomainError
from tests.test_nested_repositories import claim, git, report, repository


@pytest.fixture
def remote(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", remote_mcp_public_base_url="https://room.example")
    local_app = create_app(settings)
    public_app = create_remote_app(local_app.state.service, local_app.state.remote_provider)
    local = TestClient(local_app)
    with TestClient(public_app, base_url="https://room.example") as public:
        yield local, public, local_app.state.service, public_app
    local.close()
    local_app.state.service.close()


def pending_authorization(local, public, scopes=REMOTE_COORDINATOR_SCOPES):
    registered = public.post("/register", json={
        "client_name": "Cloud test", "redirect_uris": ["https://client.example/callback"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": " ".join(scopes),
    })
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = public.get("/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.example/callback", "state": "state-check",
        "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": " ".join(scopes), "resource": "https://room.example/mcp",
    }, follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text
    consent_url = response.headers["location"]
    query = parse_qs(urlparse(consent_url).query)
    request_id = query["request_id"][0]
    record = next(item for item in local.get("/api/v1/admin/remote").json()["pending"] if item["request_id"] == request_id)
    return client_id, verifier, consent_url, record


def authorize(local, public, scopes=REMOTE_COORDINATOR_SCOPES):
    client_id, verifier, url, record = pending_authorization(local, public, scopes)
    response = local.post(f'/api/v1/admin/remote/authorizations/{record["request_id"]}/decision', json={
        "approve": True, "verification_code": record["verification_code"], "display_name": "Cloud test",
    })
    assert response.status_code == 200, response.text
    complete = public.get(url.replace("/oauth/consent?", "/oauth/complete?"), follow_redirects=False)
    query = parse_qs(urlparse(complete.headers["location"]).query)
    assert query["state"] == ["state-check"]
    token_form = {"grant_type": "authorization_code", "client_id": client_id, "code": query["code"][0],
                  "redirect_uri": "https://client.example/callback", "code_verifier": verifier,
                  "resource": "https://room.example/mcp"}
    tokens = public.post("/token", data=token_form)
    assert tokens.status_code == 200, tokens.text
    return tokens.json(), client_id, token_form


def rpc(public, token, method, params=None):
    return public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    })


def call(public, token, name, **arguments):
    response = rpc(public, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_discovery_unauthenticated_mcp_and_management_boundary(remote):
    _local, public, _service, _app = remote
    metadata = public.get("/.well-known/oauth-authorization-server").json()
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert public.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == "https://room.example/mcp"
    response = public.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401 and "resource_metadata" in response.headers["www-authenticate"]
    for path in ("/", "/api/v1/admin/remote", "/api/v1/projects"):
        assert public.get(path).status_code == 404


def test_pending_cannot_connect_wrong_code_denied_and_expired(remote):
    local, public, service, _app = remote
    _client, _verifier, url, record = pending_authorization(local, public)
    assert public.get(url).status_code == 200
    assert public.get(url.replace("consent?", "complete?"), follow_redirects=False).status_code == 200
    wrong = local.post(f'/api/v1/admin/remote/authorizations/{record["request_id"]}/decision', json={"approve": True, "verification_code": "bad"})
    assert wrong.status_code == 403
    assert local.post(f'/api/v1/admin/remote/authorizations/{record["request_id"]}/decision', json={"approve": False}).status_code == 200
    denied = public.get(url.replace("consent?", "complete?"), follow_redirects=False)
    assert parse_qs(urlparse(denied.headers["location"]).query)["error"] == ["access_denied"]
    _client, _verifier, url, record = pending_authorization(local, public)
    with service.database.connect(write=True) as connection:
        connection.execute("UPDATE remote_oauth_authorizations SET expires_at=0 WHERE id=?", (record["request_id"],))
    assert "过期" in public.get(url).text
    assert local.post(f'/api/v1/admin/remote/authorizations/{record["request_id"]}/decision', json={"approve": True}).status_code == 409


def test_code_resource_pkce_single_use_hash_storage_rotation_and_revocation(remote):
    local, public, service, _app = remote
    tokens, client_id, form = authorize(local, public)
    assert public.post("/token", data=form).status_code == 400
    assert public.post("/token", data={**form, "resource": "https://wrong.example/mcp"}).status_code == 400
    with service.database.connect() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM remote_oauth_tokens").fetchall()]
        assert all(tokens["access_token"] not in json.dumps(row) and tokens["refresh_token"] not in json.dumps(row) for row in rows)
        assert {row["token_hash"] for row in rows} == {secret_hash(tokens["access_token"]), secret_hash(tokens["refresh_token"])}
    assert call(public, tokens["access_token"], "project_list")["ok"]
    refresh_form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"], "resource": "https://room.example/mcp"}
    refreshed = public.post("/token", data=refresh_form)
    assert refreshed.status_code == 200, refreshed.text
    assert public.post("/token", data=refresh_form).status_code == 400
    assert rpc(public, tokens["access_token"], "tools/list").status_code == 401
    new_token = refreshed.json()["access_token"]
    assert call(public, new_token, "project_list")["ok"]
    assert local.post(f"/api/v1/admin/remote/clients/{client_id}/revoke").status_code == 200
    assert rpc(public, new_token, "tools/list").status_code == 401
    assert public.post("/token", data={**refresh_form, "refresh_token": refreshed.json()["refresh_token"]}).status_code in (400, 401)


def test_cross_project_messages_tasks_no_execution_tools_or_local_paths(remote, tmp_path):
    local, public, service, _app = remote
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(); b_dir.mkdir()
    a = service.create_project(root_path=str(a_dir), name="Alpha")
    b = service.create_project(root_path=str(b_dir), name="Beta")
    tokens, _client, _form = authorize(local, public)
    token = tokens["access_token"]
    listed = rpc(public, token, "tools/list").json()["result"]["tools"]
    names = {item["name"] for item in listed}
    assert {"task_create", "task_assign", "review_submit", "message_post"}.issubset(names)
    assert not names.intersection({"work_report", "task_claim", "lease_acquire", "integration_submit", "task_update", "session_heartbeat"})
    projects = call(public, token, "project_list")["result"]["projects"]
    assert {item["id"] for item in projects} == {a["id"], b["id"]}
    assert str(tmp_path) not in json.dumps(projects)
    for project in (a, b):
        assert call(public, token, "message_post", project_id=project["id"], body="remote hello", model_display_name="unknown")["ok"]
        task = call(public, token, "task_create", project_id=project["id"], title="Planned task", description="For a local executor", acceptance_criteria=["Done correctly"])
        assert task["ok"], task
        assert task["result"]["task"]["owner_session_id"] is None
        sync = call(public, token, "room_sync", project_id=project["id"])
        assert sync["ok"], sync
        assert "remote hello" in json.dumps(sync)
        assert str(project["root_path"]) not in json.dumps(sync)
    assert not call(public, token, "message_post", project_id="missing", body="no", model_display_name="unknown")["ok"]


def test_read_only_oauth_scope_cannot_publish_or_post(remote, tmp_path):
    local, public, service, _app = remote
    folder = tmp_path / "project"; folder.mkdir()
    project = service.create_project(root_path=str(folder))
    tokens, _client, _form = authorize(local, public, ("room:read",))
    assert call(public, tokens["access_token"], "project_list")["ok"]
    denied = call(public, tokens["access_token"], "task_create", project_id=project["id"], title="denied", description="", acceptance_criteria=["x"])
    assert denied["error"]["code"] == "remote_scope_required"
    assert service.list_tasks(project["id"]) == []


def test_repository_review_matches_latest_full_sha_and_independent_identity(remote, tmp_path):
    local, public, service, _app = remote
    root = tmp_path / "repository"
    sha = repository(root)
    git(root, "remote", "add", "origin", "https://github.com/example/room-test.git")
    project = service.create_project(root_path=str(root), name="Review fixture")
    task, joined = claim(service, project)
    result = report(service, project, task, joined, sha)
    report_id = service.get_task_evidence(project["id"], task["id"])["latest_work_report_id"]
    tokens, _client, _form = authorize(local, public)
    token = tokens["access_token"]
    fetched = call(public, token, "task_get", project_id=project["id"], task_id=task["id"])
    assert fetched["result"]["latest_work_report_id"] == report_id
    assert str(root) not in json.dumps(fetched)
    evidence = {"work_report_id": report_id, "commit_hash": sha, "repository_url": "https://github.com/example/room-test",
                "evidence_urls": [f"https://github.com/example/room-test/commit/{sha}"]}
    with pytest.raises(DomainError) as self_review:
        service.submit_review(project["id"], task["id"], reviewer_session_id=joined["agent"]["id"], token=joined["token"],
            verdict="approved", criteria=[{"criterion": task["acceptance_criteria"][0], "status": "passed", "evidence": "self"}],
            repository_evidence=evidence)
    assert self_review.value.code == "reviewer_not_independent"
    arguments = {"project_id": project["id"], "task_id": task["id"], "verdict": "approved",
                 "criteria": [{"criterion": task["acceptance_criteria"][0], "status": "passed", "evidence": "Inspected exact code and check result"}],
                 "repository_evidence": evidence}
    for mutation, code in (
        ({"work_report_id": "stale"}, "review_report_changed"),
        ({"commit_hash": sha[:8]}, "review_commit_required"),
        ({"commit_hash": "a" * 40}, "review_commit_mismatch"),
        ({"repository_url": "https://github.com/foreign/repo"}, "review_repository_mismatch"),
        ({"evidence_urls": ["https://github.com/foreign/repo/commit/" + sha]}, "review_evidence_repository_mismatch"),
    ):
        rejected = call(public, token, "review_submit", **{**arguments, "repository_evidence": {**evidence, **mutation}})
        assert rejected["error"]["code"] == code, rejected
    accepted = call(public, token, "review_submit", **arguments)
    assert accepted["ok"], accepted
    assert accepted["result"]["verification_status"] == "approved"
    assert accepted["result"]["integration_status"] == "pending"
    with service.database.connect() as connection:
        event = connection.execute("SELECT payload_json FROM events WHERE event_type='review.submitted' AND task_id=?", (task["id"],)).fetchone()
        assert json.loads(event["payload_json"])["repository_evidence"]["work_report_id"] == report_id


def test_remote_settings_persist_through_formal_management_endpoint(remote, tmp_path, monkeypatch):
    local, _public, service, _app = remote
    saved = local.put("/api/v1/admin/remote/settings", json={"enabled": True, "host": "127.0.0.1", "port": 19002, "public_base_url": "https://published.example"})
    assert saved.status_code == 200 and saved.json()["restart_required"]
    loaded = load_settings(service.settings.data_dir / "config.toml", data_dir=service.settings.data_dir)
    assert loaded.remote_mcp_enabled and loaded.remote_mcp_port == 19002
    monkeypatch.setenv("AGENTCHATROOM_REMOTE_MCP_PORT", "19003")
    assert load_settings(service.settings.data_dir / "config.toml", data_dir=service.settings.data_dir).remote_mcp_port == 19003
    for changes in ({"host": "0.0.0.0"}, {"public_base_url": "http://insecure.example"}, {"port": 8765}):
        assert local.put("/api/v1/admin/remote/settings", json={**saved.json()["configuration"], **changes}).status_code == 422


def test_oauth_database_calls_run_off_event_loop(remote, monkeypatch):
    local, public, service, app = remote
    loop_threads = []
    original = app.state.provider._load_token
    def wrapped(*args, **kwargs):
        import asyncio
        try:
            asyncio.get_running_loop()
            loop_threads.append(threading.get_ident())
        except RuntimeError:
            pass
        return original(*args, **kwargs)
    monkeypatch.setattr(app.state.provider, "_load_token", wrapped)
    tokens, _client, _form = authorize(local, public)
    assert call(public, tokens["access_token"], "project_list")["ok"]
    assert loop_threads == []


def test_invalid_pkce_and_resource_do_not_consume_valid_code(remote):
    local, public, _service, _app = remote
    client_id, verifier, url, record = pending_authorization(local, public)
    assert local.post(f'/api/v1/admin/remote/authorizations/{record["request_id"]}/decision', json={
        "approve": True, "verification_code": record["verification_code"],
    }).status_code == 200
    complete = public.get(url.replace("consent?", "complete?"), follow_redirects=False)
    code = parse_qs(urlparse(complete.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": "https://client.example/callback", "code_verifier": verifier,
            "resource": "https://room.example/mcp"}
    assert public.post("/token", data={**form, "code_verifier": "w" * 64}).json()["error"] == "invalid_grant"
    assert public.post("/token", data={**form, "resource": "https://foreign.example/mcp"}).status_code == 400
    assert public.post("/token", data={**form, "redirect_uri": "https://foreign.example/callback"}).status_code == 400
    assert public.post("/token", data=form).status_code == 200


def test_concurrent_code_redemption_only_mints_one_token_pair(remote):
    local, public, _service, _app = remote
    client_id, verifier, url, record = pending_authorization(local, public)
    local.post(f'/api/v1/admin/remote/authorizations/{record["request_id"]}/decision', json={
        "approve": True, "verification_code": record["verification_code"],
    })
    completed = public.get(url.replace("consent?", "complete?"), follow_redirects=False)
    code = parse_qs(urlparse(completed.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": "https://client.example/callback", "code_verifier": verifier,
            "resource": "https://room.example/mcp"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: public.post("/token", data=form).status_code, range(2)))
    assert sorted(statuses) == [200, 400]


def test_oauth_persistence_expiry_revoke_endpoint_and_session_recovery(remote, tmp_path):
    local, public, service, app = remote
    tokens, client_id, _form = authorize(local, public)
    token = tokens["access_token"]
    from agentchatroom.remote_auth import RemoteOAuthProvider
    renewed_app = create_remote_app(service, RemoteOAuthProvider(service))
    with TestClient(renewed_app, base_url="https://room.example") as renewed:
        assert call(renewed, token, "project_list")["ok"]
    folder = tmp_path / "cache-recovery"; folder.mkdir()
    project = service.create_project(root_path=str(folder))
    assert call(public, token, "room_sync", project_id=project["id"])["ok"]
    cached = next(iter(app.state.coordinator.sessions.values()))
    original_identity = cached["identity"]["id"]
    with service.database.connect(write=True) as connection:
        connection.execute("UPDATE agent_sessions SET token_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (cached["agent"]["id"],))
    assert call(public, token, "message_post", project_id=project["id"], body="after expiry", model_display_name="unknown")["ok"]
    recovered = next(iter(app.state.coordinator.sessions.values()))
    assert recovered["identity"]["id"] == original_identity and recovered["agent"]["id"] != cached["agent"]["id"]
    revoked = public.post("/revoke", data={"client_id": client_id, "token": tokens["refresh_token"], "token_type_hint": "refresh_token"})
    assert revoked.status_code == 200
    assert rpc(public, token, "tools/list").status_code == 401


def test_remote_approval_requires_local_management_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_REMOTE_TEST_ADMIN", "x" * 32)
    settings = Settings(data_dir=tmp_path, management_auth_required=True, management_token_env="ROOM_REMOTE_TEST_ADMIN")
    app = create_app(settings)
    with TestClient(app) as local:
        assert local.get("/api/v1/admin/remote").status_code == 401
        assert local.put("/api/v1/admin/remote/settings", json={"enabled": False, "host": "127.0.0.1", "port": 18766, "public_base_url": ""}).status_code == 401
        assert local.post("/api/v1/admin/remote/authorizations/unknown/decision", json={"approve": True}).status_code == 401
        assert local.get("/api/v1/admin/remote", headers={"Authorization": "Bearer " + "x" * 32}).status_code == 200


def test_actual_second_listener_starts_and_stops_with_main_app(tmp_path):
    import socket
    import httpx
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(data_dir=tmp_path, remote_mcp_enabled=True, remote_mcp_port=port,
                        remote_mcp_public_base_url="https://listener.example")
    app = create_app(settings)
    with TestClient(app) as local, httpx.Client(trust_env=False, timeout=3) as client:
        assert local.get("/api/v1/admin/remote").json()["running"]
        assert client.get(f"http://127.0.0.1:{port}/health").json()["mode"] == "remote_coordinator"
        assert client.get(f"http://127.0.0.1:{port}/api/v1/admin/remote").status_code == 404
        assert client.post(f"http://127.0.0.1:{port}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    assert not app.state.remote_runtime.thread.is_alive()
    with httpx.Client(trust_env=False, timeout=1) as client, pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        client.get(f"http://127.0.0.1:{port}/health")
