"""Exercise two isolated stdio Bridges against one AgentChatRoom center.

This is an acceptance harness, not a production client. It deliberately uses
two independent processes and workspace identities so that the center is
tested through the same remote MCP boundary used by real Agent tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify two isolated remote AgentChatRoom Hosts through MCP Bridges."
    )
    parser.add_argument("--url", required=True, help="Center MCP URL, for example http://127.0.0.1:8765/mcp")
    parser.add_argument("--token", required=True, help="Project Agent Token")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workspace-a", required=True)
    parser.add_argument("--workspace-b", required=True)
    parser.add_argument("--host-a", default="live-host-a")
    parser.add_argument("--host-b", default="live-host-b")
    parser.add_argument(
        "--request-prefix",
        default="",
        help="Optional prefix for idempotent write keys; defaults to a unique run prefix",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args()


@dataclass
class BridgeClient:
    label: str
    url: str
    token: str
    timeout: float
    process: asyncio.subprocess.Process | None = None
    next_id: int = 1

    async def start(self) -> None:
        environment = os.environ.copy()
        environment["AGENTCHATROOM_AGENT_TOKEN"] = self.token
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agentchatroom.mcp_bridge",
            "--url",
            self.url,
            "--timeout",
            str(self.timeout),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            limit=1024 * 1024,
        )
        await self.exchange(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": f"remote-host-{self.label}", "version": "1"},
            },
        )
        await self.notify("notifications/initialized")
        await self.exchange("tools/list", {})

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError(f"{self.label}: Bridge is not running")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def exchange(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError(f"{self.label}: Bridge is not running")
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.timeout)
        except TimeoutError as error:
            details = ""
            if self.process.stderr is not None:
                try:
                    details = (await asyncio.wait_for(self.process.stderr.read(4096), timeout=0.2)).decode(
                        errors="replace"
                    )
                except TimeoutError:
                    pass
            raise RuntimeError(f"{self.label}: MCP response timed out: {details}") from error
        if not line:
            details = ""
            if self.process.stderr is not None:
                details = (await self.process.stderr.read()).decode(errors="replace")
            raise RuntimeError(f"{self.label}: Bridge exited without a response: {details}")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"{self.label}: JSON-RPC error: {response['error']}")
        return response

    async def call_tool_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.exchange("tools/call", {"name": name, "arguments": arguments})
        content = response.get("result", {}).get("content", [])
        if not content or "text" not in content[0]:
            raise RuntimeError(f"{self.label}: Tool {name} returned no JSON content")
        return json.loads(content[0]["text"])

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None


async def run(args: argparse.Namespace) -> dict[str, Any]:
    request_prefix = args.request_prefix.strip() or f"remote-two-host-{uuid.uuid4().hex[:12]}"
    clients = [
        BridgeClient("a", args.url, args.token, args.timeout),
        BridgeClient("b", args.url, args.token, args.timeout),
    ]
    try:
        await asyncio.gather(*(client.start() for client in clients))
        joined = await asyncio.gather(
            clients[0].call_tool_raw(
                "room_join",
                {
                    "project_path": args.workspace_a,
                    "agent_key": "remote-host-a-main",
                    "agent_name": "Remote Host A",
                    "client": "remote-bridge-verifier",
                    "model": "verification-model",
                    "role": "executor",
                    "host_key": args.host_a,
                    "host_name": "Remote Host A",
                    "worktree": args.workspace_a,
                },
            ),
            clients[1].call_tool_raw(
                "room_join",
                {
                    "project_path": args.workspace_b,
                    "agent_key": "remote-host-b-main",
                    "agent_name": "Remote Host B",
                    "client": "remote-bridge-verifier",
                    "model": "verification-model",
                    "role": "reviewer",
                    "host_key": args.host_b,
                    "host_name": "Remote Host B",
                    "worktree": args.workspace_b,
                },
            ),
        )
        for index, payload in enumerate(joined):
            if not payload.get("ok"):
                raise RuntimeError(f"Host {index + 1} failed to join: {payload}")
        sessions = [payload["result"] for payload in joined]
        if any(session["project"]["id"] != args.project_id for session in sessions):
            raise RuntimeError("Remote hosts did not join the requested Project")

        sync_a, sync_b = await asyncio.gather(
            clients[0].call_tool_raw(
                "room_sync",
                {
                    "project_id": args.project_id,
                    "session_id": sessions[0]["agent"]["id"],
                    "token": sessions[0]["token"],
                    "after": 0,
                },
            ),
            clients[1].call_tool_raw(
                "room_sync",
                {
                    "project_id": args.project_id,
                    "session_id": sessions[1]["agent"]["id"],
                    "token": sessions[1]["token"],
                    "after": 0,
                },
            ),
        )
        for label, payload in (("A", sync_a), ("B", sync_b)):
            if not payload.get("ok"):
                raise RuntimeError(f"Host {label} failed to sync: {payload}")

        message_arguments = {
            "project_id": args.project_id,
            "session_id": sessions[0]["agent"]["id"],
            "token": sessions[0]["token"],
            "body": "Remote Host A reached the shared Room",
            "model_display_name": "unknown",
            "request_id": f"{request_prefix}-message",
        }
        first_message = await clients[0].call_tool_raw("message_post", message_arguments)
        replay_message = await clients[0].call_tool_raw("message_post", message_arguments)
        if not first_message.get("ok") or not replay_message.get("ok"):
            raise RuntimeError(f"Message write failed: {first_message} / {replay_message}")
        first_event_id = first_message["result"]["event_id"]
        replay_event_id = replay_message["result"]["event_id"]
        if first_event_id != replay_event_id:
            raise RuntimeError("Idempotent message replay returned a different event")

        lease_arguments = {
            "project_id": args.project_id,
            "session_id": sessions[0]["agent"]["id"],
            "token": sessions[0]["token"],
            "path_pattern": "src/example-remote-host.py",
            "mode": "exclusive",
            "reason": "Remote host lease verification",
            "request_id": f"{request_prefix}-lease-a",
        }
        lease = await clients[0].call_tool_raw("lease_acquire", lease_arguments)
        if not lease.get("ok"):
            raise RuntimeError(f"Host A failed to acquire lease: {lease}")
        conflict = await clients[1].call_tool_raw(
            "lease_acquire",
            {
                "project_id": args.project_id,
                "session_id": sessions[1]["agent"]["id"],
                "token": sessions[1]["token"],
                "path_pattern": "src/example-remote-host.py",
                "mode": "exclusive",
                "reason": "Expected conflict",
                "request_id": f"{request_prefix}-lease-b",
            },
        )
        if conflict.get("ok") or conflict.get("error", {}).get("code") != "lease_conflict":
            raise RuntimeError(f"Expected a structured lease conflict, got: {conflict}")

        synced_b = await clients[1].call_tool_raw(
            "room_sync",
            {
                "project_id": args.project_id,
                "session_id": sessions[1]["agent"]["id"],
                "token": sessions[1]["token"],
                "after": 0,
            },
        )
        if not synced_b.get("ok"):
            raise RuntimeError(f"Host B failed after message/lease events: {synced_b}")
        agent_ids = {agent["id"] for agent in synced_b["result"]["snapshot"]["agents"]}
        if sessions[0]["agent"]["id"] not in agent_ids or sessions[1]["agent"]["id"] not in agent_ids:
            raise RuntimeError("Host B did not observe both remote Agent sessions")

        await clients[0].call_tool_raw(
            "lease_release",
            {
                "project_id": args.project_id,
                "lease_id": lease["result"]["lease"]["id"],
                "session_id": sessions[0]["agent"]["id"],
                "token": sessions[0]["token"],
                "request_id": f"{request_prefix}-lease-release",
            },
        )
        return {
            "project_id": args.project_id,
            "host_a": {"host_key": args.host_a, "workspace": args.workspace_a, "session_id": sessions[0]["agent"]["id"]},
            "host_b": {"host_key": args.host_b, "workspace": args.workspace_b, "session_id": sessions[1]["agent"]["id"]},
            "first_event_id": first_event_id,
            "replay_event_id": replay_event_id,
            "idempotent_replay": first_event_id == replay_event_id,
            "lease_conflict_code": conflict["error"]["code"],
            "shared_room_observed": True,
        }
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def main() -> None:
    args = parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
