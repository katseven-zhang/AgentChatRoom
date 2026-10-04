"""#372: integration commit verification must read the report event that exists.

``submit_work_report`` emits ``work.reported``; the integration path used to
query ``event_type = 'work.report'``, so ``reported_commit`` stayed empty and
every integration commit was downgraded to ``commit_self_reported`` — the
server-side git proof collected at report time never transferred.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _prepare_reported_task(service, project_id: str, worktree: Path) -> tuple[str, dict, str]:
    """Report real work: a real git commit the server can verify in-worktree."""
    subprocess.run(
        ["git", "-C", str(worktree), "init", "-q"], check=True, capture_output=True
    )
    (worktree / "change.txt").write_text("reported work", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "change.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git", "-C", str(worktree),
            "-c", "user.name=pytest",
            "-c", "user.email=pytest@example.invalid",
            "commit", "-q", "-m", "reported work",
        ],
        check=True,
        capture_output=True,
    )
    commit_hash = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return _report_and_approve(service, project_id, worktree, commit_hash), commit_hash


def _report_and_approve(service, project_id: str, worktree, commit_hash: str) -> str:
    registered = service.register_workspace(
        project_id,
        host_key="verification-host",
        host_name="Verification Host",
        local_path=str(worktree),
    )
    owner = service.join_room(
        project_id,
        software_key="worker-a",
        name="Worker A",
        client="pytest",
        model="unknown",
        worktree=str(worktree),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = service.create_task(
        project_id,
        title="Commit verification",
        acceptance_criteria=["it works"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    service.claim_task(
        project_id, task["id"], owner["agent"]["id"], owner["token"]
    )
    service.submit_work_report(
        project_id,
        task["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
        summary="done",
        files=["src/example.py"],
        tests=[{"command": "pytest -q", "exit_code": 0}],
        commit_hash=commit_hash,
    )
    reviewer = service.join_room(
        project_id,
        software_key="reviewer-b",
        name="Reviewer B",
        client="pytest",
        model="unknown",
    )
    service.submit_review(
        project_id,
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "it works", "status": "passed"}],
    )
    integrator = service.join_room(
        project_id,
        software_key="integrator-c",
        name="Integrator C",
        client="pytest",
        model="unknown",
    )
    return task["id"], integrator


def test_integration_commit_matching_the_report_is_verified(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    prepared, reported_hash = _prepare_reported_task(
        service, project["id"], project_dir
    )
    task_id, integrator = prepared

    result = service.submit_integration(
        project["id"],
        task_id,
        result="done",
        summary="merged",
        tests=[{"command": "pytest -q", "exit_code": 0}],
        commit_hash=reported_hash,
        integrator_session_id=integrator["agent"]["id"],
        token=integrator["token"],
    )

    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM events WHERE id = ?",
            (result["event_id"],),
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["commit_verified"] is True
    assert payload["commit_self_reported"] is False


def test_integration_commit_still_self_reported_when_it_differs(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir))
    prepared, reported_hash = _prepare_reported_task(
        service, project["id"], project_dir
    )
    task_id, integrator = prepared

    result = service.submit_integration(
        project["id"],
        task_id,
        result="done",
        summary="merged at a different commit",
        tests=[{"command": "pytest -q", "exit_code": 0}],
        commit_hash="f" * 40,  # differs from the reported commit
        integrator_session_id=integrator["agent"]["id"],
        token=integrator["token"],
    )

    # Honest downgrade: the mismatch is recorded as self-reported, never as
    # captured evidence.
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM events WHERE id = ?",
            (result["event_id"],),
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["commit_verified"] is False
    assert payload["commit_self_reported"] is True
