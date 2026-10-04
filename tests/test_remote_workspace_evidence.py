"""Task #276: a remote join must not turn a client-typed path into a git source.

``join_remote_room`` takes ``workspace_path`` from the request body and
registers it as the Session's workspace root. The local MCP branch resolves the
Project from the checkout and validates the scope before registering, so the
path it stores is one the server confirmed. The remote branch had no equivalent
check, and both ``local_path`` and the reported worktree come from the caller -
so the containment test in ``_trusted_worktree`` only proved the caller stayed
inside the directory it chose.

``submit_work_report`` then ran ``git -C <path> status/rev-parse/show`` on the
server and published the result to the Room. Any holder of a valid Agent Token
could therefore aim the server's git at a repository it was never scoped to and
read back that repository's branch, HEAD and dirty-file list.

A workspace now records how its path was established, and server-side git is
only read from a root the server verified.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import (
    WORKSPACE_EVIDENCE_CLIENT_ASSERTED,
    WORKSPACE_EVIDENCE_SERVER_VERIFIED,
    WORKSPACE_EVIDENCE_SOURCE_KEY,
)


def _git(path, *arguments):
    subprocess.run(["git", *arguments], cwd=path, capture_output=True, text=True)


def _make_repo(path, *, branch="main", dirty=False):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "agent@example.invalid")
    _git(path, "config", "user.name", "Agent")
    (path / "tracked.txt").write_text("committed", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "initial")
    if dirty:
        (path / "tracked.txt").write_text("modified", encoding="utf-8")
    return path


def _remote_session(service, project, workspace_path, *, key="remote-main"):
    token = service.issue_agent_token(project["id"], name=key)["token"]
    return service.join_remote_room(
        project["id"],
        agent_token=token,
        host_key=f"host:{key}",
        host_name="Remote PC",
        workspace_path=str(workspace_path),
        software_key=key,
        name=key,
        client="codex",
        model="unknown",
    )


def _claimed_task(service, project_id, session):
    task = service.create_task(
        project_id,
        title="Ship it",
        acceptance_criteria=["the change lands"],
        actor_session_id=session["agent"]["id"],
        token=session["token"],
    )["task"]
    service.claim_task(project_id, task["id"], session["agent"]["id"], session["token"])
    return task


def _stored_evidence(service, project_id, report_id):
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT system_evidence_json FROM work_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    return json.loads(row["system_evidence_json"])


# --- the reported defect ----------------------------------------------------


def test_remote_join_cannot_publish_git_evidence_from_another_server_repo(
    service, tmp_path
):
    """The reported leak: a foreign server repo's git state reached the Room.

    ``other-repo`` exists on the server and the client mentions nothing about
    it. Before the fix its branch, HEAD and dirty-file list were published.
    """
    project_repo = _make_repo(tmp_path / "project-repo")
    other_repo = _make_repo(tmp_path / "other-repo", branch="private-branch", dirty=True)
    project = service.create_project(root_path=str(project_repo), name="Room")
    project_id = project["id"]

    session = _remote_session(service, project, other_repo)
    task = _claimed_task(service, project_id, session)
    report = service.submit_work_report(
        project_id,
        task["id"],
        session_id=session["agent"]["id"],
        token=session["token"],
        summary="did the work",
        files=["src/thing.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )

    evidence = _stored_evidence(service, project_id, report["report_id"])
    serialized = json.dumps(evidence)
    assert evidence["captured"] is False
    assert evidence["reason"] == "workspace_evidence_untrusted"
    assert evidence["evidence_source"] == WORKSPACE_EVIDENCE_CLIENT_ASSERTED
    # Nothing about that repository may appear: not its branch, not its HEAD,
    # not its dirty files, and not the path that was used to reach it.
    assert "private-branch" not in serialized
    assert "dirty_files" not in evidence
    assert evidence["worktree"] == ""
    assert str(other_repo) not in serialized


def test_a_client_asserted_workspace_is_marked_as_such_on_registration(
    service, tmp_path
):
    project_repo = _make_repo(tmp_path / "project-repo")
    project = service.create_project(root_path=str(project_repo), name="Room")
    session = _remote_session(service, project, project_repo)

    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT metadata_json FROM workspaces WHERE id = ?",
            (session["workspace"]["id"],),
        ).fetchone()
    metadata = json.loads(row["metadata_json"])
    assert metadata[WORKSPACE_EVIDENCE_SOURCE_KEY] == WORKSPACE_EVIDENCE_CLIENT_ASSERTED


def test_a_reported_commit_is_not_verified_against_an_untrusted_root(
    service, tmp_path
):
    """Reporting a commit still works; it just cannot be verified server-side.

    The commit exists in the repository the client meant, but the server has no
    verified root for it, so it must answer unverified rather than read git to
    find out.
    """
    project_repo = _make_repo(tmp_path / "project-repo")
    other_repo = _make_repo(tmp_path / "other-repo")
    project = service.create_project(root_path=str(project_repo), name="Room")
    project_id = project["id"]

    session = _remote_session(service, project, other_repo)
    task = _claimed_task(service, project_id, session)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=other_repo, capture_output=True, text=True
    ).stdout.strip()

    with pytest.raises(DomainError) as refused:
        service.submit_work_report(
            project_id,
            task["id"],
            session_id=session["agent"]["id"],
            token=session["token"],
            summary="did the work",
            files=["src/thing.py"],
            commit_hash=head,
            tests=[{"command": "pytest", "exit_code": 0}],
        )
    assert refused.value.code == "invalid_commit_hash"

    # The unverified-commit event must not carry the client-typed path either.
    events = service.list_events(project_id, after=0)["events"]
    unverified = [e for e in events if e["event_type"] == "work.commit_unverified"]
    assert unverified
    payload = json.dumps(unverified[-1]["payload"])
    assert str(other_repo) not in payload
    assert unverified[-1]["payload"]["evidence_source"] == (
        WORKSPACE_EVIDENCE_CLIENT_ASSERTED
    )


def test_the_registered_path_is_never_usable_to_claim_a_verified_root(
    service, tmp_path
):
    """A caller cannot promote its own path by supplying the reserved key.

    The key is the only record of whether the server confirmed the root, so if a
    request body could set it the whole boundary would be advisory.
    """
    project_repo = _make_repo(tmp_path / "project-repo")
    project = service.create_project(root_path=str(project_repo), name="Room")

    with pytest.raises(DomainError) as refused:
        service.register_workspace(
            project["id"],
            host_key="forger",
            host_name="Forger",
            local_path=str(project_repo),
            workspace_metadata={
                WORKSPACE_EVIDENCE_SOURCE_KEY: WORKSPACE_EVIDENCE_SERVER_VERIFIED
            },
        )
    assert refused.value.code == "reserved_workspace_metadata_key"
    assert refused.value.status_code == 422


def test_a_workspace_with_no_provenance_record_is_treated_as_unverified(
    service, tmp_path
):
    """A row whose provenance key is gone must not keep its git evidence.

    The reader defaults an absent key to unverified: defaulting the other way
    would grandfather exactly the remote registrations this card is about.
    """
    project_repo = _make_repo(tmp_path / "project-repo", dirty=True)
    other_repo = _make_repo(tmp_path / "other-repo", branch="legacy-branch", dirty=True)
    project = service.create_project(root_path=str(project_repo), name="Room")
    project_id = project["id"]

    session = _remote_session(service, project, other_repo)
    # Simulate a pre-existing row: the key is simply absent.
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE workspaces SET metadata_json = '{}' WHERE id = ?",
            (session["workspace"]["id"],),
        )

    task = _claimed_task(service, project_id, session)
    report = service.submit_work_report(
        project_id,
        task["id"],
        session_id=session["agent"]["id"],
        token=session["token"],
        summary="did the work",
        files=["src/thing.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    evidence = _stored_evidence(service, project_id, report["report_id"])
    assert evidence["captured"] is False
    assert "legacy-branch" not in json.dumps(evidence)


# --- the verified path that must keep working -------------------------------


def test_a_server_verified_workspace_still_yields_git_evidence(service, tmp_path):
    """The local branch's evidence is unaffected: it registered a verified root."""
    project_repo = _make_repo(tmp_path / "project-repo", branch="main", dirty=True)
    project = service.create_project(root_path=str(project_repo), name="Room")
    project_id = project["id"]

    registered = service.register_workspace(
        project_id,
        host_key="local-host",
        host_name="Local PC",
        local_path=str(project_repo),
        worktree=str(project_repo),
        server_verified=True,
    )
    session = service.join_room(
        project_id,
        name="Local Agent",
        client="codex",
        model="unknown",
        worktree=str(project_repo),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = _claimed_task(service, project_id, session)
    report = service.submit_work_report(
        project_id,
        task["id"],
        session_id=session["agent"]["id"],
        token=session["token"],
        summary="did the work",
        files=["tracked.txt"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )

    evidence = _stored_evidence(service, project_id, report["report_id"])
    assert evidence["captured"] is True
    assert evidence["branch"] == "main"
    assert evidence["head"]
    # A verified root is the one case where naming the path is fine.
    assert evidence["worktree"] == str(project_repo.resolve())


def test_verified_roots_still_reject_a_worktree_outside_the_registration(
    service, tmp_path
):
    """Containment is still enforced where it means something."""
    project_repo = _make_repo(tmp_path / "project-repo")
    outside = _make_repo(tmp_path / "outside-repo")
    project = service.create_project(root_path=str(project_repo), name="Room")
    project_id = project["id"]

    registered = service.register_workspace(
        project_id,
        host_key="local-host",
        host_name="Local PC",
        local_path=str(project_repo),
        server_verified=True,
    )
    session = service.join_room(
        project_id,
        name="Local Agent",
        client="codex",
        model="unknown",
        worktree=str(outside),
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    task = _claimed_task(service, project_id, session)

    with pytest.raises(DomainError) as refused:
        service.submit_work_report(
            project_id,
            task["id"],
            session_id=session["agent"]["id"],
            token=session["token"],
            summary="did the work",
            files=["src/thing.py"],
            tests=[{"command": "pytest", "exit_code": 0}],
        )
    assert refused.value.code == "untrusted_worktree"
