"""Verify one real AgentChatRoom center through remote Streamable HTTP MCP.

This is a deployment acceptance harness. It deliberately uses one independent
remote Agent identity and never prints the supplied Agent Token. For two
independent workspaces, run it alongside ``verify_remote_hosts.py`` or use the
two-host harness directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


EXPECTED_TOOLS = {
    "room_join",
    "room_sync",
    "task_get",
    "message_post",
    "lease_acquire",
    "lease_release",
    "session_leave",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify one remote AgentChatRoom center through Streamable HTTP MCP."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("AGENTCHATROOM_SERVER_URL", ""),
        help="Center MCP URL; defaults to AGENTCHATROOM_SERVER_URL",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("AGENTCHATROOM_AGENT_TOKEN", ""),
        help="Project Agent Token; defaults to AGENTCHATROOM_AGENT_TOKEN",
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--task-id",
        default="",
        help="Optional exact Task ID to verify with task_get instead of reading the full task list",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--host-key", default="live-server-verifier")
    parser.add_argument("--host-name", default="Live Server Verification Host")
    parser.add_argument("--agent-key", default="live-server-verifier-main")
    parser.add_argument("--agent-name", default="Live Server Verification Agent")
    parser.add_argument("--client", default="agentchatroom-live-verifier")
    parser.add_argument("--model", default="verification-model")
    parser.add_argument("--role", default="executor")
    parser.add_argument(
        "--request-prefix",
        default="",
        help="Prefix for idempotency keys; defaults to a unique run prefix",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    args.url = args.url.strip().rstrip("/")
    args.token = args.token.strip()
    if not args.url:
        parser.error("--url or AGENTCHATROOM_SERVER_URL is required")
    if not args.token:
        parser.error("--token or AGENTCHATROOM_AGENT_TOKEN is required")
    if not args.url.endswith("/mcp"):
        parser.error("--url must point to the center MCP endpoint ending in /mcp")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def parse_tool_result(result: types.CallToolResult, label: str) -> dict[str, Any]:
    for item in result.content:
        if isinstance(item, types.TextContent):
            try:
                payload = json.loads(item.text)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"{label} returned invalid JSON") from error
            if not isinstance(payload, dict):
                raise RuntimeError(f"{label} returned a non-object payload")
            return payload
    raise RuntimeError(f"{label} returned no text payload")


def require_ok(payload: dict[str, Any], label: str) -> dict[str, Any]:
    if payload.get("ok") is not True:
        error = payload.get("error")
        raise RuntimeError(f"{label} failed: {error if isinstance(error, dict) else 'unknown error'}")
    return payload


async def run(args: argparse.Namespace) -> dict[str, Any]:
    request_prefix = args.request_prefix.strip() or f"live-server-{uuid.uuid4().hex[:12]}"
    headers = {"Authorization": f"Bearer {args.token}"}
    timeout = httpx.Timeout(args.timeout, read=args.timeout)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as (
            upstream_read,
            upstream_write,
            _mcp_session_id,
        ):
            async with ClientSession(
                upstream_read,
                upstream_write,
                client_info=types.Implementation(
                    name="AgentChatRoom live server verifier",
                    version="1",
                ),
            ) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                missing = sorted(EXPECTED_TOOLS - tool_names)
                if missing:
                    raise RuntimeError(f"center MCP is missing expected tools: {', '.join(missing)}")

                task_get_verified = False
                if args.task_id:
                    task_payload = require_ok(
                        parse_tool_result(
                            await session.call_tool(
                                "task_get",
                                {
                                    "project_id": args.project_id,
                                    "task_id": args.task_id,
                                },
                            ),
                            "task_get",
                        ),
                        "task_get",
                    )
                    task = task_payload["result"]
                    if task.get("id") != args.task_id:
                        raise RuntimeError("task_get returned a different Task")
                    task_get_verified = True

                joined = require_ok(
                    parse_tool_result(
                        await session.call_tool(
                            "room_join",
                            {
                                "project_path": args.workspace,
                                "agent_key": args.agent_key,
                                "agent_name": args.agent_name,
                                "client": args.client,
                                "model": args.model,
                                "role": args.role,
                                "host_key": args.host_key,
                                "host_name": args.host_name,
                                "worktree": args.workspace,
                                "capabilities": {"mcp": True, "live_server_verifier": True},
                            },
                        ),
                        "room_join",
                    ),
                    "room_join",
                )
                joined_result = joined["result"]
                if joined_result["project"]["id"] != args.project_id:
                    raise RuntimeError("room_join returned a different Project")
                session_info = joined_result["agent"]
                session_id = session_info["id"]
                session_token = joined_result["token"]

                first_sync = require_ok(
                    parse_tool_result(
                        await session.call_tool(
                            "room_sync",
                            {
                                "project_id": args.project_id,
                                "session_id": session_id,
                                "token": session_token,
                                "after": 0,
                            },
                        ),
                        "room_sync",
                    ),
                    "room_sync",
                )

                message_arguments = {
                    "project_id": args.project_id,
                    "session_id": session_id,
                    "token": session_token,
                    "body": "Live server MCP verification reached the shared Room",
                    "model_display_name": "unknown",
                    "request_id": f"{request_prefix}-message",
                }
                first_message = require_ok(
                    parse_tool_result(
                        await session.call_tool("message_post", message_arguments),
                        "message_post",
                    ),
                    "message_post",
                )
                replay_message = require_ok(
                    parse_tool_result(
                        await session.call_tool("message_post", message_arguments),
                        "message_post replay",
                    ),
                    "message_post replay",
                )
                first_event_id = first_message["result"]["event_id"]
                replay_event_id = replay_message["result"]["event_id"]
                if first_event_id != replay_event_id:
                    raise RuntimeError("idempotent replay returned a different event")

                lease = require_ok(
                    parse_tool_result(
                        await session.call_tool(
                            "lease_acquire",
                            {
                                "project_id": args.project_id,
                                "session_id": session_id,
                                "token": session_token,
                                "path_pattern": "src/example-live-verification.py",
                                "mode": "exclusive",
                                "reason": "Live server MCP verification",
                                "request_id": f"{request_prefix}-lease",
                            },
                        ),
                        "lease_acquire",
                    ),
                    "lease_acquire",
                )
                lease_id = lease["result"]["lease"]["id"]
                released = require_ok(
                    parse_tool_result(
                        await session.call_tool(
                            "lease_release",
                            {
                                "project_id": args.project_id,
                                "lease_id": lease_id,
                                "session_id": session_id,
                                "token": session_token,
                                "request_id": f"{request_prefix}-lease-release",
                            },
                        ),
                        "lease_release",
                    ),
                    "lease_release",
                )
                final_sync = require_ok(
                    parse_tool_result(
                        await session.call_tool(
                            "room_sync",
                            {
                                "project_id": args.project_id,
                                "session_id": session_id,
                                "token": session_token,
                                "after": first_sync["result"]["cursor"],
                            },
                        ),
                        "room_sync after writes",
                    ),
                    "room_sync after writes",
                )
                left = require_ok(
                    parse_tool_result(
                        await session.call_tool(
                            "session_leave",
                            {
                                "project_id": args.project_id,
                                "session_id": session_id,
                                "token": session_token,
                                "request_id": f"{request_prefix}-leave",
                            },
                        ),
                        "session_leave",
                    ),
                    "session_leave",
                )

    return {
        "project_id": args.project_id,
        "task_id": args.task_id or None,
        "host_key": args.host_key,
        "workspace": args.workspace,
        "session_id": session_id,
        "tools_checked": sorted(EXPECTED_TOOLS),
        "tools_visible_count": len(tool_names),
        "initial_cursor": first_sync["result"]["cursor"],
        "final_cursor": final_sync["result"]["cursor"],
        "first_event_id": first_event_id,
        "replay_event_id": replay_event_id,
        "idempotent_replay": first_event_id == replay_event_id,
        "lease_id": lease_id,
        "lease_released": released.get("result", {}).get("released", True),
        "session_left": left.get("result", {}).get("left", True),
        "task_get_verified": task_get_verified,
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
