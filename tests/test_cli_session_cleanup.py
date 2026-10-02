"""Task #263: a failed `submit` must not leak the Session it just opened.

`run_submit` joined first and only issued the leave on the success path, so every
HTTP failure between them left a live Session holding Presence and, after a
reclaim, a task. The `logs` command also read the whole `server.log` into memory
to print its last lines.
"""

from __future__ import annotations

import asyncio

import pytest

from agentchatroom import cli
from agentchatroom.api import create_app
from agentchatroom.cli import SubmitError, _leave_session, print_server_log, run_submit
from test_cli_submit import _args, _prepared_project, _serve, _stop


@pytest.mark.asyncio
async def test_a_failing_report_still_leaves_the_session(settings, project_dir):
    app = create_app(settings)
    project, _member, credential = _prepared_project(app, project_dir)

    server, server_task, url = await _serve(app)
    try:
        with pytest.raises(SubmitError):
            await asyncio.to_thread(
                run_submit,
                _args(
                    token=credential["token"],
                    message="this report will fail after the join",
                    report_task="9999",
                    summary="Implemented something",
                    file=["src/example.py"],
                    test=["pytest tests/test_cli_session_cleanup.py::0"],
                    cwd=str(project_dir),
                ),
                url,
            )

        events = app.state.service.list_events(project["id"], after=0)["events"]
        kinds = [event["event_type"] for event in events]
        assert "agent.joined" in kinds, "the fixture must fail after the join"
        assert "agent.left" in kinds
        assert all(agent["left_at"] for agent in app.state.service.snapshot(project["id"])["agents"])
    finally:
        await _stop(server, server_task)


@pytest.mark.asyncio
async def test_keep_session_still_keeps_the_session_open(settings, project_dir):
    app = create_app(settings)
    project, _member, credential = _prepared_project(app, project_dir)

    server, server_task, url = await _serve(app)
    try:
        with pytest.raises(SubmitError):
            await asyncio.to_thread(
                run_submit,
                _args(
                    token=credential["token"],
                    message="keep this session",
                    report_task="9999",
                    summary="Implemented something",
                    file=["src/example.py"],
                    test=["pytest tests/test_cli_session_cleanup.py::0"],
                    cwd=str(project_dir),
                    keep_session=True,
                ),
                url,
            )

        kinds = [
            event["event_type"]
            for event in app.state.service.list_events(project["id"], after=0)["events"]
        ]
        agents = app.state.service.snapshot(project["id"])["agents"]

        assert "agent.joined" in kinds
        assert "agent.left" not in kinds
        assert [agent["name"] for agent in agents] == ["OpenCode"]
        assert agents[0]["left_at"] is None
    finally:
        await _stop(server, server_task)


def test_leave_session_reports_cleanup_failures_without_masking_them(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def failing(base_url, method, path, payload, **kwargs):
        calls.append((path, kwargs.get("quiet", False)))
        raise SystemExit("service unreachable")

    monkeypatch.setattr(cli, "request_json", failing)

    assert _leave_session("http://x", "p1", "s1", "t", quiet=True) is False
    with pytest.raises(SystemExit):
        _leave_session("http://x", "p1", "s1", "t")

    assert len(calls) == 2


def test_logs_command_uses_the_bounded_tail(tmp_path, capsys, monkeypatch):
    log = tmp_path / "server.log"
    log.write_text(
        "".join(f"line {index}\n" for index in range(5000)), encoding="utf-8"
    )
    settings = type("S", (), {"data_dir": tmp_path})()
    seen: list[tuple[int, int]] = []
    real = cli.tail_text_lines

    def spy(path, limit, **kwargs):
        seen.append((limit, len(real(path, 100000)[0])))
        return real(path, limit, **kwargs)

    monkeypatch.setattr(cli, "tail_text_lines", spy)

    print_server_log(settings, lines=3)

    captured = capsys.readouterr().out.splitlines()
    assert captured == ["line 4997", "line 4998", "line 4999"]
    assert seen and seen[0][0] == 3


def test_logs_command_still_fails_when_the_log_is_absent(tmp_path):
    settings = type("S", (), {"data_dir": tmp_path})()

    with pytest.raises(SystemExit):
        print_server_log(settings, lines=5)


def test_logs_marks_a_byte_budget_truncation(tmp_path, capsys, monkeypatch):
    log = tmp_path / "server.log"
    log.write_text("line 1" + chr(10), encoding="utf-8")
    settings = type("S", (), {"data_dir": tmp_path})()
    monkeypatch.setattr(cli, "tail_text_lines", lambda path, limit, **kwargs: (["line 1"], True))

    print_server_log(settings, lines=80)

    printed = capsys.readouterr().out
    assert "line 1" in printed
    assert "not scanned" in printed
