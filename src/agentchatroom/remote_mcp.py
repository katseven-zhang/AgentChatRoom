"""A separate, OAuth-only coordination surface with no local execution tools."""
from __future__ import annotations

import asyncio
import html
import threading
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import __version__
from .config import validate_remote_settings
from .contracts import REMOTE_COORDINATOR_SCOPES, RepositoryReviewEvidence, ReviewCriterion
from .errors import DomainError
from .remote_auth import RemoteOAuthProvider


PRIVATE_FIELDS = frozenset({
    "token", "token_hash", "ticket_hash", "code_hash", "metadata_json", "capabilities_json",
    "root_path", "workspace_root", "project_path", "repository_root", "repository_path",
    "worktree", "workspace_path", "local_path", "data_dir", "database_path", "file_hashes",
    "git_evidence", "system_evidence", "connection", "credentials", "leases", "workspaces", "hosts",
})


def remote_projection(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: remote_projection(item) for key, item in value.items() if key not in PRIVATE_FIELDS}
        remote = result.get("git_remote")
        if isinstance(remote, str):
            url = urlparse(remote)
            if url.username or url.password:
                host = url.hostname or ""
                if url.port:
                    host += ":" + str(url.port)
                result["git_remote"] = url._replace(netloc=host).geturl()
        return result
    if isinstance(value, list):
        return [remote_projection(item) for item in value]
    return value


class RemoteCoordinator:
    def __init__(self, service: Any, provider: RemoteOAuthProvider) -> None:
        self.service, self.provider = service, provider
        self.sessions: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.lock = threading.RLock()

    def call(self, access: Any, scope: str, project_id: str | None, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            if access is None or scope not in access.scopes:
                raise DomainError("remote_scope_required", f"The {scope} OAuth scope is required", status_code=403,
                                  details={"required_action": "reauthorize_with_the_required_coordinator_scope"})
            with self.lock, self.service.database.connect(write=True):
                token = self.provider._load_token(access.token, "access")
                if token is None:
                    raise DomainError("remote_authorization_expired", "OAuth authorization expired or was revoked", status_code=401)
                if operation == "projects":
                    projects = [{key: row.get(key) for key in ("id", "name", "git_remote", "archived_at")}
                                for row in self.service.list_projects() if not row.get("archived_at")]
                    return {"ok": True, "result": remote_projection({"mode": "remote_coordinator", "projects": projects,
                            "required_action": "use_an_explicit_project_id_for_each_project_operation"})}
                if not project_id:
                    raise DomainError("remote_project_required", "Select a Project using project_list first")
                self.service.get_project(project_id)
                key = (access.client_id, token["grant_id"], project_id)
                joined = self.sessions.get(key)
                if joined is not None:
                    try:
                        self.service.verify_session(project_id, joined["agent"]["id"], joined["token"])
                    except DomainError as error:
                        if error.code not in {"session_closed", "session_token_expired", "invalid_session_token"}:
                            raise
                        joined = None
                if joined is None or not self.service.session_active(project_id, joined["agent"]["id"]):
                    client = self.provider.client_record(access.client_id)
                    if client is None:
                        raise DomainError("remote_authorization_expired", "The OAuth client has been revoked", status_code=401)
                    joined = self.service.join_room(
                        project_id, software_key=client["software_key"], name=client["display_name"],
                        client="remote-mcp", model="unknown", role="coordinator",
                        capabilities={"remote_coordination": True}, metadata={"remote_oauth_client_id": access.client_id},
                    )
                    self.sessions[key] = joined
                session_id, session_token = joined["agent"]["id"], joined["token"]
                if operation == "sync":
                    raw = self.service.room_sync(project_id, session_id=session_id, token=session_token, mcp_context=True, **kwargs)
                    snapshot = raw.get("snapshot", {})
                    result = {key: raw.get(key) for key in ("cursor", "latest_cursor", "messages", "unread_count")}
                    result["project"] = {key: snapshot.get("project", {}).get(key) for key in ("id", "name", "git_remote")}
                    tasks = snapshot.get("tasks", [])
                    result["tasks"] = tasks[:self.service.settings.snapshot_recent_limit]
                    result["tasks_truncated"] = len(tasks) > len(result["tasks"])
                    result["members"] = [{key: row.get(key) for key in ("id", "name", "role", "status")}
                                         for row in snapshot.get("members", [])]
                    result["documents"] = snapshot.get("documents", [])
                elif operation == "post":
                    result = self.service.post_message(project_id, session_id=session_id, token=session_token, **kwargs)
                elif operation == "ack":
                    result = self.service.acknowledge_event(project_id, kwargs["event_id"], session_id, session_token)
                elif operation == "create":
                    result = self.service.create_task(project_id, actor_session_id=session_id, token=session_token, **kwargs)
                elif operation == "assign":
                    result = self.service.assign_task(project_id, assigned_by_session_id=session_id, token=session_token, **kwargs)
                elif operation == "task":
                    result = self.service.get_task_evidence(project_id, kwargs["task_id"])
                elif operation == "tasks":
                    tasks = self.service.list_tasks(project_id, phase=kwargs.get("phase"))
                    result = {"tasks": tasks[:self.service.settings.snapshot_recent_limit],
                              "truncated": len(tasks) > self.service.settings.snapshot_recent_limit}
                elif operation == "document":
                    result = self.service.get_project_document(project_id, kwargs["doc_key"])
                elif operation == "review":
                    result = self.service.submit_review(project_id, reviewer_session_id=session_id, token=session_token, **kwargs)
                else:
                    raise DomainError("remote_operation_forbidden", "This operation is not available to a coordinator", status_code=403)
                return {"ok": True, "result": remote_projection(result)}
        except DomainError as error:
            return {"ok": False, **remote_projection(error.as_dict())}


def create_remote_app(service: Any, provider: RemoteOAuthProvider | None = None) -> Any:
    settings = service.settings
    validate_remote_settings(settings)
    provider = provider or RemoteOAuthProvider(service)
    coordinator = RemoteCoordinator(service, provider)
    public = urlparse(provider.issuer)
    local_host = f"{settings.remote_mcp_host}:{settings.remote_mcp_port}"
    mcp = FastMCP(
        settings.product_name + " Remote", host=settings.remote_mcp_host, port=settings.remote_mcp_port,
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        instructions="Remote coordination only. Call project_list, then use explicit project_id. "
            "Read/send Room messages, publish/assign tasks, assess GitHub reports. "
            "No filesystem, execution, claims, WorkReports, integration or administration tools. "
            "Approve only the exact latest report SHA inspected through GitHub. "
            "Use changes_requested pinned to the latest report to return missing or mismatched evidence.",
        auth_server_provider=provider,
        auth=AuthSettings(issuer_url=provider.issuer, resource_server_url=provider.resource,
            required_scopes=["room:read"], revocation_options=RevocationOptions(enabled=True),
            client_registration_options=ClientRegistrationOptions(enabled=True,
                valid_scopes=list(REMOTE_COORDINATOR_SCOPES), default_scopes=list(REMOTE_COORDINATOR_SCOPES))),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[public.netloc, local_host],
            allowed_origins=[provider.issuer, "http://" + local_host],
        ),
    )

    def tool(scope: str, *, read: bool = False) -> Any:
        return mcp.tool(annotations=ToolAnnotations(readOnlyHint=read, destructiveHint=False, openWorldHint=False),
            meta={"securitySchemes": [{"type": "oauth2", "scopes": list(dict.fromkeys(["room:read", scope]))}]})

    async def invoke(scope: str, project_id: str | None, operation: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(partial(coordinator.call, get_access_token(), scope, project_id, operation, **kwargs))

    @tool("room:read", read=True)
    async def project_list() -> dict[str, Any]:
        """List active Rooms without local directory paths; select any explicit project_id."""
        return await invoke("room:read", None, "projects")

    @tool("room:read", read=True)
    async def room_bootstrap() -> dict[str, Any]:
        """Confirm this is the remote coordinator connection and list selectable Projects."""
        return await invoke("room:read", None, "projects")

    @tool("room:read", read=True)
    async def room_sync(project_id: str, after: int = 0) -> dict[str, Any]:
        """Read recent Room messages, tasks, members and documents. Use returned cursor on next read."""
        return await invoke("room:read", project_id, "sync", after=after)

    @tool("message:write")
    async def message_post(project_id: str, body: str, model_display_name: str, kind: str = "message",
                           task_id: str | None = None, mentions: list[str] | None = None,
                           requires_ack: bool = False, channel: str = "public") -> dict[str, Any]:
        """Post a message, decision or blocker using the actual client model label, or unknown."""
        return await invoke("message:write", project_id, "post", body=body, model_display_name=model_display_name,
            kind=kind, task_id=task_id, mentions=mentions, requires_ack=requires_ack, channel=channel)

    @tool("message:write")
    async def message_acknowledge(project_id: str, event_id: int) -> dict[str, Any]:
        """Acknowledge a requested message using its Project-local event number."""
        return await invoke("message:write", project_id, "ack", event_id=event_id)

    @tool("task:publish")
    async def task_create(project_id: str, title: str, description: str, acceptance_criteria: list[str],
                          depends_on: list[str] | None = None, priority: int | None = None) -> dict[str, Any]:
        """Publish a task with acceptance criteria; publication does not claim or execute it."""
        return await invoke("task:publish", project_id, "create", title=title, description=description,
                            acceptance_criteria=acceptance_criteria, depends_on=depends_on, priority=priority)

    @tool("task:assign")
    async def task_assign(project_id: str, task_id: str, assigned_to_member_id: str, note: str = "") -> dict[str, Any]:
        """Propose a task assignment to an existing Project member; it remains subject to acknowledgement."""
        return await invoke("task:assign", project_id, "assign", task_id=task_id, assigned_to_member_id=assigned_to_member_id, note=note)

    @tool("room:read", read=True)
    async def task_get(project_id: str, task_id: str) -> dict[str, Any]:
        """Read a task, its latest WorkReport, criteria and reviews before assessing GitHub evidence."""
        return await invoke("room:read", project_id, "task", task_id=task_id)

    @tool("room:read", read=True)
    async def task_list(project_id: str, phase: str | None = None) -> dict[str, Any]:
        """List Project tasks, optionally filtered by state_view phase (for example awaiting_review)."""
        return await invoke("room:read", project_id, "tasks", phase=phase)

    @tool("room:read", read=True)
    async def project_document_get(project_id: str, doc_key: str) -> dict[str, Any]:
        """Read the Project's versioned instructions before planning or verifying work."""
        return await invoke("room:read", project_id, "document", doc_key=doc_key)

    @tool("review:github")
    async def review_submit(project_id: str, task_id: str, verdict: str, criteria: list[ReviewCriterion],
                            repository_evidence: RepositoryReviewEvidence, notes: str = "") -> dict[str, Any]:
        """Approve or return the latest WorkReport through an independent review.
        Both verdicts require the latest work_report_id, registered repository_url and concrete
        criterion evidence (status passed/failed). approved requires the report's exact full SHA
        and /commit/<SHA> URL. changes_requested may omit SHA/URLs to request missing report
        evidence, or reference a different inspected full SHA with its commit URL to identify
        unreported supplementary work. It reopens execution for the existing owner to submit a
        new report, preserving history. An approval does not integrate or deploy the work.
        """
        return await invoke("review:github", project_id, "review", task_id=task_id, verdict=verdict,
                            criteria=criteria, repository_evidence=dict(repository_evidence), notes=notes)

    app = mcp.streamable_http_app()
    app.state.provider, app.state.coordinator, app.state.mcp_server = provider, coordinator, mcp

    @asynccontextmanager
    async def lifespan(_app: Any):
        async with mcp.session_manager.run():
            yield
    app.router.lifespan_context = lifespan

    async def metadata(_request: Request) -> JSONResponse:
        return JSONResponse({"issuer": provider.issuer, "authorization_endpoint": provider.issuer + "/authorize",
            "token_endpoint": provider.issuer + "/token", "registration_endpoint": provider.issuer + "/register",
            "revocation_endpoint": provider.issuer + "/revoke", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
            "scopes_supported": list(REMOTE_COORDINATOR_SCOPES)})

    async def resource_metadata(_request: Request) -> JSONResponse:
        return JSONResponse({"resource": provider.resource, "authorization_servers": [provider.issuer],
            "scopes_supported": list(REMOTE_COORDINATOR_SCOPES), "bearer_methods_supported": ["header"]})

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "mode": "remote_coordinator"})

    async def revoke(request: Request) -> JSONResponse:
        # SDK 1.x's RevocationRequest declares nullable client_secret without a
        # default, rejecting valid public clients. Keep this compatibility fix
        # on the remote adapter rather than weakening client authentication.
        form = await request.form()
        client = await provider.get_client(str(form.get("client_id", "")))
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        secret = str(form.get("token", ""))
        if not secret:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        token = await provider.load_refresh_token(client, secret)
        if token is None:
            token = await provider.load_access_token(secret)
        if token is not None and token.client_id == client.client_id:
            await provider.revoke_token(token)
        return JSONResponse({}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def consent(request: Request) -> Any:
        try:
            request_id, ticket = request.query_params.get("request_id", ""), request.query_params.get("ticket", "")
            result = await asyncio.to_thread(partial(provider.consent, request_id, ticket, complete=request.url.path.endswith("complete")))
            headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
            if result.get("redirect"):
                return RedirectResponse(result["redirect"], status_code=302, headers=headers)
            text = "请在本机 Room 的「系统设置 → 远程接入」核对验证码并批准，然后点击继续。"
            if result["status"] in {"denied", "expired", "code_issued", "consumed"}:
                text = "此请求已拒绝、过期或完成。请从客户端重新发起连接。"
            esc = html.escape
            # A form submission carries form-action CSP through HTTP redirects
            # in browsers and can block the registered cross-origin callback.
            # Navigation keeps CSP strict while allowing the OAuth redirect.
            continue_url = "/oauth/complete?" + urlencode({"request_id": request_id, "ticket": ticket})
            body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Room 远程授权</title>
                <body><main><h1>Room 远程授权</h1><p>{esc(text)}</p><h2>{esc(str(result.get("verification_code", "")))}</h2>
                <p>客户端：{esc(str(result.get("display_name", "")))}</p><p>回调：{esc(str(result.get("redirect_uri", "")))}</p>
                <p>权限：{esc(" · ".join(result.get("scopes", [])))}</p>
                <p><a href="{esc(continue_url)}">已在本机批准，继续连接</a></p></main></body></html>'''
            return HTMLResponse(body, headers={**headers, "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'"})
        except DomainError as error:
            return JSONResponse(error.as_dict(), status_code=error.status_code, headers={"Cache-Control": "no-store"})

    app.router.routes[:0] = [
        Route("/.well-known/oauth-authorization-server", metadata),
        Route("/.well-known/oauth-protected-resource", resource_metadata),
        Route("/.well-known/oauth-protected-resource/mcp", resource_metadata),
        Route("/oauth/consent", consent), Route("/oauth/complete", consent), Route("/health", health),
        Route("/revoke", revoke, methods=["POST"]),
    ]
    app.add_middleware(RemoteTokenResourceMiddleware, resource=provider.resource)
    return app


class RemoteTokenResourceMiddleware:
    """The SDK validates PKCE, but token-request resource must also be bound."""
    def __init__(self, app: Any, *, resource: str) -> None:
        self.app, self.resource = app, resource

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") in {"/token", "/revoke"} and scope.get("method") == "POST":
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > 16384:
                    await JSONResponse({"error": "invalid_request"}, status_code=413)(scope, receive, send)
                    return
                if not event.get("more_body"):
                    break
            form = parse_qs(body.decode("utf-8", errors="replace"))
            if scope.get("path") == "/token" and form.get("resource") != [self.resource]:
                await JSONResponse({"error": "invalid_request", "error_description": "resource must match the advertised MCP resource"}, status_code=400)(scope, receive, send)
                return
            delivered = False
            async def replay() -> Any:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            await self.app(scope, replay, send)
            return
        await self.app(scope, receive, send)


class RemoteMcpRuntime:
    def __init__(self, service: Any, provider: RemoteOAuthProvider) -> None:
        self.server = uvicorn.Server(uvicorn.Config(create_remote_app(service, provider),
            host=service.settings.remote_mcp_host, port=service.settings.remote_mcp_port,
            access_log=False, log_config=None, proxy_headers=False))
        self.thread = threading.Thread(target=self.server.run, name="agentchatroom-remote-mcp", daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while self.thread.is_alive() and not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            self.stop()
            raise RuntimeError("Remote MCP listener could not start; check the configured loopback port")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.server.force_exit = True
