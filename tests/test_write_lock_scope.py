"""Regression tests for the write-lock scope of idempotent writes (#337).

A work report collects git evidence by running several subprocesses. That work
used to happen while the idempotency reservation still held the database write
lock, so one report stalled every other writer for as long as git took, and in
Streamable-HTTP form froze the whole event loop.
"""

from __future__ import annotations

import time

import pytest

from agentchatroom import services
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


def _prepare(service, project_dir, *, name="Lock Scope"):
    project = service.create_project(root_path=str(project_dir), name=name)
    registered = service.register_workspace(
        project["id"],
        host_key="lock-scope-host",
        host_name="Lock Scope Host",
        local_path=str(project_dir),
    )
    session = service.join_room(
        project["id"],
        name="Worker",
        client="codex",
        model="unknown",
        worktree=str(project_dir),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Report with evidence",
        acceptance_criteria=["Evidence is collected"],
        actor_session_id=session["agent"]["id"],
        token=session["token"],
    )["task"]
    service.claim_task(
        project["id"],
        task["id"],
        session["agent"]["id"],
        session["token"],
    )
    return project, session, task


def _tests_evidence():
    return [{"command": "pytest -q", "exit_code": 0, "notes": "suite green"}]


def test_git_evidence_runs_without_holding_the_write_lock(
    settings, project_dir, monkeypatch
):
    """An unrelated writer must get through while git evidence is collected."""
    primary = AgentChatRoomService(Database(settings.database_path), settings)
    primary.initialize()
    project, session, task = _prepare(primary, project_dir)

    # A second service owns its own connection and its own ContextVar, so its
    # writes genuinely contend for the SQLite file lock rather than silently
    # reusing the in-flight transaction.
    contender = AgentChatRoomService(Database(settings.database_path), settings)
    contender.initialize()

    git_calls: list[str] = []
    posted: list[dict] = []
    real_run = services.subprocess.run

    def slow_run(*args, **kwargs):
        git_calls.append("git")
        # If the reservation still held the write lock, this call would block
        # for the full busy timeout and then fail, which is exactly the bug.
        posted.append(
            contender.post_message(
                project["id"],
                body="written while git was running",
                session_id=session["agent"]["id"],
                token=session["token"],
                model_display_name="test-model",
            )
        )
        time.sleep(0.05)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(services.subprocess, "run", slow_run)

    started = time.monotonic()
    primary.submit_work_report(
        project["id"],
        task["id"],
        session_id=session["agent"]["id"],
        token=session["token"],
        summary="Collected evidence",
        files=["README.md"],
        tests=_tests_evidence(),
    )
    elapsed = time.monotonic() - started

    assert git_calls, "work_report never invoked git, so the test proved nothing"
    # Every concurrent write landed while git was still running.
    assert len(posted) == len(git_calls)
    assert all("event_id" in item for item in posted), posted
    assert elapsed < 5, f"concurrent write stalled for {elapsed:.1f}s"


def test_a_failed_idempotent_action_releases_its_reservation(service, project_dir):
    """A retry after a failure must not be blocked by a stale reservation."""
    project, session, task = _prepare(service, project_dir, name="Retry Scope")
    failing = {"count": 0}

    def flaky():
        failing["count"] += 1
        if failing["count"] == 1:
            raise DomainError("transient_failure", "first attempt fails", status_code=409)
        return {"ok": True, "attempt": failing["count"]}

    with pytest.raises(DomainError) as first:
        service._execute_idempotent(
            project_id=project["id"],
            request_id="retry-me",
            operation="test.flaky",
            request_payload={},
            action=flaky,
        )
    assert first.value.code == "transient_failure"

    second = service._execute_idempotent(
        project_id=project["id"],
        request_id="retry-me",
        operation="test.flaky",
        request_payload={},
        action=flaky,
    )
    assert second["attempt"] == 2
    assert second["idempotent_replay"] is False


def test_lock_contention_is_reported_as_a_structured_domain_error(
    settings, project_dir
):
    """A blocked writer gets a retryable DomainError, not a raw sqlite error."""
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()
    project = service.create_project(root_path=str(project_dir), name="Contention")

    blocker = Database(settings.database_path)
    connection = blocker.connect(write=True)
    connection.__enter__()
    try:
        with pytest.raises(DomainError) as contended:
            service.create_task(
                project["id"], title="Blocked", acceptance_criteria=["never written"]
            )
        assert contended.value.code == "database_write_contended"
        assert contended.value.status_code == 503
        assert contended.value.details["required_action"] == "retry_after_short_backoff"
    finally:
        connection.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_a_blocking_sync_mcp_tool_does_not_park_the_event_loop():
    """#337: sync tools must run off the loop so other clients keep being served."""
    import threading

    import anyio
    from mcp.server.fastmcp import FastMCP

    from agentchatroom.mcp_server import ServiceBoundToolManager

    probe = FastMCP("loop-probe")
    probe._tool_manager = ServiceBoundToolManager(None, None)
    seen: dict[str, int] = {}

    @probe.tool(name="probe_blocking_sync_tool")
    def probe_blocking_sync_tool() -> dict:
        seen["thread"] = threading.get_ident()
        time.sleep(0.2)
        return {"ok": True}

    loop_thread = threading.get_ident()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    async with anyio.create_task_group() as group:
        group.start_soon(ticker)
        result = await probe._tool_manager.call_tool("probe_blocking_sync_tool", {})
        group.cancel_scope.cancel()

    assert result == {"ok": True}
    assert seen["thread"] != loop_thread, "the tool ran inline on the event loop"
    # A parked loop cannot tick while a 200ms blocking call is in flight.
    assert ticks > 5, f"event loop only ticked {ticks} times during the tool call"
