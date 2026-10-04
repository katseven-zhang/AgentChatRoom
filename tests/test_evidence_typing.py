"""Evidence must never record a boolean as a passing exit code (#351)."""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import is_strict_int, require_int_field


def test_is_strict_int_rejects_booleans():
    assert is_strict_int(0) is True
    assert is_strict_int(-1) is True
    assert is_strict_int(False) is False
    assert is_strict_int(True) is False
    assert is_strict_int("0") is False
    assert is_strict_int(None) is False


def test_require_int_field_rejects_booleans():
    assert require_int_field(None, "priority") is None
    assert require_int_field(2, "priority") == 2
    for bad in (True, False):
        with pytest.raises(DomainError) as refused:
            require_int_field(bad, "priority")
        assert refused.value.code == "invalid_priority"
        assert refused.value.status_code == 422


def _claimed_task(service, project, *, worktree=None):
    # A work report collects git evidence, so the Session needs a registered
    # workspace even when the evidence under test is rejected.
    worktree = worktree or project["root_path"]
    registered = service.register_workspace(
        project["id"],
        host_key="evidence-host",
        host_name="Evidence Host",
        local_path=worktree,
    )
    session = service.join_room(
        project["id"],
        name="Worker",
        client="codex",
        model="unknown",
        worktree=worktree,
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Evidence guard",
        acceptance_criteria=["Booleans are refused"],
        actor_session_id=session["agent"]["id"],
        token=session["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], session["agent"]["id"], session["token"]
    )
    return session, task


def test_work_report_refuses_a_boolean_exit_code(service, project):
    """A JSON `false` would otherwise be stored as exit code 0: a silent pass."""
    session, task = _claimed_task(service, project)

    for bad in (False, True):
        with pytest.raises(DomainError) as refused:
            service.submit_work_report(
                project["id"],
                task["id"],
                session_id=session["agent"]["id"],
                token=session["token"],
                summary="boolean exit code",
                files=["README.md"],
                tests=[{"command": "pytest -q", "exit_code": bad}],
            )
        assert refused.value.code == "invalid_test_evidence"
        assert refused.value.status_code == 422


def test_work_report_still_accepts_a_real_integer_exit_code(service, project):
    session, task = _claimed_task(service, project)

    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=session["agent"]["id"],
        token=session["token"],
        summary="real exit code",
        files=["README.md"],
        tests=[{"command": "pytest -q", "exit_code": 1, "notes": "one failure"}],
    )

    # Assert on what landed in the immutable record, not on the return shape.
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT tests_json FROM work_reports WHERE task_id = ?", (task["id"],)
        ).fetchone()
    assert row is not None
    import json as _json

    stored = _json.loads(row["tests_json"])
    assert stored[0]["exit_code"] == 1
    assert isinstance(stored[0]["exit_code"], bool) is False


def test_task_priority_refuses_a_boolean(service, project):
    session = service.join_room(
        project["id"], name="Worker", client="codex", model="unknown"
    )

    with pytest.raises(DomainError) as refused:
        service.create_task(
            project["id"],
            title="Boolean priority",
            acceptance_criteria=["nope"],
            priority=True,
            actor_session_id=session["agent"]["id"],
            token=session["token"],
        )
    assert refused.value.code == "invalid_priority"


def test_task_progress_refuses_a_boolean(service, project):
    session, task = _claimed_task(service, project)

    with pytest.raises(DomainError) as refused:
        service.update_task(
            project["id"],
            task["id"],
            progress_percent=False,
            session_id=session["agent"]["id"],
            token=session["token"],
        )
    assert refused.value.code == "invalid_progress_percent"
    assert refused.value.status_code == 422


def test_rest_work_report_rejects_a_boolean_exit_code(settings, project_dir):
    """The REST body is an untyped dict, so the domain guard is the only one."""
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Evidence"},
        ).json()
        joined = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={"name": "Worker", "client": "codex", "model": "unknown"},
        ).json()
        session = joined["agent"]
        created = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "Evidence guard",
                "acceptance_criteria": ["Booleans are refused"],
                "actor_session_id": session["id"],
                "token": joined["token"],
            },
        ).json()["task"]
        client.post(
            f"/api/v1/projects/{project['id']}/tasks/{created['id']}/claim",
            json={"session_id": session["id"], "token": joined["token"]},
        )
        response = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{created['id']}/reports",
            json={
                "session_id": session["id"],
                "token": joined["token"],
                "summary": "boolean exit code",
                "files": ["README.md"],
                "tests": [{"command": "pytest -q", "exit_code": False}],
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_test_evidence"


def test_web_asset_has_no_parsing_helper_that_treats_blank_exit_code_as_zero():
    """`Number("")` is 0 and `Number.isInteger(0)` is true — a blank passed."""
    from pathlib import Path

    app_js = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agentchatroom"
        / "web"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert "parseTestLines" not in app_js


# --- #362: evidence collection must not depend on the process locale -------


def _git(*arguments, cwd):
    import subprocess

    return subprocess.run(
        ["git", *arguments],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def service_database(settings):
    from agentchatroom.database import Database

    return Database(settings.database_path).connect()


def test_git_evidence_survives_a_non_ascii_branch_and_path(tmp_path):
    """A C/ANSI locale must not turn a non-ASCII branch into mojibake evidence."""
    from agentchatroom.services import run_git_capture

    repo = tmp_path / "仓库-ünïcode"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "文件.txt").write_text("内容\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "初始提交", cwd=repo)
    branch = "功能/分支-ü"
    _git("checkout", "-b", branch, cwd=repo)

    returncode, branch_name, _stderr = run_git_capture(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"]
    )

    assert returncode == 0
    assert branch_name == branch, f"branch came back as {branch_name!r}"
    assert "�" not in branch_name, "replacement characters leaked into the output"


def test_work_report_completes_for_a_non_ascii_worktree(settings, tmp_path):
    """The report must finish, not 500, when git output is non-ASCII."""
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    repo = tmp_path / "项目-α"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "文件.md").write_text("x\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "初始", cwd=repo)
    _git("checkout", "-b", "分支-ß", cwd=repo)

    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(repo), "name": "Unicode"}
        ).json()
        pid = project["id"]
        workspace = client.post(
            f"/api/v1/projects/{pid}/workspaces",
            json={
                "host_key": "unicode-host",
                "host_name": "Unicode Host",
                "local_path": str(repo),
            },
        ).json()
        joined = client.post(
            f"/api/v1/projects/{pid}/agents/join",
            json={
                "name": "Worker",
                "client": "codex",
                "model": "unknown",
                "worktree": str(repo),
                "host_id": workspace["host"]["id"],
                "workspace_id": workspace["workspace"]["id"],
            },
        ).json()
        sid, tok = joined["agent"]["id"], joined["token"]
        task = client.post(
            f"/api/v1/projects/{pid}/tasks",
            json={
                "title": "Unicode evidence",
                "acceptance_criteria": ["No 500"],
                "actor_session_id": sid,
                "token": tok,
            },
        ).json()["task"]
        client.post(
            f"/api/v1/projects/{pid}/tasks/{task['id']}/claim",
            json={"session_id": sid, "token": tok},
        )
        response = client.post(
            f"/api/v1/projects/{pid}/tasks/{task['id']}/reports",
            json={
                "session_id": sid,
                "token": tok,
                "summary": "unicode worktree",
                "files": ["文件.md"],
                "tests": [{"command": "pytest -q", "exit_code": 0}],
            },
        )

    assert response.status_code == 201, response.text
    # Assert on the immutable record rather than the response envelope.
    with service_database(settings) as connection:
        row = connection.execute(
            "SELECT system_evidence_json FROM work_reports WHERE task_id = ?",
            (task["id"],),
        ).fetchone()
    assert row is not None
    import json as _json

    evidence = _json.loads(row["system_evidence_json"])
    assert evidence["captured"] is True
    assert "�" not in row["system_evidence_json"]
    assert evidence["branch"] == "分支-ß"

