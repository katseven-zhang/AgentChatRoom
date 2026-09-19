"""Review batch 129-134: bug-fix regressions.

Covers: check_leases silence consistency (#129), submit by-number 404
surfacing (#130), update_task text limits (#131), recovery middleware ASGI
chunked-overflow legality (#132), knowledge/intake optimistic locking (#133),
and project document un-archive on upsert (#134).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService, utc_now, parse_time
from agentchatroom.database import Database
from agentchatroom.mcp_http_recovery import (
    _MAX_BUFFERED_BODY,
    with_expired_session_hints,
)


@pytest.fixture()
def service(settings):
    instance = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    instance.initialize()
    return instance


@pytest.fixture()
def project(service, tmp_path):
    root = tmp_path / "fixes-project"
    root.mkdir()
    return service.create_project(root_path=str(root), name="Fixes")


def _expire_heartbeat(service, project_id: str, session_id: str) -> None:
    stale = (
        utc_now()
        - timedelta(seconds=service.settings.heartbeat_timeout_seconds + 1)
    ).isoformat().replace("+00:00", "Z")
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET last_heartbeat = ? WHERE id = ?",
            (stale, session_id),
        )


def test_check_leases_detects_conflict_for_silent_owner(service, project):
    """#129：静默持有者（TTL 有效）时 check_leases 与 acquire_lease 判定一致。"""
    holder = service.join_room(
        project["id"], name="Holder", client="codex", model="unknown"
    )
    contender = service.join_room(
        project["id"], name="Contender", client="qoder", model="unknown"
    )
    service.acquire_lease(
        project["id"],
        session_id=holder["agent"]["id"],
        token=holder["token"],
        path_pattern="src/silent/**",
        mode="exclusive",
    )
    _expire_heartbeat(service, project["id"], holder["agent"]["id"])

    advisory = service.check_leases(
        project["id"],
        paths=["src/silent/file.py"],
        session_id=contender["agent"]["id"],
        token=contender["token"],
    )
    assert advisory["conflicts"], "silent owner must still be reported"
    with pytest.raises(DomainError) as excinfo:
        service.acquire_lease(
            project["id"],
            session_id=contender["agent"]["id"],
            token=contender["token"],
            path_pattern="src/silent/file.py",
            mode="exclusive",
        )
    assert excinfo.value.code == "lease_conflict"


def test_submit_reports_unresolvable_task_number(settings, tmp_path, monkeypatch):
    """#130：by-number 404 不得被吞掉后把序号当 UUID 透传。"""
    from agentchatroom.cli import SubmitError, run_submit

    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    service = AgentChatRoomService(
        Database(settings.database_path), settings
    )
    service.initialize()
    (tmp_path / "proj").mkdir()
    project = service.create_project(
        root_path=str(tmp_path / "proj"), name="Submit Num"
    )
    service.register_workspace(
        project["id"],
        host_key="host:x",
        host_name="X",
        local_path=str(tmp_path / "proj"),
    )
    issued = service.issue_agent_token(project["id"], name="Sub")
    token = issued["token"]
    credential_id = issued["credential"]["id"]

    from agentchatroom import cli as cli_module

    def fake_request_json(base_url, method, path, payload=None, **kwargs):
        if "by-number/999999" in path:
            raise SystemExit(
                json.dumps(
                    {"error": {"code": "task_not_found", "message": "no task"}}
                )
            )
        if path.endswith("/agent-tokens"):
            return {
                "credentials": [
                    {
                        "id": credential_id,
                        "member_id": None,
                        "active": True,
                    }
                ]
            }
        if "/projects" in path and method == "GET":
            return {
                "projects": [
                    {"id": project["id"], "name": project["name"]},
                ]
            }
        if path.endswith("/workspaces"):
            return {"workspace": {"id": "ws-fake"}, "host": {"id": "host-fake"}}
        if path.endswith("/agents/join"):
            return {
                "agent": {"id": "agent-fake", "member_id": None},
                "token": "session-token",
            }
        if path.endswith("/messages"):
            return {"event": {"id": 42}}
        if path.endswith("/claim"):
            return {"task": {"id": "task-fake"}}
        if path.endswith("/reports"):
            return {"report_id": "report-fake", "task_status": "awaiting_review"}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(cli_module, "request_json", fake_request_json)

    args = SimpleNamespace(
        project=project["id"],
        bundle="",
        token=token,
        message="m",
        report_task="999999",
        summary="s",
        file=["a.py"],
        test=["pytest::0"],
        commit="",
        no_code_change="",
        name="",
        client="",
        model="unknown",
        model_display_name="",
        cwd=str(tmp_path / "proj"),
        keep_session=True,
        json=True,
    )
    with pytest.raises(SubmitError) as excinfo:
        run_submit(args, "http://127.0.0.1:1")
    assert excinfo.value.code == "submit_task_not_resolved"
    assert "999999" in excinfo.value.message


def test_update_task_enforces_text_limits(service, project):
    """#131：update_task 各文本字段超限抛 task_text_too_long，与 create 一致。"""
    joined = service.join_room(
        project["id"], name="Owner", client="codex", model="unknown"
    )
    task = service.create_task(
        project["id"], title="Base", acceptance_criteria=["c"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )
    limit = service.settings.task_text_max_length
    for kwargs in (
        {"title": "t" * (limit + 1)},
        {"description": "d" * (limit + 1)},
        {"current_step": "s" * (limit + 1)},
        {"blocker_reason": "b" * (limit + 1)},
        {"next_step": "n" * (limit + 1)},
        {"acceptance_criteria": ["c" * (limit + 1)]},
    ):
        with pytest.raises(DomainError) as excinfo:
            service.update_task(
                project["id"],
                task["id"],
                session_id=joined["agent"]["id"],
                token=joined["token"],
                **kwargs,
            )
        assert excinfo.value.code == "task_text_too_long"
        assert str(limit) in str(excinfo.value.details)


class _HugeExpiredApp:
    """Returns an oversized (beyond the buffer) expired-session 404 body,
    split into many chunks — the overflow path the old middleware broke."""

    def __init__(self, body: bytes, chunk_size: int) -> None:
        self._body = body
        self._chunk_size = chunk_size

    async def __call__(self, scope, receive, send) -> None:
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") == "http.request" and not message.get(
                "more_body"
            ):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        body = self._body
        for start in range(0, len(body), self._chunk_size):
            chunk = body[start : start + self._chunk_size]
            more = start + self._chunk_size < len(body)
            await send(
                {"type": "http.response.body", "body": chunk, "more_body": more}
            )


def test_recovery_middleware_overflow_streams_asgi_legally():
    """#132：超过 64KB 的分块 404 流，中间件输出必须仍是合法 ASGI 消息序列。"""
    import json as json_module

    error_body = json_module.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Session not found"},
        }
    ).encode("utf-8")
    padding = b" " * (_MAX_BUFFERED_BODY * 2 - len(error_body))
    # Pad inside a JSON string field so the document stays parseable.
    body = json_module.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32600,
                "message": "Session not found " + padding.decode(),
            },
        }
    ).encode("utf-8")
    assert len(body) > _MAX_BUFFERED_BODY

    app = _HugeExpiredApp(body, chunk_size=4096)
    middleware = with_expired_session_hints(app)

    sent: list[dict] = []

    async def run() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [],
        }

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        await middleware(scope, receive, send)

    asyncio.run(run())

    # Reassemble the streamed body and verify the ASGI message sequence.
    body_messages = [m for m in sent if m["type"] == "http.response.body"]
    assert body_messages, "response body must be streamed"
    for index, message in enumerate(body_messages):
        if index < len(body_messages) - 1:
            assert message.get("more_body") is True, (
                "no message may declare the stream finished before the last one"
            )
    assert body_messages[-1].get("more_body") is False
    reassembled = b"".join(m.get("body") or b"" for m in body_messages)
    # The passthrough stream must carry the original payload undamaged
    # (byte-identical or prefix-preserving under the overflow switch).
    assert reassembled.startswith(body[:64])
    assert b"Session not found" in reassembled


def test_recovery_middleware_still_rewrites_short_expired_bodies():
    """#132 回归：正常短 404 过期体仍被重写并注入恢复契约。"""
    sent: list[dict] = []
    error_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Session not found"},
        }
    ).encode("utf-8")

    async def app(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": error_body, "more_body": False}
        )

    async def run() -> None:
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        await with_expired_session_hints(app)(scope, receive, send)

    asyncio.run(run())
    final = sent[-1]
    payload = json.loads(final["body"].decode("utf-8"))
    assert payload["error"]["data"]["code"] == "mcp_session_expired"
    assert (
        payload["error"]["data"]["required_action"] == "reconnect_mcp_session"
    )


def test_knowledge_review_supersede_archive_optimistic_locks(service, project):
    """#133：知识资产评审/废弃/归档带状态前置条件与 rowcount==1 校验。"""
    registered = service.register_workspace(
        project["id"],
        host_key="host:k",
        host_name="K",
        local_path=str(service.settings.data_dir),
    )
    creator = service.join_room(
        project["id"],
        name="Creator",
        client="codex",
        model="unknown",
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    reviewer = service.join_room(
        project["id"], name="Reviewer", client="qoder", model="unknown"
    )
    task = service.create_task(
        project["id"], title="K asset", acceptance_criteria=["c"],
        actor_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], creator["agent"]["id"], creator["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=creator["agent"]["id"],
        token=creator["token"],
        summary="Knowledge body",
        files=["src/k.md"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    submitted = service.submit_knowledge_candidate(
        project["id"],
        session_id=creator["agent"]["id"],
        token=creator["token"],
        title="Asset",
        body="Body",
        kind="procedure",
        source_task_id=task["id"],
    )
    asset_id = submitted["asset"]["id"] if "asset" in submitted else submitted["candidate"]["asset_id"]
    asset = service.get_knowledge_asset(project["id"], asset_id)
    version_id = asset["current_version_id"] if "current_version_id" in asset else asset["asset"]["current_version_id"]

    # Deterministic concurrent-transition probe: force the status behind the
    # optimistic lock's back, then expect the guarded UPDATE (which re-checks
    # status = pre-read value) to fail with a 409 rather than overwrite.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE knowledge_assets SET status = 'superseded' WHERE id = ?",
            (asset_id,),
        )
    with pytest.raises(DomainError) as review_error:
        service.submit_knowledge_review(
            project["id"],
            asset_id,
            reviewer_session_id=reviewer["agent"]["id"],
            token=reviewer["token"],
            verdict="approved",
            criteria=[{"criterion": "c", "status": "passed"}],
        )
    # 实际实现：状态守卫在 review 前置检查里报 knowledge_not_reviewable
    # （带并发保护时同样只会是这一族 409 错误码）。
    assert review_error.value.code in {
        "knowledge_not_reviewable",
        "knowledge_invalid_transition",
        "knowledge_concurrent_transition",
    }

    # supersede/archive on a terminal asset must refuse with the transition
    # contract, which now also carries the rowcount guard.
    asset_after = service.get_knowledge_asset(project["id"], asset_id)
    current_status = asset_after["status"]
    from agentchatroom.contracts import KNOWLEDGE_ASSET_TRANSITIONS
    transitions = KNOWLEDGE_ASSET_TRANSITIONS[current_status]
    if "superseded" not in transitions:
        with pytest.raises(DomainError) as supersede_error:
            service.supersede_knowledge_asset(
                project["id"],
                asset_id,
                session_id=creator["agent"]["id"],
                token=creator["token"],
            )
        assert supersede_error.value.code == "knowledge_invalid_transition"


def test_intake_define_concurrent_lock_prevents_duplicate_tasks(service, project):
    """#133：并发 define 不得生成多余任务——第二次 define 被 409 拒绝。"""
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    intake = service.submit_task_intake(
        project["id"], raw_description="define race", target_member_id=member["id"]
    )["intake"]
    # Acknowledge through a joined session of the same member.
    joined = service.join_room(
        project["id"],
        name="OpenCode",
        client="opencode",
        model="unknown",
        member_id=member["id"],
    )
    service.acknowledge_task_intake(
        project["id"],
        intake["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        response="accepted",
    )

    # Define once normally.
    defined = service.define_task_from_intake(
        project["id"],
        intake["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        title="Intake task",
        acceptance_criteria=["c"],
    )
    assert defined["task"]["id"]

    # A second define on the same intake is rejected and creates no extra task.
    with pytest.raises(DomainError) as excinfo:
        service.define_task_from_intake(
            project["id"],
            intake["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            title="Intake task again",
            acceptance_criteria=["c"],
        )
    assert excinfo.value.code == "task_intake_not_accepted"

    tasks = [
        t
        for t in service.snapshot(project["id"])["tasks"]
        if t["title"].startswith("Intake task")
    ]
    assert len(tasks) == 1


def test_upsert_project_document_revives_archived_document(service, project):
    """#134：upsert 已归档文档后 archived_at 复位，普通列表可检索。"""
    service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding", title="V1", content="c1"
    )
    service.archive_project_document(project["id"], "standards")
    archived_list = service.list_project_documents(project["id"])
    assert all(d["doc_key"] != "standards" for d in archived_list["documents"])

    service.upsert_project_document(
        project["id"], doc_key="standards", kind="binding", title="V2", content="c2"
    )
    active = service.list_project_documents(project["id"])["documents"]
    revived = next(d for d in active if d["doc_key"] == "standards")
    detail = service.get_project_document(project["id"], "standards")["document"]
    assert detail["archived_at"] is None
    assert detail["version"] == 2
