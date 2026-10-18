"""#376: work reports for nested-repository commits and non-Git deliveries.

The upstream report (WorkBuddy, ``传奇源码研究``) showed one commit requirement
both refusing a real commit that lived in a sibling repository and refusing a
pure local delivery, leaving no legal way out of ``in_progress``. These tests
lock the two explicit channels and the boundary that keeps them from becoming a
way around commit evidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.database import SCHEMA, SCHEMA_VERSION, Database
from agentchatroom.errors import DomainError
from tests.conftest import join_room_with_workspace


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True
    )
    return result.stdout.decode("utf-8").strip()


def repository(root: Path, *, remote: bool = False) -> str:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    (root / "code.txt").write_text(root.name + "\n", encoding="utf-8")
    git(root, "add", "code.txt")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    if remote:
        git(
            root,
            "remote",
            "add",
            "origin",
            "https://example.invalid/fixture/" + root.name + ".git",
        )
    return git(root, "rev-parse", "HEAD")


def claim(service, project, **join_kwargs):
    joined = join_room_with_workspace(
        service,
        project,
        agent_key="builder-main",
        name="Builder",
        client="test-client",
        model="unknown",
        **join_kwargs,
    )
    task = service.create_task(
        project["id"],
        title="Delivery evidence",
        acceptance_criteria=["Evidence matches the delivery"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )
    return task, joined


def report(service, project, task, joined, **kwargs):
    return service.submit_work_report(
        project["id"],
        task["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        summary="Delivered",
        tests=[{"command": "fixture validation", "exit_code": 0}],
        **kwargs,
    )


def test_local_delivery_records_file_hashes_and_enters_review(service, project_dir):
    repository(project_dir / "selected", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    (project_dir / "deploy").mkdir()
    deployed = project_dir / "deploy" / "run.txt"
    deployed.write_text("deployed\n", encoding="utf-8")
    (project_dir / "docs").mkdir()
    (project_dir / "docs" / "note.md").write_text("# note\n", encoding="utf-8")
    task, joined = claim(service, project)

    result = report(
        service,
        project,
        task,
        joined,
        files=["deploy/run.txt", "docs/note.md"],
        delivery_mode="local",
    )

    assert result["delivery_mode"] == "local"
    assert result["task_status"] == "awaiting_review"
    assert service.get_task(project["id"], task["id"])["phase"] == "awaiting_review"
    stored = service.snapshot(project["id"])["reports"][0]
    assert stored["delivery_mode"] == "local"
    assert stored["commit_hash"] == ""
    evidence = stored["system_evidence"]
    assert evidence["captured"] is True
    assert evidence["delivery_verified"] == "local_only"
    assert evidence["delivery_mode"] == "local"
    by_path = {item["path"]: item for item in evidence["file_evidence"]}
    assert by_path["deploy/run.txt"]["sha256"] == hashlib.sha256(
        deployed.read_bytes()
    ).hexdigest()
    assert by_path["docs/note.md"]["status"] == "captured"
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM events "
            "WHERE project_id = ? AND event_type = 'work.reported'",
            (project["id"],),
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["delivery_mode"] == "local"
    assert payload["system_evidence"]["delivery_verified"] == "local_only"


def test_local_delivery_refuses_commit_evidence_and_selected_repository_files(
    service, project_dir
):
    repository(project_dir / "selected", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as with_commit:
        report(
            service,
            project,
            task,
            joined,
            files=[],
            no_code_change_reason="Nothing to commit",
            delivery_mode="local",
            commit_hash="a" * 40,
        )
    assert with_commit.value.code == "delivery_mode_conflict"

    with pytest.raises(DomainError) as with_commit_repo:
        report(
            service,
            project,
            task,
            joined,
            files=["deploy/run.txt"],
            delivery_mode="local",
            commit_repo="selected",
        )
    assert with_commit_repo.value.code == "delivery_mode_conflict"

    with pytest.raises(DomainError) as selected_file:
        report(
            service,
            project,
            task,
            joined,
            files=["selected/code.txt"],
            delivery_mode="local",
        )
    assert selected_file.value.code == "delivery_mode_conflict"
    assert selected_file.value.details["files"] == ["selected/code.txt"]
    assert selected_file.value.details["required_action"] == (
        "submit_git_delivery_with_commit_or_move_files_outside_the_git_worktrees"
    )
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_local_delivery_allows_a_session_rooted_outside_the_selected_repository(
    service, project_dir
):
    repository(project_dir / "selected", remote=True)
    other = project_dir / "other"
    repository(other)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    (project_dir / "deploy").mkdir()
    (project_dir / "deploy" / "run.txt").write_text("local\n", encoding="utf-8")
    task, joined = claim(service, project, worktree=str(other))

    result = report(
        service,
        project,
        task,
        joined,
        files=["deploy/run.txt"],
        delivery_mode="local",
    )

    assert result["delivery_mode"] == "local"
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["file_evidence"][0]["status"] == "captured"


def test_local_delivery_marks_files_outside_the_registered_workspace(
    service, project_dir
):
    (project_dir / "work").mkdir()
    (project_dir / "work" / "out.txt").write_text("inside\n", encoding="utf-8")
    (project_dir / "deploy").mkdir()
    (project_dir / "deploy" / "run.txt").write_text("outside\n", encoding="utf-8")
    project = service.create_project(root_path=str(project_dir))
    registered = service.register_workspace(
        project["id"],
        host_key="narrow-host",
        host_name="Narrow",
        local_path=str(project_dir / "work"),
    )
    joined = service.join_room(
        project["id"],
        agent_key="narrow-builder",
        name="Narrow builder",
        client="fixture",
        model="unknown",
        worktree=str(project_dir / "work"),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Narrow local delivery",
        acceptance_criteria=["Only workspace files are hashed"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )

    report(
        service,
        project,
        task,
        joined,
        files=["work/out.txt", "deploy/run.txt"],
        delivery_mode="local",
    )

    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    by_path = {item["path"]: item for item in evidence["file_evidence"]}
    assert by_path["work/out.txt"]["status"] == "captured"
    assert by_path["deploy/run.txt"]["status"] == "outside_workspace"


def test_commit_repo_verifies_commit_in_an_explicit_sibling_repository(
    service, project_dir
):
    repository(project_dir / "selected", remote=True)
    other = project_dir / "other"
    other_commit = repository(other, remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    (project_dir / "deploy").mkdir()
    (project_dir / "deploy" / "run.txt").write_text("local\n", encoding="utf-8")
    task, joined = claim(service, project)

    result = report(
        service,
        project,
        task,
        joined,
        files=["other/code.txt", "deploy/run.txt"],
        commit_hash=other_commit,
        commit_repo="other",
    )

    assert result["commit_repo"] == "other"
    assert result["delivery_mode"] == "git"
    assert service.get_task(project["id"], task["id"])["phase"] == "awaiting_review"
    stored = service.snapshot(project["id"])["reports"][0]
    evidence = stored["system_evidence"]
    assert stored["commit_hash"] == other_commit
    assert evidence["commit_repo"] == "other"
    assert evidence["repository_path"] == "other"
    assert evidence["reported_commit_exists"] is True
    assert evidence["worktree"] == str(other.resolve())
    assert evidence["commit_files"] == ["code.txt"]
    by_path = {item["path"]: item for item in evidence["file_evidence"]}
    assert by_path["other/code.txt"]["status"] == "captured"
    assert by_path["other/code.txt"]["repository_relative_path"] == "code.txt"
    assert by_path["other/code.txt"]["sha256"] == hashlib.sha256(
        (other / "code.txt").read_bytes()
    ).hexdigest()
    assert by_path["deploy/run.txt"]["status"] == "outside_commit_repository"


def test_commit_repo_allows_a_session_rooted_in_that_repository(
    service, project_dir
):
    repository(project_dir / "selected", remote=True)
    other = project_dir / "other"
    other_commit = repository(other, remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project, worktree=str(other))

    report(
        service,
        project,
        task,
        joined,
        files=["other/code.txt"],
        commit_hash=other_commit,
        commit_repo="other",
    )

    assert service.get_task(project["id"], task["id"])["phase"] == "awaiting_review"
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["commit_repo"] == "other"
    assert evidence["worktree"] == str(other.resolve())


def test_commit_repo_refuses_a_session_rooted_in_a_third_repository(
    service, project_dir
):
    repository(project_dir / "selected", remote=True)
    other_commit = repository(project_dir / "other")
    repository(project_dir / "third")
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project, worktree=str(project_dir / "third"))

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["other/code.txt"],
            commit_hash=other_commit,
            commit_repo="other",
        )

    assert error.value.code == "untrusted_repository"
    assert error.value.details["required_action"] == (
        "use_a_worktree_of_commit_repo_or_its_workspace_root"
    )
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_commit_absent_from_the_workspace_is_refused_with_diagnostics(
    service, project_dir, tmp_path
):
    """#377: a report whose commit is nowhere in the workspace still refuses.

    The root Session can no longer borrow a sibling repository's commit only
    because it is the first one found; an absent commit leaves the primary as
    the searched worktree and reports what the caller could name instead.
    """
    repository(project_dir / "selected", remote=True)
    repository(project_dir / "other")
    outside_commit = repository(tmp_path / "outside")
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["code.txt"],
            commit_hash=outside_commit,
        )

    assert error.value.code == "invalid_commit_hash"
    details = error.value.details
    assert details["searched_worktree"] == str(
        (project_dir / "selected").resolve()
    )
    assert details["required_action"] == (
        "retry_with_commit_repo_from_repositories_available_or_declare_local_delivery"
    )
    available = {item["relative_path"] for item in details["repositories_available"]}
    assert {"selected", "other"} <= available
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"
    assert not service.snapshot(project["id"])["reports"]


@pytest.mark.parametrize(
    ("commit_repo", "code"),
    [
        ("..", "invalid_commit_repo"),
        ("/absolute/path", "invalid_commit_repo"),
        ("C:/outside", "invalid_commit_repo"),
        ("selected/sub", "invalid_commit_repo"),
        ("missing", "repository_not_found"),
    ],
)
def test_commit_repo_refuses_unsafe_or_unknown_paths(
    service, project_dir, commit_repo, code
):
    selected_commit = repository(project_dir / "selected", remote=True)
    (project_dir / "selected" / "sub").mkdir()
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["selected/code.txt"],
            commit_hash=selected_commit,
            commit_repo=commit_repo,
        )

    assert error.value.code == code
    assert "required_action" in error.value.details


def test_commit_repo_must_stay_inside_the_registered_workspace(
    service, project_dir
):
    repository(project_dir / "selected", remote=True)
    other_commit = repository(project_dir / "other")
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    registered = service.register_workspace(
        project["id"],
        host_key="direct-host",
        host_name="Direct",
        local_path=str(project_dir / "selected"),
    )
    joined = service.join_room(
        project["id"],
        agent_key="direct-builder",
        name="Direct builder",
        client="fixture",
        model="unknown",
        worktree=str(project_dir / "selected"),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Direct workspace delivery",
        acceptance_criteria=["Evidence stays inside the workspace"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["other/code.txt"],
            commit_hash=other_commit,
            commit_repo="other",
        )

    assert error.value.code == "invalid_commit_repo"
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_commit_repo_from_a_client_asserted_workspace_is_not_run(service, project_dir):
    repository(project_dir / "selected", remote=True)
    other_commit = repository(project_dir / "other")
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    registered = service.register_workspace(
        project["id"],
        host_key="asserted-host",
        host_name="Asserted",
        local_path=str(project_dir),
        server_verified=False,
    )
    joined = service.join_room(
        project["id"],
        agent_key="asserted-builder",
        name="Asserted builder",
        client="fixture",
        model="unknown",
        worktree=str(project_dir),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        title="Asserted workspace delivery",
        acceptance_criteria=["Server reads no git here"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["other/code.txt"],
            commit_hash=other_commit,
            commit_repo="other",
        )

    assert error.value.code == "invalid_commit_hash"
    assert error.value.details["reason"] == "workspace_evidence_untrusted"
    assert error.value.details["commit_repo"] == "other"
    assert "searched_worktree" not in error.value.details
    assert not service.snapshot(project["id"])["reports"]


def test_unknown_delivery_mode_is_refused(service, project_dir):
    repository(project_dir / "selected", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["selected/code.txt"],
            delivery_mode="artifact",
        )

    assert error.value.code == "invalid_delivery_mode"
    assert error.value.details["allowed"] == ["git", "local"]


def test_migration_adds_delivery_mode_with_git_default(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    old_schema = "\n".join(
        line for line in SCHEMA.splitlines() if "delivery_mode" not in line
    )
    connection.executescript(old_schema)
    connection.execute(
        "INSERT INTO work_reports("
        "id, project_id, task_id, session_id, summary, files_json, tests_json, "
        "created_at) VALUES ('report_legacy', 'project_1', 'task_1', 'session_1', "
        "'legacy', '[]', '[]', '2026-01-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION - 1,)
    )
    connection.commit()
    connection.close()

    Database(path).initialize()

    connection = sqlite3.connect(path)
    try:
        columns = {
            row[1]: row[4]
            for row in connection.execute("PRAGMA table_info(work_reports)")
        }
        assert columns["delivery_mode"] == "'git'"
        assert connection.execute(
            "SELECT delivery_mode FROM work_reports WHERE id = 'report_legacy'"
        ).fetchone()[0] == "git"
        assert connection.execute("SELECT version FROM schema_meta").fetchone()[0] == (
            SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_rest_accepts_local_delivery(settings, project_dir):
    (project_dir / "deploy").mkdir()
    (project_dir / "deploy" / "run.txt").write_text("deployed\n", encoding="utf-8")
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects", json={"root_path": str(project_dir)}
        ).json()
        registered = client.post(
            f"/api/v1/projects/{project['id']}/workspaces",
            json={
                "host_key": "test-host",
                "host_name": "Test Host",
                "local_path": project["root_path"],
            },
        ).json()
        agent = client.post(
            f"/api/v1/projects/{project['id']}/agents/join",
            json={
                "agent_key": "builder-main",
                "name": "Builder",
                "client": "test-client",
                "model": "unknown",
                "worktree": project["root_path"],
                "host_id": registered["host"]["id"],
                "workspace_id": registered["workspace"]["id"],
            },
        ).json()
        task = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "Local delivery",
                "actor_session_id": agent["agent"]["id"],
                "token": agent["token"],
                "acceptance_criteria": ["Delivery is recorded"],
            },
        ).json()["task"]
        client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/claim",
            json={"session_id": agent["agent"]["id"], "token": agent["token"]},
        )

        response = client.post(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}/reports",
            json={
                "session_id": agent["agent"]["id"],
                "token": agent["token"],
                "summary": "Local delivery complete",
                "files": ["deploy/run.txt"],
                "tests": [{"command": "fixture validation", "exit_code": 0}],
                "delivery_mode": "local",
            },
        )

        assert response.status_code == 201
        assert response.json()["delivery_mode"] == "local"
        snapshot = client.get(
            f"/api/v1/projects/{project['id']}/snapshot"
        ).json()
        assert snapshot["reports"][0]["delivery_mode"] == "local"
        assert snapshot["reports"][0]["system_evidence"]["file_evidence"][0][
            "status"
        ] == "captured"


def test_commit_repo_missing_repository_returns_actionable_diagnostics(
    service, project_dir
):
    selected_commit = repository(project_dir / "selected", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="selected"
    )
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["selected/code.txt"],
            commit_hash=selected_commit,
            commit_repo="missing",
        )

    assert error.value.code == "repository_not_found"
    details = error.value.details
    assert details["reported_commit_hash"] == selected_commit
    assert details["reason"] == "not_a_git_worktree"
    assert details["searched_worktree"].endswith("missing")
    assert any(
        item["relative_path"] == "selected"
        for item in details["repositories_available"]
    )
    assert details["required_action"] == (
        "use_a_git_repository_from_repositories_available"
    )
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"
