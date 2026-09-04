from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlparse

import anyio
import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from . import __version__


logger = logging.getLogger(__name__)
BRIDGE_INSTANCE_ID = uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    server_url: str
    agent_token: str = ""
    ca_file: Path | None = None
    timeout_seconds: float = 30.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    presence_keepalive_enabled: bool = True
    presence_keepalive_interval_seconds: float = 15.0


AUTO_IDEMPOTENCY_TOOL_NAMES = frozenset(
    {
        "session_leave",
        "message_post",
        "message_acknowledge",
        "task_create",
        "task_claim",
        "task_assign",
        "task_acknowledge",
        "task_handoff",
        "task_handoff_acknowledge",
        "task_update",
        "lease_acquire",
        "lease_release",
        "work_report",
        "review_submit",
        "integration_submit",
        "knowledge_candidate_submit",
        "knowledge_review",
        "knowledge_supersede",
        "knowledge_archive",
    }
)
ResultT = TypeVar("ResultT")
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def parse_bridge_settings(argv: list[str] | None = None) -> BridgeSettings:
    def environment_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        normalized = raw.strip().lower()
        if normalized not in {"true", "false"}:
            parser.error(f"{name} must be true or false")
        return normalized == "true"

    parser = argparse.ArgumentParser(
        prog="agentchatroom-mcp-bridge",
        description="Forward a local stdio MCP client to an AgentChatRoom center.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("AGENTCHATROOM_SERVER_URL", ""),
        help="Central Streamable HTTP MCP URL",
    )
    parser.add_argument(
        "--ca-file",
        default=os.getenv("AGENTCHATROOM_TLS_CA_FILE", ""),
        help="Optional CA bundle for a private TLS certificate",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("AGENTCHATROOM_BRIDGE_TIMEOUT_SECONDS", "30")),
        help="Connection timeout in seconds",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=int(os.getenv("AGENTCHATROOM_BRIDGE_RETRY_ATTEMPTS", "3")),
        help="Maximum attempts for a transient upstream operation",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=float(os.getenv("AGENTCHATROOM_BRIDGE_RETRY_BACKOFF_SECONDS", "0.25")),
        help="Initial reconnect backoff in seconds",
    )
    parser.add_argument(
        "--presence-interval",
        type=float,
        default=float(
            os.getenv("AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS", "15")
        ),
        help="Background session heartbeat interval in seconds",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args(argv)
    url = args.url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error(
            "--url or AGENTCHATROOM_SERVER_URL must be a full http(s) MCP URL"
        )
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retry_attempts < 1:
        parser.error("--retry-attempts must be at least 1")
    if args.retry_backoff < 0:
        parser.error("--retry-backoff must not be negative")
    if args.presence_interval <= 0:
        parser.error("--presence-interval must be positive")
    ca_file = Path(args.ca_file).expanduser().resolve() if args.ca_file else None
    if ca_file is not None and not ca_file.is_file():
        parser.error(f"CA file does not exist: {ca_file}")
    return BridgeSettings(
        server_url=url,
        agent_token=os.getenv("AGENTCHATROOM_AGENT_TOKEN", "").strip(),
        ca_file=ca_file,
        timeout_seconds=args.timeout,
        retry_attempts=args.retry_attempts,
        retry_backoff_seconds=args.retry_backoff,
        presence_keepalive_enabled=environment_bool(
            "AGENTCHATROOM_PRESENCE_KEEPALIVE_ENABLED", True
        ),
        presence_keepalive_interval_seconds=args.presence_interval,
    )


def prepare_tool_arguments(
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    forwarded = dict(arguments)
    if name not in AUTO_IDEMPOTENCY_TOOL_NAMES:
        return forwarded
    current = str(forwarded.get("request_id", "")).strip()
    if not current:
        current = f"request_{uuid.uuid4().hex}"
    prefix = f"bridge:{BRIDGE_INSTANCE_ID}:"
    if not current.startswith(prefix):
        scoped = prefix + current
        if len(scoped) > 128:
            digest = uuid.uuid5(uuid.NAMESPACE_URL, current).hex
            scoped = f"{prefix}{digest}"[:128]
        current = scoped
    forwarded["request_id"] = current
    return forwarded


def exception_tree(error: BaseException) -> list[BaseException]:
    pending = [error]
    visited: set[int] = set()
    flattened: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        flattened.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return flattened


def retryable_bridge_error(error: Exception) -> bool:
    for current in exception_tree(error):
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code in RETRYABLE_HTTP_STATUS_CODES
        if isinstance(
            current,
            (
                httpx.RequestError,
                OSError,
                TimeoutError,
                anyio.EndOfStream,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ),
        ):
            return True
        message = str(current).lower()
        if any(
            marker in message
            for marker in (
                "connection closed",
                "connection reset",
                "connection refused",
                "disconnected",
                "stream closed",
            )
        ):
            return True
    return False


class ReconnectingUpstream:
    def __init__(
        self,
        settings: BridgeSettings,
        http_client: httpx.AsyncClient | None = None,
        *,
        headers: dict[str, str] | None = None,
        verify: bool | str = True,
    ) -> None:
        self.settings = settings
        self.headers = dict(headers or {})
        self.verify = verify
        self._provided_http_client = http_client

    @asynccontextmanager
    async def _http_client(self):
        if self._provided_http_client is not None:
            yield self._provided_http_client
            return
        timeout = httpx.Timeout(
            self.settings.timeout_seconds,
            read=self.settings.timeout_seconds,
        )
        async with httpx.AsyncClient(
            headers=self.headers,
            timeout=timeout,
            verify=self.verify,
            follow_redirects=True,
        ) as http_client:
            yield http_client

    async def _run(
        self,
        operation: Callable[[ClientSession], Awaitable[ResultT]],
    ) -> ResultT:
        last_error: Exception | None = None
        for attempt in range(self.settings.retry_attempts):
            try:
                # Use a fresh HTTP pool for every attempt. A center restart can
                # leave a keep-alive socket in the old pool that otherwise
                # waits forever instead of failing fast and being retried.
                async with self._http_client() as http_client:
                    with anyio.fail_after(self.settings.timeout_seconds):
                        async with streamable_http_client(
                            self.settings.server_url,
                            http_client=http_client,
                        ) as (upstream_read, upstream_write, _session_id):
                            async with ClientSession(
                                upstream_read,
                                upstream_write,
                                client_info=types.Implementation(
                                    name="AgentChatRoom stdio Bridge",
                                    version=__version__,
                                ),
                            ) as session:
                                await session.initialize()
                                return await operation(session)
            except Exception as error:
                if not retryable_bridge_error(error):
                    raise
                last_error = error
                logger.warning(
                    "Upstream MCP attempt %d/%d failed; retrying if possible: %s",
                    attempt + 1,
                    self.settings.retry_attempts,
                    type(error).__name__,
                )
                if attempt + 1 < self.settings.retry_attempts:
                    await anyio.sleep(
                        self.settings.retry_backoff_seconds * (2**attempt)
                    )
        assert last_error is not None
        raise last_error

    async def list_tools(self) -> types.ListToolsResult:
        return await self._run(lambda session: session.list_tools())

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> types.CallToolResult:
        return await self._run(lambda session: session.call_tool(name, arguments))

    async def close(self) -> None:
        return None


def tool_payload(result: types.CallToolResult) -> dict[str, Any] | None:
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        return structured
    for item in result.content:
        if isinstance(item, types.TextContent):
            try:
                payload = json.loads(item.text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                return payload
    return None


class BridgePresenceManager:
    """Keep only sessions joined through this client-side Bridge alive."""

    def __init__(self, upstream: ReconnectingUpstream, settings: BridgeSettings) -> None:
        self.upstream = upstream
        self.enabled = settings.presence_keepalive_enabled
        self.interval_seconds = settings.presence_keepalive_interval_seconds
        self.sessions: dict[str, dict[str, str]] = {}

    def observe(
        self,
        name: str,
        arguments: dict[str, object],
        result: types.CallToolResult,
    ) -> None:
        if not self.enabled:
            return
        payload = tool_payload(result)
        if not payload or not payload.get("ok"):
            return
        if name == "room_join":
            joined = payload.get("result") or {}
            project = joined.get("project") or {}
            agent = joined.get("agent") or {}
            session_id = str(agent.get("id", ""))
            token = str(joined.get("token", ""))
            project_id = str(project.get("id", ""))
            if project_id and session_id and token:
                self.sessions[session_id] = {
                    "project_id": project_id,
                    "session_id": session_id,
                    "token": token,
                }
        elif name == "session_leave":
            self.sessions.pop(str(arguments.get("session_id", "")), None)

    async def heartbeat_once(self) -> None:
        for session_id, session in list(self.sessions.items()):
            arguments = prepare_tool_arguments("session_heartbeat", dict(session))
            try:
                result = await self.upstream.call_tool(
                    "session_heartbeat", arguments
                )
                payload = tool_payload(result)
                if payload and not payload.get("ok") and payload.get("error", {}).get(
                    "code"
                ) in {"session_not_found", "invalid_session_token"}:
                    self.sessions.pop(session_id, None)
            except Exception as error:
                logger.warning(
                    "Presence heartbeat failed for session %s: %s",
                    session_id,
                    type(error).__name__,
                )

    async def run(self) -> None:
        while True:
            await anyio.sleep(self.interval_seconds)
            await self.heartbeat_once()


async def run_bridge(settings: BridgeSettings) -> None:
    headers = {}
    if settings.agent_token:
        headers["Authorization"] = f"Bearer {settings.agent_token}"
    verify: bool | str = str(settings.ca_file) if settings.ca_file else True
    upstream = ReconnectingUpstream(settings, headers=headers, verify=verify)
    presence = BridgePresenceManager(upstream, settings)
    bridge = Server(
        "AgentChatRoom Bridge",
        version=__version__,
        instructions=(
            "This process forwards tools to the configured AgentChatRoom "
            "center and owns no coordination database."
        ),
    )

    @bridge.list_tools()
    async def list_tools() -> types.ListToolsResult:
        return await upstream.list_tools()

    @bridge.call_tool(validate_input=False)
    async def call_tool(
        name: str,
        arguments: dict[str, object],
    ) -> types.CallToolResult:
        forwarded = prepare_tool_arguments(name, arguments)
        result = await upstream.call_tool(name, forwarded)
        presence.observe(name, forwarded, result)
        return result

    try:
        async with anyio.create_task_group() as task_group:
            if presence.enabled:
                task_group.start_soon(presence.run)
            async with stdio_server() as (local_read, local_write):
                await bridge.run(
                    local_read,
                    local_write,
                    bridge.create_initialization_options(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                    raise_exceptions=True,
                )
            task_group.cancel_scope.cancel()
    finally:
        await upstream.close()


def main(argv: list[str] | None = None) -> None:
    settings = parse_bridge_settings(argv)
    try:
        anyio.run(run_bridge, settings)
    except (httpx.HTTPError, OSError) as error:
        raise SystemExit(f"AgentChatRoom Bridge could not connect: {error}") from error


if __name__ == "__main__":
    main(sys.argv[1:])
