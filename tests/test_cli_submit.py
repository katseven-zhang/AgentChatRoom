"""#118: one-shot REST submission fallback (``agentchatroom submit``)."""

from __future__ import annotations

import argparse
import asyncio
import socket
from dataclasses import replace

import pytest
import uvicorn

from agentchatroom.api import create_app
from agentchatroom.cli import SubmitError, _decode_submit_bundle, run_submit


async def _serve(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert server.started
    return server, task, f"http://127.0.0.1:{port}"


async def _stop(server, task) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


def _args(**overrides) -> argparse.Namespace:
    payload: dict = {
        "project": "Submit Project",
        "bundle": "",
        "token": "",
        "message": "",
        "report_task": "",
        "summary": "",
        "file": [],
        "test": [],
        "commit": "",
        "no_code_change": "",
        "name": "",
        "client": "",
        "model": "unknown",
        "model_display_name": "",
        "cwd": "",
        "keep_session": False,
    }
    payload.update(overrides)
    return argparse.Namespace(**payload)


def _prepared_project(app, root_path, name: str = "Submit Project"):
    service = app.state.service
    project = service.create_project(root_path=str(root_path), name=name)
    member = service.create_project_member(
        project["id"], member_key="opencode", name="OpenCode"
    )["member"]
    issued = service.issue_agent_token(
        project["id"], name="OpenCode submit", member_id=member["id"]
    )
    credential = {"id": issued["credential"]["id"], "token": issued["token"]}
    return project, member, credential


@pytest.mark.asyncio
async def test_submit_posts_message_and_report_with_only_a_credential(
    settings, project_dir
):
    """AC1-3：MCP 全失效场景下仅凭凭据完成提交；workspace 自动注册；只 join 一次。"""
    app = create_app(
        replace(
            settings,
            heartbeat_timeout_seconds=30,
            presence_keepalive_interval_seconds=0.05,
        )
    )
    project, member, credential = _prepared_project(app, project_dir)
    task = app.state.service.create_task(
        project["id"],
        title="Submit fallback task",
        acceptance_criteria=["evidence lands without MCP"],
    )["task"]

    server, server_task, url = await _serve(app)
    try:
        result = await asyncio.to_thread(
            run_submit,
            _args(
                token=credential["token"],
                message="submit fallback works without MCP",
                report_task=str(task["task_number"]),
                summary="Implemented the fallback",
                file=["src/example.py"],
                test=["pytest tests/test_cli_submit.py::0"],
                cwd=str(project_dir),
            ),
            url,
        )

        assert result["ok"] is True
        assert result["left"] is True
        assert result["workspace_id"], "workspace must be auto-registered"
        # AC3: the one-shot run joins exactly once — no duplicate sessions.
        agents = app.state.service.snapshot(project["id"])["agents"]
        assert len(agents) == 1
        assert agents[0]["name"] == "OpenCode"
        # The message landed in the room.
        events = app.state.service.list_events(project["id"], after=0)["events"]
        messages = [e for e in events if e["event_type"] == "message.message"]
        assert any(
            (e.get("payload") or {}).get("body") == "submit fallback works without MCP"
            for e in messages
        )
        # The work report landed and pushed the task into awaiting_review.
        stored = next(
            item
            for item in app.state.service.snapshot(project["id"])["tasks"]
            if item["id"] == task["id"]
        )
        assert stored["state_view"]["phase"] == "awaiting_review"
    finally:
        await _stop(server, server_task)


@pytest.mark.asyncio
async def test_submit_rejects_a_revoked_credential_with_required_action(
    settings, project_dir
):
    """AC1/AC4：凭据被吊销时给出机器可读错误与补发凭据的 required_action。"""
    app = create_app(settings)
    project, _member, credential = _prepared_project(app, project_dir)
    app.state.service.revoke_agent_token(project["id"], credential["id"])

    server, server_task, url = await _serve(app)
    try:
        with pytest.raises(SubmitError) as excinfo:
            await asyncio.to_thread(
                run_submit,
                _args(token=credential["token"], message="should not land"),
                url,
            )
        assert excinfo.value.code == "submit_credential_invalid"
        assert (
            excinfo.value.required_action == "issue_new_project_token_and_update_bundle"
        )
        # Nothing was written to the room.
        events = app.state.service.list_events(project["id"], after=0)["events"]
        assert not [
            e for e in events if e["event_type"] == "message.message"
        ]
    finally:
        await _stop(server, server_task)


@pytest.mark.asyncio
async def test_submit_reports_unknown_project_with_known_names(settings, project_dir):
    app = create_app(settings)
    server, server_task, url = await _serve(app)
    try:
        with pytest.raises(SubmitError) as excinfo:
            await asyncio.to_thread(
                run_submit, _args(project="No Such Project", message="x"), url
            )
        assert excinfo.value.code == "submit_project_not_found"
        assert "Submit Project" not in str(excinfo.value.message)
    finally:
        await _stop(server, server_task)


def test_submit_bundle_decoder_round_trips_projects(tmp_path):
    import base64
    import json

    entries = [
        {"name": "agentchatroom", "token": "acr.credential_abc.secret"},
        {"name": "other", "token": "acr.credential_def.secret"},
    ]
    encoded = "acrb.v1." + base64.urlsafe_b64encode(
        json.dumps({"projects": entries}).encode("utf-8")
    ).decode().rstrip("=")
    literal = _decode_submit_bundle(encoded)
    assert literal == entries
    bundle_file = tmp_path / "bundle.txt"
    bundle_file.write_text(encoded, encoding="utf-8")
    assert _decode_submit_bundle(str(bundle_file)) == entries
    with pytest.raises(SubmitError) as excinfo:
        _decode_submit_bundle("not-a-bundle")
    assert excinfo.value.code == "submit_bundle_invalid"
