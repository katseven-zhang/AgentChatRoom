# audit-allow-file:private_lan_address
"""Lifecycle and background-thread robustness (#360).

Four independent leaks and stalls shared one theme: a process-global registry
that nothing bounded, and work scheduled outside the transaction or the lock
that was supposed to cover it.
"""
from __future__ import annotations

import threading
import time

import pytest

from agentchatroom import mcp_server
from agentchatroom.api import (
    BACKUP_TICK_SLICE_SECONDS,
    _wait_for_backup_tick,
    auto_backup_worker,
    start_auto_backup_worker,
)
from agentchatroom.config import Settings
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService, TokenTouchBuffer


@pytest.fixture
def fresh_mcp_bindings():
    """Restore the process-global MCP tables so a test cannot leak into another."""
    saved_bindings = dict(mcp_server._session_bindings)
    saved_written = dict(mcp_server._session_binding_written)
    saved_keys = dict(mcp_server._transport_keys)
    saved_tombstones = dict(mcp_server._transport_tombstones)
    saved_pending = dict(mcp_server._pending_restores)
    saved_limit = mcp_server._binding_limit
    saved_ttl = mcp_server._binding_ttl_seconds
    yield
    mcp_server._session_bindings.clear()
    mcp_server._session_bindings.update(saved_bindings)
    mcp_server._session_binding_written.clear()
    mcp_server._session_binding_written.update(saved_written)
    mcp_server._transport_keys.clear()
    mcp_server._transport_keys.update(saved_keys)
    mcp_server._transport_tombstones.clear()
    mcp_server._transport_tombstones.update(saved_tombstones)
    mcp_server._pending_restores.clear()
    mcp_server._pending_restores.update(saved_pending)
    mcp_server._binding_limit = saved_limit
    mcp_server._binding_ttl_seconds = saved_ttl


def _two_projects(service, tmp_path):
    """Two Projects, each with its root actually on disk."""
    roots = []
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)
    return (
        service.create_project(root_path=str(roots[0]), name="Alpha"),
        service.create_project(root_path=str(roots[1]), name="Beta"),
    )


def _http_key(session_id: str) -> str:
    return f"{mcp_server.HTTP_TRANSPORT_KEY_PREFIX}{session_id}"


def _binding() -> "object":
    from agentchatroom.bootstrap import RuntimeBinding

    return RuntimeBinding(
        project_id="project_1",
        session_id="session_1",
        token="token",
        cursor=0,
        software_key="key",
        agent_key="agent_1",
        conversation_synced=True,
    )


# ---------------------------------------------------------------- session bindings


def test_http_binding_keys_age_out_instead_of_accumulating_forever(
    fresh_mcp_bindings,
) -> None:
    """A key minted from the mcp-session-id header had no reclamation path.

    Only the weakref callback reclaims a stdio key, and only a reaped transport
    reclaims an HTTP one - so a client that just disconnects left its Room
    Session binding in the table for the rest of the process lifetime.
    """
    binding = _binding()
    mcp_server.configure_session_bindings(limit=4, ttl_seconds=3600)
    for index in range(4):
        mcp_server.persist_runtime_binding(_http_key(f"expired-{index}"), binding)

    # A key older than the window is dropped even though the table is small.
    mcp_server._session_binding_written[_http_key("expired-0")] = (
        time.monotonic() - 7200
    )
    mcp_server.persist_runtime_binding(_http_key("fresh-0"), binding)

    assert _http_key("expired-0") not in mcp_server._session_bindings
    assert _http_key("fresh-0") in mcp_server._session_bindings


def test_http_binding_keys_are_capped_by_count(fresh_mcp_bindings) -> None:
    binding = _binding()
    mcp_server.configure_session_bindings(limit=3, ttl_seconds=86400)
    for index in range(10):
        mcp_server.persist_runtime_binding(_http_key(f"session-{index}"), binding)

    assert len(mcp_server._session_binding_written) <= 3
    # The newest survives: dropping the oldest is what keeps the live one.
    assert _http_key("session-9") in mcp_server._session_bindings
    assert _http_key("session-0") not in mcp_server._session_bindings


def test_configuring_a_smaller_limit_prunes_immediately(fresh_mcp_bindings) -> None:
    """Lowering the bound has to reclaim what is already held, not only future writes."""
    binding = _binding()
    mcp_server.configure_session_bindings(limit=50, ttl_seconds=86400)
    for index in range(20):
        mcp_server.persist_runtime_binding(_http_key(f"held-{index}"), binding)
    assert len(mcp_server._session_binding_written) == 20

    mcp_server.configure_session_bindings(limit=5)

    assert len(mcp_server._session_binding_written) <= 5


def test_a_live_stdio_binding_is_never_pruned(fresh_mcp_bindings) -> None:
    """The prune must not unbind a connection that is still running.

    A stdio key owned by a live connection object is reclaimed by its weakref
    callback; pruning it by age would silently drop a writable binding for a
    perfectly healthy session.
    """
    binding = _binding()

    class Session:
        pass

    session = Session()
    key = mcp_server.mcp_session_key(_ContextWithSession(session))
    mcp_server.persist_runtime_binding(key, binding)
    assert key not in mcp_server._session_binding_written

    mcp_server.configure_session_bindings(limit=1, ttl_seconds=0.001)
    time.sleep(0.01)

    assert mcp_server.get_runtime_binding(key) is binding


class _ContextWithSession:
    def __init__(self, session) -> None:
        self.session = session


def test_stdio_local_singleton_is_not_pruned(fresh_mcp_bindings) -> None:
    mcp_server.configure_session_bindings(limit=1, ttl_seconds=0.001)
    mcp_server.persist_runtime_binding("stdio:local", None)
    time.sleep(0.01)
    mcp_server.persist_runtime_binding(_http_key("other"), None)

    assert "stdio:local" in mcp_server._session_bindings


def test_a_reaped_binding_leaves_the_age_index(fresh_mcp_bindings) -> None:
    """Once the tombstone table owns a binding, the prune must not race adoption."""
    binding = _binding()
    mcp_server.configure_session_bindings(limit=64, ttl_seconds=3600)
    mcp_server.persist_runtime_binding(_http_key("reaped"), binding)

    retained = mcp_server.retain_transport_tombstone("reaped")

    assert retained is binding
    assert _http_key("reaped") not in mcp_server._session_binding_written
    # Adoption still works after the prune window would have elapsed.
    mcp_server.configure_session_bindings(limit=64, ttl_seconds=0.001)
    time.sleep(0.01)
    assert mcp_server.lookup_transport_tombstone("reaped", "key") is binding


# ------------------------------------------------------- idempotent replay guards


def test_replay_liveness_is_read_outside_the_write_transaction(service, project) -> None:
    """The guard reads a lease or session row; doing that inside the write lock
    serialised every other writer for the length of a second connection's query.
    """
    import contextlib

    open_writes = 0
    observed: list[int] = []

    class TrackingDatabase:
        """Counts how many write transactions are open while the guard runs."""

        def __init__(self, inner):
            self._inner = inner

        @contextlib.contextmanager
        def connect(self, *, write=False):
            nonlocal open_writes
            if not write:
                with self._inner.connect() as connection:
                    yield connection
                return
            open_writes += 1
            try:
                with self._inner.connect(write=True) as connection:
                    yield connection
            finally:
                open_writes -= 1

        def __getattr__(self, item):
            return getattr(self._inner, item)

    service.database = TrackingDatabase(service.database)

    def guard(_replay):
        observed.append(open_writes)
        return True

    first = service._execute_idempotent(
        project_id=project["id"],
        request_id="req-outside",
        operation="lease.acquire",
        request_payload={"path": "src/**"},
        action=lambda: {"lease": {"id": "lease_1"}},
        replay_guard=guard,
    )
    assert first["idempotent_replay"] is False

    replayed = service._execute_idempotent(
        project_id=project["id"],
        request_id="req-outside",
        operation="lease.acquire",
        request_payload={"path": "src/**"},
        action=lambda: pytest.fail("a live replay must not run the action again"),
        replay_guard=guard,
    )

    assert replayed["idempotent_replay"] is True
    assert replayed["lease"]["id"] == "lease_1"
    # The guard ran with no write transaction held, which is the whole point:
    # the liveness read no longer serialises every other writer behind it. It
    # ran once, not twice - the answer is computed once and reused.
    assert observed == [0]


# ------------------------------------------------------------ TokenTouchBuffer


def test_close_joins_the_worker_before_its_final_flush(settings) -> None:
    """close() must not return while the worker is still inside a flush.

    Without the join, close()'s flush drains the queue while the worker is
    still writing, and the worker's failure path then puts the update back into
    _pending - after the final flush already ran, so it is never written.
    """
    service = AgentChatRoomService(Database(settings.database_path), settings)
    buffer = TokenTouchBuffer(service, interval_seconds=3600, min_calls=1)

    worker_in_flush = threading.Event()
    release_worker = threading.Event()

    original_flush = buffer.flush
    state = {"calls": 0}

    def gated_flush():
        state["calls"] += 1
        if state["calls"] == 1:
            # The worker's own flush: hold it open so close() has to wait.
            worker_in_flush.set()
            release_worker.wait(timeout=5)
            raise RuntimeError("write lost the race with close")
        return original_flush()

    buffer.flush = gated_flush
    buffer._pending["cred_racing"] = "2026-01-01T00:00:00Z"
    with buffer._lock:
        buffer._ensure_thread_locked()
    # The worker sleeps on the wakeup for a full interval otherwise.
    buffer._wakeup.set()
    worker_in_flush.wait(timeout=5)
    assert worker_in_flush.is_set(), "the worker must be inside its flush"

    closed = threading.Event()

    def run_close():
        buffer.close()
        closed.set()

    closer = threading.Thread(target=run_close, daemon=True)
    closer.start()
    # close() must still be waiting: it cannot finish while the worker holds a
    # pending update that the final flush has not drained yet.
    assert not closed.wait(timeout=0.3), "close() returned while the worker was running"

    release_worker.set()
    assert closed.wait(timeout=5), "close() must finish once the worker exits"
    closer.join(timeout=5)
    assert buffer._pending == {}, "a re-queue after the final flush is stranded work"


def test_close_unregisters_the_atexit_callback(settings, monkeypatch) -> None:
    """Every rebuilt service left a live atexit entry pointing at a closed database.

    atexit exposes no public registry, so the module's own register/unregister
    calls are recorded instead - that is the contract close() has to honour.
    """
    import agentchatroom.services as services_module

    registered: list = []
    unregistered: list = []
    monkeypatch.setattr(
        services_module.atexit, "register", lambda callback: registered.append(callback)
    )
    monkeypatch.setattr(
        services_module.atexit,
        "unregister",
        lambda callback: unregistered.append(callback),
    )

    service = AgentChatRoomService(Database(settings.database_path), settings)
    buffer = TokenTouchBuffer(service, interval_seconds=3600, min_calls=1)
    assert buffer.flush in registered

    buffer.close()

    assert buffer.flush in unregistered, (
        "close() must unregister, or interpreter exit flushes a closed database"
    )


def test_a_note_racing_close_is_dropped_not_queued(settings) -> None:
    service = AgentChatRoomService(Database(settings.database_path), settings)
    buffer = TokenTouchBuffer(service, interval_seconds=3600, min_calls=1)
    buffer.close()

    buffer.note("cred_after_close")

    assert buffer._pending == {}


def test_a_failed_final_flush_does_not_requeue(settings, monkeypatch) -> None:
    """Nothing retries a closed buffer, so a re-queued update would be stranded."""
    service = AgentChatRoomService(Database(settings.database_path), settings)
    buffer = TokenTouchBuffer(service, interval_seconds=3600, min_calls=1)

    class FailingDatabase:
        def connect(self, *, write=False):
            raise RuntimeError("database is closed")

        def close(self):
            pass

    service.database = FailingDatabase()
    buffer.note("cred_1")
    buffer._closed = True

    buffer.flush()

    assert buffer._pending == {}


# ------------------------------------------------------------ backup scheduler


class _WorkerSettings:
    auto_backup_enabled = True
    auto_backup_interval_seconds = 3600
    auto_backup_max_kept = 10


def test_a_shortened_interval_applies_without_waiting_out_the_old_period() -> None:
    """The wait timeout was evaluated on entry, so lowering it did nothing.

    The scheduler slept out the interval that was already in flight, which for
    the default period meant a management save looked like it had no effect.
    """
    stop = threading.Event()
    reschedule = threading.Event()
    observed: list[int] = []

    class Stub:
        auto_backup_enabled = True
        auto_backup_interval_seconds = 86400

        def create_backup(self, *, source, max_kept=None):
            observed.append(max_kept)
            return {"ok": True}

    worker = threading.Thread(
        target=auto_backup_worker,
        args=(Stub(), _WorkerSettings(), stop, {"auto_backup_interval_seconds": 86400}),
        kwargs={"reschedule": reschedule},
        daemon=True,
    )
    worker.start()
    try:
        # The new interval is what the next tick must use.
        reschedule.set()
        deadline = time.monotonic() + 5
        while not observed and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        worker.join(timeout=5)

    assert observed, "the reschedule signal must produce a tick"


def test_the_tick_slice_never_sleeps_past_the_deadline() -> None:
    """A slice must shorten the remaining wait, not restart the period."""
    stop = threading.Event()
    reschedule = threading.Event()

    started = time.monotonic()
    stop.wait(timeout=0)  # keep the event type honest for readers
    assert _wait_for_backup_tick(stop, reschedule, 1) is False
    elapsed = time.monotonic() - started

    assert BACKUP_TICK_SLICE_SECONDS <= 1
    assert 0.5 <= elapsed <= 2.0, f"one tick took {elapsed:.2f}s"


def test_a_set_stop_event_ends_the_worker_immediately() -> None:
    stop = threading.Event()
    reschedule = threading.Event()
    stop.set()

    started = time.monotonic()
    assert _wait_for_backup_tick(stop, reschedule, 86400) is True
    assert time.monotonic() - started < 1.0


def test_start_auto_backup_worker_binds_the_reschedule_signal() -> None:
    """The signal has to reach the worker; Thread takes no such keyword."""
    import agentchatroom.api as api_module

    seen: dict = {}

    def fake_worker(service, settings, stop_event, overrides=None, *, reschedule=None):
        seen["reschedule"] = reschedule

    original = api_module.auto_backup_worker
    api_module.auto_backup_worker = fake_worker
    try:
        signal = threading.Event()
        start_auto_backup_worker(
            object(), _WorkerSettings(), threading.Event(), {}, reschedule=signal
        )
        for _ in range(100):
            if "reschedule" in seen:
                break
            time.sleep(0.01)
    finally:
        api_module.auto_backup_worker = original

    assert seen.get("reschedule") is signal


# ------------------------------------------------------- global host ownership


def test_a_project_cannot_rename_a_host_another_project_registered(
    service, tmp_path
) -> None:
    """hosts has no project scope, so one Project could rename another's host."""
    first, second = _two_projects(service, tmp_path)

    service.register_workspace(
        first["id"],
        host_key="shared-host",
        host_name="Original Name",
        local_path=str(tmp_path / "alpha"),
        host_metadata={"os": "first"},
    )

    hijacked = service.register_workspace(
        second["id"],
        host_key="shared-host",
        host_name="Hijacked Name",
        local_path=str(tmp_path / "beta"),
        host_metadata={"os": "second"},
    )

    # The row itself keeps its identity fields...
    assert hijacked["host"]["name"] == "Original Name"
    assert hijacked["host"]["metadata"] == {"os": "first"}
    # ...and both Projects display the name they did not choose.
    for project in (first, second):
        assert service.list_workspaces(project["id"])[0]["host_name"] == "Original Name"


def test_registration_still_refreshes_last_seen_on_a_shared_host(
    service, tmp_path
) -> None:
    """Refusing the rename must not freeze the host's liveness."""
    first, second = _two_projects(service, tmp_path)

    created = service.register_workspace(
        first["id"],
        host_key="shared-host",
        host_name="Host",
        local_path=str(tmp_path / "alpha"),
    )
    before = created["host"]["last_seen_at"]

    time.sleep(0.01)
    refreshed = service.register_workspace(
        second["id"],
        host_key="shared-host",
        host_name="Host",
        local_path=str(tmp_path / "beta"),
    )

    assert refreshed["host"]["last_seen_at"] > before
    assert refreshed["host"]["id"] == created["host"]["id"]


def test_management_authority_may_rename_a_host(service, tmp_path) -> None:
    (first,) = _two_projects(service, tmp_path)[:1]

    service.register_workspace(
        first["id"],
        host_key="managed-host",
        host_name="Before",
        local_path=str(tmp_path / "alpha"),
    )
    service.register_workspace(
        first["id"],
        host_key="managed-host",
        host_name="After",
        local_path=str(tmp_path / "alpha"),
        host_updates_allowed=True,
    )

    assert service.list_workspaces(first["id"])[0]["host_name"] == "After"


def test_a_refused_rename_is_visible_in_the_event(service, tmp_path) -> None:
    (first,) = _two_projects(service, tmp_path)[:1]
    checkout = tmp_path / "alpha-checkout"
    checkout.mkdir()

    service.register_workspace(
        first["id"],
        host_key="shared-host",
        host_name="Kept",
        local_path=str(tmp_path / "alpha"),
    )
    service.register_workspace(
        first["id"],
        host_key="shared-host",
        host_name="Ignored",
        local_path=str(checkout),
    )

    events = service.query_audit(first["id"], event_type="workspace.registered")
    assert events["events"], "the registration must still be recorded"
    assert events["events"][-1]["payload"]["host_rename_refused"] is True


def test_the_workspace_itself_is_still_written_for_a_refused_rename(
    service, tmp_path
) -> None:
    """Only the shared host row is protected; the workspace row is per Project."""
    first, second = _two_projects(service, tmp_path)

    service.register_workspace(
        first["id"],
        host_key="shared-host",
        host_name="Host",
        local_path=str(tmp_path / "alpha"),
    )
    service.register_workspace(
        second["id"],
        host_key="shared-host",
        host_name="Host",
        local_path=str(tmp_path / "beta"),
    )

    # #255: local_path is the canonical identity key; the caller-typed text
    # the rename test cares about lives in display_path.
    paths = {entry["display_path"] for entry in service.list_workspaces(second["id"])}
    assert paths == {str(tmp_path / "beta")}
