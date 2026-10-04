"""#299: transport tombstones only serve bookmarks a client can still replay.

The Bridge opens a fresh ``streamable_http_client`` per forwarded call and
closes it again, so the server reaps a transport -- and would retain a
tombstone for it -- on every single call. Those entries are unreachable: the
Bridge never presents the id again, yet under the old oldest-first eviction they
pushed genuinely waiting bookmarks out of a 256-entry table, which is what
breaks transparent adoption ("透明采纳").
"""

from __future__ import annotations

import logging

import pytest

from agentchatroom import mcp_server
from agentchatroom.bootstrap import RuntimeBinding


@pytest.fixture(autouse=True)
def _isolated_tombstones():
    """Reset the module-level tombstone tables and the liveness probe.

    ``configure_transport_tombstones`` treats ``None`` as "leave unchanged", so
    the probe is cleared here directly; otherwise a probe installed by an
    earlier test would decide later tests' eviction order.
    """
    mcp_server.clear_transport_tombstones()
    mcp_server.clear_runtime_binding()
    previous = mcp_server._tombstone_session_active
    mcp_server._tombstone_session_active = None
    mcp_server.configure_transport_tombstones(limit=256, ttl_seconds=86400.0)
    yield
    mcp_server.clear_transport_tombstones()
    mcp_server.clear_runtime_binding()
    mcp_server._tombstone_session_active = previous
    mcp_server.configure_transport_tombstones(limit=256, ttl_seconds=86400.0)


@pytest.fixture()
def clock(monkeypatch):
    """Drive ``time.monotonic`` so recorded ages are exact.

    Eviction ranks by recorded-at, and Windows only resolves ``monotonic`` to
    ~15ms, so real-time inserts would tie and the victim would fall to an
    arbitrary tie-break. Tests advance this explicitly instead of sleeping.
    """
    state = {"now": 1000.0}
    monkeypatch.setattr(
        mcp_server.time, "monotonic", lambda: state["now"], raising=False
    )

    def advance(seconds: float = 1.0) -> None:
        state["now"] += seconds

    return advance


def _binding(session_id: str, project_id: str = "project_t") -> RuntimeBinding:
    return RuntimeBinding(
        project_id=project_id,
        session_id=session_id,
        token=f"token-{session_id}",
        cursor=0,
        software_key="soft",
        agent_key="member",
        conversation_synced=True,
    )


def _bind(transport_id: str, binding: RuntimeBinding) -> None:
    mcp_server.persist_runtime_binding(
        f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{transport_id}", binding
    )


def test_client_delete_suppresses_the_tombstone():
    """AC1: a transport the client explicitly deleted is never retained."""
    _bind("t-keep", _binding("s-keep"))
    _bind("t-deleted", _binding("s-deleted"))

    mcp_server.note_client_terminated_transport("t-deleted")
    mcp_server.retain_transport_tombstone("t-deleted")
    mcp_server.retain_transport_tombstone("t-keep")

    assert "t-deleted" not in mcp_server._transport_tombstones
    assert "t-keep" in mcp_server._transport_tombstones


def test_delete_drops_a_tombstone_that_already_exists():
    """A late DELETE must also retract a tombstone taken before it arrived."""
    _bind("t-late", _binding("s-late"))
    mcp_server.retain_transport_tombstone("t-late")
    assert "t-late" in mcp_server._transport_tombstones

    mcp_server.note_client_terminated_transport("t-late")

    assert "t-late" not in mcp_server._transport_tombstones


def test_bridge_churn_does_not_evict_a_waiting_bookmark(caplog, clock):
    """AC2: per-call garbage must not push out a genuinely replayable entry.

    Without a liveness probe every entry looks alike, so the tombstone that a
    client may still replay is evicted by age alone -- exactly the reported
    failure. The probe marks the churned Bridge Sessions as gone, so they are
    dropped first and the waiting bookmark survives.
    """
    mcp_server.configure_transport_tombstones(limit=4, ttl_seconds=86400.0)
    gone_sessions = {f"bridge-{index}" for index in range(8)}
    mcp_server.configure_transport_tombstones(
        session_active=lambda project_id, session_id: session_id not in gone_sessions
    )

    # The real bookmark goes in first, so it is strictly the oldest entry.
    _bind("t-waiting", _binding("s-waiting"))
    mcp_server.retain_transport_tombstone("t-waiting")

    for session_id in sorted(gone_sessions):
        clock()
        transport_id = f"t-{session_id}"
        _bind(transport_id, _binding(session_id))
        mcp_server.retain_transport_tombstone(transport_id)

    assert "t-waiting" in mcp_server._transport_tombstones
    assert len(mcp_server._transport_tombstones) <= 4


def test_eviction_prefers_dead_sessions_over_replayable_ones(caplog, clock):
    """AC2: a replayable entry outranks an unreplayable one of the same age."""
    mcp_server.configure_transport_tombstones(limit=2, ttl_seconds=86400.0)
    mcp_server.configure_transport_tombstones(
        session_active=lambda project_id, session_id: session_id == "s-alive"
    )
    for transport_id, session_id in (
        ("t-alive", "s-alive"),
        ("t-dead", "s-dead"),
    ):
        _bind(transport_id, _binding(session_id))
        mcp_server.retain_transport_tombstone(transport_id)

    with caplog.at_level(logging.INFO, logger=mcp_server.__name__):
        clock()
        _bind("t-new-dead", _binding("s-dead-2"))
        mcp_server.retain_transport_tombstone("t-new-dead")

    survivors = set(mcp_server._transport_tombstones)
    assert "t-alive" in survivors
    assert "t-dead" not in survivors
    assert any("Evicted transport tombstone" in record.message for record in caplog.records)


def test_eviction_falls_back_to_age_when_no_probe_is_configured(clock):
    """Without a probe every entry is equally replayable, so age still decides."""
    mcp_server.configure_transport_tombstones(limit=1, ttl_seconds=86400.0)
    _bind("t-old", _binding("s-old"))
    mcp_server.retain_transport_tombstone("t-old")
    clock()
    _bind("t-new", _binding("s-new"))
    mcp_server.retain_transport_tombstone("t-new")

    assert set(mcp_server._transport_tombstones) == {"t-new"}


def test_transport_state_ignores_tombstones_whose_session_already_left():
    """AC3: a tombstone for a closed Session is not reclaim evidence."""
    mcp_server.configure_transport_tombstones(
        session_active=lambda project_id, session_id: False
    )
    _bind("t-left", _binding("s-left"))
    mcp_server.retain_transport_tombstone("t-left")

    assert mcp_server.session_transport_state("project_t", "s-left") == "unknown"


def test_transport_state_still_reports_gone_for_a_replayable_tombstone():
    """The reclaim fast path must survive for a Session that is still open."""
    mcp_server.configure_transport_tombstones(
        session_active=lambda project_id, session_id: True
    )
    _bind("t-live", _binding("s-live"))
    mcp_server.retain_transport_tombstone("t-live")

    assert mcp_server.session_transport_state("project_t", "s-live") == "gone"


def test_liveness_probe_failure_does_not_evict_on_a_guess():
    """A probe that raises must not be read as "this Session is gone"."""

    def exploding(project_id: str, session_id: str) -> bool:
        raise RuntimeError("database unavailable")

    mcp_server.configure_transport_tombstones(session_active=exploding)
    _bind("t-keep", _binding("s-keep"))
    mcp_server.retain_transport_tombstone("t-keep")

    assert mcp_server.session_transport_state("project_t", "s-keep") == "gone"


def test_terminated_bookmarks_expire_with_the_tombstone_ttl(clock):
    """The termination memo is bounded too, or it would grow without limit."""
    mcp_server.configure_transport_tombstones(ttl_seconds=0.01)
    mcp_server.note_client_terminated_transport("t-ancient")

    clock(1.0)
    mcp_server.retain_transport_tombstone("t-ancient")  # triggers the prune

    assert "t-ancient" not in mcp_server._client_terminated_transports
