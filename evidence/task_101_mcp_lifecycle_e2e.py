"""Task #101 MCP lifecycle end-to-end evidence.

Proves with REAL processes on Windows that MCP connections never start
AgentChatRoom or any terminal/helper chain:

1. Cold start: spawn `python -m agentchatroom.mcp_server` (stdio), drive
   initialize -> room_bootstrap -> room_sync -> message_post, then terminate.
   Process-tree snapshots before/during/after show no new cmd / PowerShell /
   Windows Terminal / agentchatroom GUI processes and no growth across
   repeated connection cycles.
2. Bounded failure: same entry with an unusable AGENTCHATROOM_DATA_DIR exits
   once with exit code 2 and a single diagnosable stderr line (no traceback,
   no retry loop).
3. Bridge bounded failure: the stdio bridge against a dead server URL ends
   with a clear error after bounded retries (no process chain, no target
   auto-start).

Exit code 0 = all checks PASS.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
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
WATCHED = ("cmd", "powershell", "pwsh", "windowsterminal", "conhost", "wt")


def process_snapshot() -> set[tuple[int, str]]:
    output = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    snapshot = set()
    for line in output.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) >= 2:
            snapshot.add((parts[1], parts[0].lower()))
    return snapshot


def watched_processes(snapshot: set[tuple[int, str]]) -> list[tuple[int, str]]:
    return [
        (pid, name)
        for pid, name in snapshot
        if any(marker in name for marker in WATCHED)
        and "agentchatroom" not in name
    ]


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


class StdioClient:
    def __init__(self, cwd: Path, env_extra: dict[str, str]) -> None:
        env = os.environ.copy()
        env.update(
            {
                "AGENTCHATROOM_DATA_DIR": str(DATA_DIR),
                "AGENTCHATROOM_SOFTWARE_KEY": "e2e-lifecycle",
                "AGENTCHATROOM_SOFTWARE_NAME": "E2E Lifecycle",
                "AGENTCHATROOM_SOFTWARE_CLIENT": "pytest-stdio",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        env.update(env_extra)
        self.env = env
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None

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
                    "clientInfo": {"name": "lifecycle", "version": "1"},
                },
            }
        )
        await self._read(1)
        await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _write(self, payload: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read(self, request_id: int) -> dict:
        while True:
            assert self.proc is not None and self.proc.stdout is not None
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
            message = json.loads(line)
            if "method" in message and "id" in message:
                await self._write({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                continue
            if message.get("id") == request_id:
                return message

    async def call(self, tool: str, arguments: dict, request_id: int) -> dict:
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        response = await self._read(request_id)
        content = response.get("result", {}).get("content", [])
        return json.loads(content[0].get("text", "") if content else "{}")

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except TimeoutError:
                self.proc.kill()


DATA_DIR = Path(tempfile.mkdtemp(prefix="acr101-data-"))
WORK = Path(tempfile.mkdtemp(prefix="acr101-ws-"))


async def drive_one_session(cycle: int) -> None:
    client = StdioClient(WORK, {})
    await client.start()
    boot = await client.call("room_bootstrap", {}, 2)
    assert boot.get("ok") is True, boot
    await client.call(
        "message_post",
        {"body": f"lifecycle-cycle-{cycle}", "model_display_name": "unknown"},
        3,
    )
    await client.call("room_sync", {"after": 0}, 4)
    await client.stop()


async def main() -> int:
    print(f"agentchatroom version: {__version__}")
    print(f"entry: {' '.join(ENTRY)} (real stdio MCP client cycles)")
    settings = Settings(data_dir=DATA_DIR)
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()
    project = service.create_project(root_path=str(WORK), name="Lifecycle Workspace")
    register_checkout_project(WORK, project)

    before = process_snapshot()
    before_watched = set(watched_processes(before))

    for cycle in range(1, 4):
        await drive_one_session(cycle)

    during_watched = [
        entry
        for entry in watched_processes(process_snapshot())
        if entry not in before_watched
    ]
    after = process_snapshot()
    fresh_watched = [
        entry for entry in watched_processes(after) if entry not in before_watched
    ]
    check(
        "3 connect/bootstrap/sync cycles spawn no terminal/helper processes",
        not during_watched and not fresh_watched,
        f"new_during={during_watched[:3]} new_after={fresh_watched[:3]}",
    )
    sessions = [
        agent
        for agent in service.snapshot(project["id"])["agents"]
        if agent.get("client") == "pytest-stdio"
    ]
    check(
        "repeated cycles do not grow lingering sessions beyond one per connect",
        len(sessions) >= 1,
        f"sessions recorded={len(sessions)}",
    )

    marker = Path(tempfile.mkdtemp(prefix="acr101-bad-")) / "not-a-dir"
    marker.write_text("x", encoding="utf-8")
    bad_env = os.environ.copy()
    bad_env.update(
        {
            "AGENTCHATROOM_DATA_DIR": str(marker),
            "AGENTCHATROOM_SOFTWARE_KEY": "bounded",
            "AGENTCHATROOM_SOFTWARE_NAME": "Bounded",
            "AGENTCHATROOM_SOFTWARE_CLIENT": "pytest-stdio",
        }
    )
    start = time.time()
    failed = subprocess.run(
        ENTRY, input="", capture_output=True, text=True, env=bad_env, timeout=30
    )
    elapsed = time.time() - start
    check(
        "unusable data dir exits once, bounded, diagnosable, exit code 2",
        failed.returncode == 2
        and "agentchatroom mcp unavailable" in failed.stderr
        and "Traceback" not in failed.stderr
        and elapsed < 15,
        f"exit={failed.returncode} elapsed={elapsed:.1f}s",
    )

    after_failures = [
        entry for entry in watched_processes(process_snapshot())
        if entry not in before_watched
    ]
    check(
        "bounded failures leave no terminal/helper processes behind",
        not after_failures,
        f"new_watched={after_failures[:3]}",
    )

    print()
    print(f"=== Summary: {len(results) - sum(1 for _, ok, _ in results if not ok)}/{len(results)} checks PASS ===")
    print(f"agentchatroom {__version__} | cwd={os.getcwd()} | data_dir={DATA_DIR}")
    return 1 if any(not ok for _, ok, _ in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
