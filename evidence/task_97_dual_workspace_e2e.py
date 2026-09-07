"""Task #97 dual-workspace end-to-end evidence.

Drives three REAL MCP stdio clients (`python -m agentchatroom.mcp_server`)
against one isolated data directory with two registered project workspaces:

1. client A: cwd=workspace A                      -> bootstrap ready into A
2. client B: cwd=workspace B                      -> bootstrap ready into B
3. client C: cwd=workspace B + AGENTCHATROOM_PROJECT_PATH=A (stale pin)
       -> bootstrap must enter B (never A) and report the
          `configured_project_path_ignored` notice.

Also asserts per-project message isolation and that A/B sessions stay
connected after the stale-pin client joins. Exit code 0 = all checks PASS.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentchatroom import __version__  # noqa: E402
from agentchatroom.config import Settings  # noqa: E402
from agentchatroom.database import Database  # noqa: E402
from agentchatroom.project_registration import register_checkout_project  # noqa: E402
from agentchatroom.services import AgentChatRoomService  # noqa: E402

PYTHON = sys.executable
ENTRY = [PYTHON, "-m", "agentchatroom.mcp_server"]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


class StdioClient:
    """Minimal newline-delimited JSON-RPC MCP client (real stdio transport)."""

    def __init__(self, name: str, cwd: Path, env_extra: dict[str, str]) -> None:
        self.name = name
        self.cwd = cwd
        env = os.environ.copy()
        env.update(
            {
                "AGENTCHATROOM_DATA_DIR": str(DATA_DIR),
                "AGENTCHATROOM_SOFTWARE_KEY": "e2e-dual-workspace",
                "AGENTCHATROOM_SOFTWARE_NAME": "E2E Dual Workspace",
                "AGENTCHATROOM_SOFTWARE_CLIENT": "pytest-stdio",
                "AGENTCHATROOM_PRESENCE_KEEPALIVE_ENABLED": "true",
                "AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS": "0.05",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        env.update(env_extra)
        self.env = env
        self.proc = None

    async def _write(self, payload: dict) -> None:
        assert self.proc is not None
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()

    async def _serve_request(self, message: dict) -> None:
        # room_bootstrap asks the real client for its workspace roots via
        # roots/list; answer like a standard MCP client would.
        result: dict = {}
        if message.get("method") == "roots/list":
            result = {"roots": [{"uri": self.cwd.resolve().as_uri()}]}
        await self._write(
            {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        )

    async def _read_response(self, request_id: int) -> dict:
        while True:
            assert self.proc is not None and self.proc.stdout is not None
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
            message = json.loads(line)
            if "method" in message and "id" in message:
                await self._serve_request(message)
                continue
            if message.get("id") == request_id:
                return message

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *ENTRY,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=str(self.cwd),
        )
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": self.name, "version": "1"},
                },
            }
        )
        await self._read_response(1)
        await self._write(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

    async def call(self, tool: str, arguments: dict, request_id: int) -> dict:
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        response = await self._read_response(request_id)
        content = response.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""
        return json.loads(text)

    async def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except TimeoutError:
                self.proc.kill()


DATA_DIR = Path(tempfile.mkdtemp(prefix="acr97-data-"))
WORK_A = Path(tempfile.mkdtemp(prefix="acr97-ws-a-"))
WORK_B = Path(tempfile.mkdtemp(prefix="acr97-ws-b-"))
OUTSIDE = Path(tempfile.mkdtemp(prefix="acr97-plain-"))


async def main() -> int:
    settings = Settings(data_dir=DATA_DIR)
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()
    project_a = service.create_project(root_path=str(WORK_A), name="E2E Workspace A")
    project_b = service.create_project(root_path=str(WORK_B), name="E2E Workspace B")
    register_checkout_project(WORK_A, project_a)
    register_checkout_project(WORK_B, project_b)
    print(f"agentchatroom version: {__version__}")
    print(f"entry: {' '.join(ENTRY)} (real stdio MCP clients)")
    print(f"workspace A: {WORK_A} -> {project_a['id']}")
    print(f"workspace B: {WORK_B} -> {project_b['id']}")

    client_a = StdioClient("client-A", WORK_A, {})
    client_b = StdioClient("client-B", WORK_B, {})
    # Regression: cwd=B (workspace evidence) + stale pin=A must enter B, not A.
    client_c = StdioClient(
        "client-C-stale-pin",
        WORK_B,
        {"AGENTCHATROOM_PROJECT_PATH": str(WORK_A)},
    )

    await client_a.start()
    boot_a = await client_a.call("room_bootstrap", {}, 2)
    check(
        "A bootstrap ready into workspace A",
        boot_a.get("ok") is True
        and boot_a["result"]["status"] == "ready"
        and boot_a["result"]["project"]["id"] == project_a["id"],
        f"status={boot_a.get('result', {}).get('status')}",
    )

    await client_b.start()
    boot_b = await client_b.call("room_bootstrap", {}, 2)
    check(
        "B bootstrap ready into workspace B",
        boot_b.get("ok") is True
        and boot_b["result"]["status"] == "ready"
        and boot_b["result"]["project"]["id"] == project_b["id"],
        f"status={boot_b.get('result', {}).get('status')}",
    )

    await client_a.call(
        "message_post",
        {"body": "workspace-A-only-message", "model_display_name": "unknown"},
        3,
    )
    await client_b.call(
        "message_post",
        {"body": "workspace-B-only-message", "model_display_name": "unknown"},
        3,
    )
    sync_a = await client_a.call("room_sync", {"after": 0}, 4)
    sync_b = await client_b.call("room_sync", {"after": 0}, 4)
    body_a = json.dumps(sync_a, ensure_ascii=False)
    body_b = json.dumps(sync_b, ensure_ascii=False)
    check(
        "A sync carries A message, never B message",
        "workspace-A-only-message" in body_a
        and "workspace-B-only-message" not in body_a,
    )
    check(
        "B sync carries B message, never A message",
        "workspace-B-only-message" in body_b
        and "workspace-A-only-message" not in body_b,
    )

    await client_c.start()
    boot_c = await client_c.call("room_bootstrap", {}, 2)
    result_c = boot_c.get("result", {})
    notices = result_c.get("notices") or []
    notice_codes = [notice.get("code") for notice in notices]
    check(
        "stale pin cannot divert cwd=B bootstrap into A",
        boot_c.get("ok") is True
        and result_c.get("status") == "ready"
        and result_c.get("project", {}).get("id") == project_b["id"],
        f"project={result_c.get('project', {}).get('id')}",
    )
    check(
        "ignored stale pin is reported with recovery action",
        "configured_project_path_ignored" in notice_codes
        and any(
            notice.get("required_action")
            == "align_or_remove_agentchatroom_project_path_env"
            for notice in notices
        ),
        f"notices={notice_codes}",
    )

    def is_e2e_agent(agent: dict) -> bool:
        return (
            agent.get("software_key")
            or (agent.get("metadata") or {}).get("software_key")
            or ""
        ) == "e2e-dual-workspace" or agent.get("client") == "pytest-stdio"

    agents_a = [
        agent for agent in service.snapshot(project_a["id"])["agents"]
        if is_e2e_agent(agent)
    ]
    detail = "; ".join(
        f"{agent['id']} status={agent['status']} hb={agent.get('last_heartbeat')}"
        for agent in agents_a
    )
    check(
        "A session still connected after B and stale-pin joins",
        any(agent["status"] == "online" for agent in agents_a),
        detail,
    )

    for client in (client_a, client_b, client_c):
        await client.stop()

    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"=== Summary: {len(results) - len(failed)}/{len(results)} checks PASS ===")
    print(f"agentchatroom {__version__} | cwd={os.getcwd()} | data_dir={DATA_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
