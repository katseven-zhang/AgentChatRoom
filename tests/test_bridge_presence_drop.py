"""Task #291: the Bridge must stop heartbeating a Session that is gone.

`session_leave` is answered by the center, but the Bridge popped the registry
entry under whatever ``session_id`` the client happened to send -- and the
protocol fills that in server-side, so the key was usually ``""``. A closed
Session therefore kept a fresh upstream transport, a refused write and a warning
every cycle; ``session_closed`` was not even in the drop set.
"""

from __future__ import annotations

import json

import mcp.types as types
import pytest

from agentchatroom.contracts import PRESENCE_DROP_ERROR_CODES
from agentchatroom.mcp_bridge import BridgePresenceManager, BridgeSettings

PROJECT_ID = "project_presence"
SESSION_ID = "agent_presence"

JOIN_RESULT = {
    "ok": True,
    "result": {
        "project": {"id": PROJECT_ID},
        "agent": {"id": SESSION_ID},
        "token": "token-under-test",
    },
}


def _result(payload: dict) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))]
    )


class FakeUpstream:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        payload = self.responses.pop(0) if self.responses else {"ok": True, "result": {}}
        return _result(payload)


def _manager(upstream) -> BridgePresenceManager:
    return BridgePresenceManager(
        upstream,
        BridgeSettings(
            server_url="http://center.invalid/mcp",
            presence_keepalive_interval_seconds=0.01,
        ),
    )


@pytest.mark.asyncio
async def test_leave_without_a_named_session_stops_the_heartbeat():
    upstream = FakeUpstream()
    manager = _manager(upstream)
    manager.observe("room_join", {}, _result(JOIN_RESULT))
    assert SESSION_ID in manager.sessions

    # A client is not required to name its own Session: the center resolves it
    # from the credential, and the response carries the Session that closed.
    manager.observe(
        "session_leave",
        {},
        _result({"ok": True, "result": {"agent": {"id": SESSION_ID}}}),
    )

    assert manager.sessions == {}
    await manager.heartbeat_once()
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_session_named_by_the_client_is_still_honoured():
    upstream = FakeUpstream()
    manager = _manager(upstream)
    manager.observe("room_join", {}, _result(JOIN_RESULT))

    manager.observe(
        "session_leave",
        {"session_id": SESSION_ID},
        _result({"ok": True, "result": {}}),
    )

    assert manager.sessions == {}


@pytest.mark.asyncio
async def test_a_closed_session_is_dropped_after_the_heartbeat_notices():
    for code in sorted(PRESENCE_DROP_ERROR_CODES):
        upstream = FakeUpstream([{"ok": False, "error": {"code": code}}])
        manager = _manager(upstream)
        manager.observe("room_join", {}, _result(JOIN_RESULT))

        await manager.heartbeat_once()

        assert len(upstream.calls) == 1, code
        assert manager.sessions == {}, code

        await manager.heartbeat_once()

        assert upstream.calls == ["session_heartbeat"], code
