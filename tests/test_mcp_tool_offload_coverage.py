"""Task #277: no MCP tool may occupy the shared event loop.

REST, SSE and every MCP client live in one process on one loop, so a synchronous
tool that shells out to git freezes all of them for its duration. The offload
lives in ``CompatibleToolManager``, and a tool only benefits from it if its
signature still qualifies -- so the real risk is silent: a future tool can be
added that runs inline again, and nothing fails until a client freezes.
"""

from __future__ import annotations

import threading
import time

import pytest

from agentchatroom import mcp_compat, mcp_server
from agentchatroom.mcp_compat import CompatibleToolManager
from agentchatroom.mcp_server import ServiceBoundToolManager


def _eligible(tool) -> bool:
    return bool(tool.is_async or mcp_compat._can_offload(tool))


def test_every_advertised_mcp_tool_runs_off_the_event_loop():
    offenders = [
        tool.name
        for tool in mcp_server.mcp._tool_manager.list_tools()
        if not _eligible(tool)
    ]

    assert not offenders, (
        "these tools would execute inline on the event loop and freeze every "
        f"other client: {offenders}"
    )


def test_http_and_stdio_adapters_share_the_offloading_manager(service):
    """Both deployment shapes must get the same execution model, not two."""
    stdio_server = mcp_server.mcp
    http_server = mcp_server.create_mcp(service)

    for name, server in (("stdio", stdio_server), ("http", http_server)):
        manager = server._tool_manager
        assert isinstance(manager, ServiceBoundToolManager), name
        assert isinstance(manager, CompatibleToolManager), name
        assert all(_eligible(tool) for tool in manager.list_tools()), name


@pytest.mark.asyncio
async def test_two_blocking_tools_overlap_instead_of_serialising():
    """Two slow tools must run at the same time, not back to back."""
    import anyio
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("overlap-probe")
    probe._tool_manager = ServiceBoundToolManager(None, None)
    block_seconds = 0.3
    threads: set[int] = set()

    def make_blocking(name: str):
        def tool_fn() -> dict:
            threads.add(threading.get_ident())
            time.sleep(block_seconds)
            return {"ok": True, "name": name}

        return tool_fn

    probe.tool(name="probe_slow_one")(make_blocking("one"))
    probe.tool(name="probe_slow_two")(make_blocking("two"))

    loop_thread = threading.get_ident()
    started = time.perf_counter()
    results: list[dict] = []

    async def call(name: str) -> None:
        results.append(await probe._tool_manager.call_tool(name, {}))

    async with anyio.create_task_group() as group:
        group.start_soon(call, "probe_slow_one")
        group.start_soon(call, "probe_slow_two")
    elapsed = time.perf_counter() - started

    assert [item["ok"] for item in results] == [True, True]
    # Which one finishes first is genuinely nondeterministic: they are running at
    # the same time, which is the whole point of this test.
    assert {item["name"] for item in results} == {"one", "two"}
    assert len(threads) == 2, f"both tools reused one worker thread: {threads}"
    assert threads.isdisjoint({loop_thread})
    # Serialised execution would cost 2 * block_seconds; overlap stays near one.
    assert elapsed < block_seconds * 1.75, f"two blocking tools took {elapsed:.3f}s"
