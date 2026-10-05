from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.config import load_settings
from agentchatroom.database import Database, SCHEMA
from agentchatroom.errors import DomainError
from agentchatroom.integrations import build_project_coordination_instructions
from agentchatroom.project_registration import (
    checkout_scope, load_checkout_registration, register_checkout_project,
    resolve_checkout_project_key, stored_project_scope,
)
from tests.conftest import join_room_with_workspace


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8").strip()


def repository(root, *, remote=False):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    (root / "code.txt").write_text(root.name + "\n", encoding="utf-8")
    git(root, "add", "code.txt")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    if remote:
        git(root, "remote", "add", "origin", "https://example.invalid/fixture/" + root.name + ".git")
    return git(root, "rev-parse", "HEAD")


def claim(service, project, *, worktree=None):
    joined = join_room_with_workspace(service, project, agent_key="builder-main",
                                     name="Builder", client="test-client", model="unknown",
                                     worktree=worktree or project["root_path"])
    task = service.create_task(project["id"], title="Repository evidence",
                               acceptance_criteria=["Evidence uses the selected repository"],
                               actor_session_id=joined["agent"]["id"], token=joined["token"])["task"]
    service.claim_task(project["id"], task["id"], joined["agent"]["id"], joined["token"])
    return task, joined


def report(service, project, task, joined, commit):
    return service.submit_work_report(project["id"], task["id"],
                                     session_id=joined["agent"]["id"], token=joined["token"],
                                     summary="Captured selected repository", files=["code.txt", "output.txt"],
                                     tests=[{"command": "fixture validation", "exit_code": 0}], commit_hash=commit)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_discovery_never_selects_first_child(service, project_dir, count):
    (project_dir / "ordinary").mkdir()
    (project_dir / "unrelated.txt").write_text("outside\n", encoding="utf-8")
    for index in range(count):
        repository(project_dir / f"repo-{index}", remote=index == 1)
    inspection = service.inspect_repository_candidates(str(project_dir))
    assert [item["relative_path"] for item in inspection["candidates"]] == [f"repo-{index}" for index in range(count)]
    assert inspection["complete"]
    if count:
        with pytest.raises(DomainError) as error:
            service.create_project(root_path=str(project_dir))
        assert error.value.code == "repository_selection_required"
        assert error.value.details["required_action"] == "select_repository_explicitly"
    project = service.create_project(root_path=str(project_dir), repository_path="")
    assert project["root_path"] == str(project_dir)
    assert project["repository_path"] == ""
    assert project["project_source"] == "path"


@pytest.mark.parametrize("remote", [False, True])
def test_selected_child_preserves_workspace_and_verifies_real_evidence(service, project_dir, remote):
    child = project_dir / "项目"
    commit = repository(child, remote=remote)
    (child / "output.txt").write_text("artifact\n", encoding="utf-8")
    (project_dir / "outside.txt").write_text("excluded\n", encoding="utf-8")
    project = service.create_project(root_path=str(project_dir), repository_path="项目")
    register_checkout_project(project_dir, project)
    assert project["repository_root"] == str(child)
    assert project["workspace_root"] == str(project_dir)
    assert project["logical_path"] == ""
    assert project["project_source"] == ("git_remote" if remote else "git_local")
    assert checkout_scope(project_dir) == stored_project_scope(project)
    assert resolve_checkout_project_key(project_dir) == (project["project_key"], True)
    resolved = service.resolve_project_for_join(root_path=str(project_dir), registered_project_key=project["project_key"])
    assert resolved["id"] == project["id"]
    task, joined = claim(service, project)
    report(service, project, task, joined, commit)
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["captured"] and evidence["reported_commit_exists"]
    assert evidence["worktree"] == str(child)
    assert evidence["commit_files"] == ["code.txt"]
    assert evidence["commit_diff_stat"]
    assert "outside.txt" not in str(evidence)
    assert evidence["file_evidence"][1]["sha256"] == hashlib.sha256((child / "output.txt").read_bytes()).hexdigest()
    assert service.get_task(project["id"], task["id"])["phase"] == "awaiting_review"


def test_git_file_worktree_is_a_candidate_and_accepts_commit(service, project_dir, tmp_path):
    origin = tmp_path / "origin"
    commit = repository(origin)
    child = project_dir / "feature"
    git(origin, "worktree", "add", "-qb", "feature", str(child))
    assert (child / ".git").is_file()
    assert service.inspect_repository_candidates(str(project_dir))["candidates"][0]["relative_path"] == "feature"
    project = service.create_project(root_path=str(project_dir), repository_path="feature")
    task, joined = claim(service, project)
    report(service, project, task, joined, commit)
    assert service.snapshot(project["id"])["reports"][0]["system_evidence"]["reported_commit_exists"]


def test_existing_local_room_selection_retains_history_tasks_sessions_and_lease(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    register_checkout_project(project_dir, project)
    task, joined = claim(service, project)
    lease = service.acquire_lease(project["id"], session_id=joined["agent"]["id"], token=joined["token"],
                                 task_id=task["id"], path_pattern="code.txt")["lease"]
    with service.database.connect() as connection:
        before = [dict(row) for row in connection.execute("SELECT * FROM events WHERE project_id = ? ORDER BY id", (project["id"],))]
    repository(project_dir / "repo")
    updated = service.select_project_repository(project["id"], "repo")["project"]
    assert updated["id"] == project["id"] and updated["project_key"] == project["project_key"]
    assert updated["root_path"] == project["root_path"]
    assert service.get_task(project["id"], task["id"])["owner_session_id"] == joined["agent"]["id"]
    assert any(item["id"] == lease["id"] and item["active"] for item in service.list_leases(project["id"]))
    with service.database.connect() as connection:
        after = [dict(row) for row in connection.execute("SELECT * FROM events WHERE project_id = ? ORDER BY id", (project["id"],))]
    assert after[:len(before)] == before
    assert after[-1]["event_type"] == "project.updated"
    assert load_checkout_registration(project_dir)["repository_path"] == "repo"


@pytest.mark.parametrize("selection", ["../foreign", "/foreign", "C:/foreign", "repo/../../foreign"])
def test_selection_rejects_escape(service, project, selection):
    with pytest.raises(DomainError) as error:
        service.select_project_repository(project["id"], selection)
    assert error.value.code in {"invalid_repository_path", "untrusted_repository"}
    assert not service.get_project(project["id"])["repository_path"]


def test_cannot_select_ordinary_directory(service, project, project_dir):
    (project_dir / "ordinary").mkdir()
    with pytest.raises(DomainError) as error:
        service.select_project_repository(project["id"], "ordinary")
    assert error.value.code == "repository_not_found"


def test_existing_room_scope_collision_fails_without_rewriting_registration(service, project_dir):
    parent = service.create_project(root_path=str(project_dir))
    register_checkout_project(project_dir, parent)
    repository(project_dir / "repo")
    other = service.create_project(root_path=str(project_dir / "repo"))
    before = (project_dir / ".agentchatroom" / "project.json").read_bytes()
    with pytest.raises(DomainError) as error:
        service.select_project_repository(parent["id"], "repo")
    assert error.value.code == "project_scope_conflict"
    assert other["id"] in error.value.details["project_ids"]
    assert (project_dir / ".agentchatroom" / "project.json").read_bytes() == before
    assert service.get_project(parent["id"])["repository_path"] == ""


def test_foreign_registration_blocks_selection(service, project_dir, tmp_path):
    parent = service.create_project(root_path=str(project_dir))
    register_checkout_project(project_dir, parent)
    repository(project_dir / "repo")
    path = project_dir / ".agentchatroom" / "project.json"
    path.write_text(path.read_text(encoding="utf-8").replace(parent["project_key"], "prj_foreign"), encoding="utf-8")
    with pytest.raises(DomainError) as error:
        service.select_project_repository(parent["id"], "repo")
    assert error.value.code == "project_registration_conflict"


def test_sibling_and_deeper_independent_repositories_cannot_supply_evidence(service, project_dir):
    commit = repository(project_dir / "selected")
    repository(project_dir / "sibling")
    repository(project_dir / "selected" / "nested")
    project = service.create_project(root_path=str(project_dir), repository_path="selected")
    for relative in ("sibling", "selected/nested"):
        task, joined = claim(service, project, worktree=str(project_dir / relative))
        with pytest.raises(DomainError) as error:
            report(service, project, task, joined, commit)
        assert error.value.code == "untrusted_repository"
        assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_other_child_commit_is_not_accepted_in_selected_repository(service, project_dir):
    repository(project_dir / "chosen")
    other_commit = repository(project_dir / "other")
    project = service.create_project(root_path=str(project_dir), repository_path="chosen")
    task, joined = claim(service, project)
    with pytest.raises(DomainError) as error:
        report(service, project, task, joined, other_commit)
    assert error.value.code == "invalid_commit_hash"
    assert error.value.details["reported_commit_hash"] == other_commit
    assert not service.snapshot(project["id"])["reports"]


def test_plain_parent_report_is_not_silently_bound_to_child(service, project_dir):
    project = service.create_project(root_path=str(project_dir))
    commit = repository(project_dir / "child")
    task, joined = claim(service, project)
    with pytest.raises(DomainError) as error:
        report(service, project, task, joined, commit)
    assert error.value.code == "repository_selection_required"
    assert error.value.details["reported_commit_hash"] == commit
    assert error.value.details["reason"] == "not_a_git_worktree"
    assert error.value.details["required_action"] == "inspect_and_select_repository_in_project_settings"
    assert service.get_project(project["id"])["repository_path"] == ""


def test_unavailable_selected_repository_reports_configuration_error(service, project_dir):
    child = project_dir / "selected"
    commit = repository(child)
    project = service.create_project(root_path=str(project_dir), repository_path="selected")
    task, joined = claim(service, project)
    (child / ".git").rename(child / ".git-unavailable")
    with pytest.raises(DomainError) as error:
        report(service, project, task, joined, commit)
    assert error.value.code == "repository_not_found"
    assert error.value.details["reported_commit_hash"] == commit
    assert error.value.details["required_action"] == "recheck_repository_in_project_settings"
    assert not service.snapshot(project["id"])["reports"]


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("relative", ["branches/feature", "selected/feature"])
def test_parent_workspace_reports_actual_linked_worktree(service, project_dir, remote, relative):
    child = project_dir / "selected"
    main_head = repository(child, remote=remote)
    project = service.create_project(root_path=str(project_dir), repository_path="selected")
    branch = project_dir / relative
    git(child, "worktree", "add", "-qb", "fixture-feature", str(branch))
    (branch / "code.txt").write_text("worktree changed\n", encoding="utf-8")
    git(branch, "add", "code.txt")
    git(branch, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "worktree change")
    commit = git(branch, "rev-parse", "HEAD")
    assert main_head != commit and git(child, "rev-parse", "HEAD") == main_head
    assert (branch / ".git").is_file()
    task, joined = claim(service, project, worktree=str(branch))
    report(service, project, task, joined, commit)
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["reported_commit_exists"] is True
    assert evidence["worktree"] == str(branch)
    assert evidence["head"] == commit
    code = next(item for item in evidence["file_evidence"] if item["path"] == "code.txt")
    assert code["sha256"] == hashlib.sha256((branch / "code.txt").read_bytes()).hexdigest()


def test_same_remote_independent_clone_is_not_a_selected_worktree(service, project_dir):
    child = project_dir / "selected"
    repository(child, remote=True)
    project = service.create_project(root_path=str(project_dir), repository_path="selected")
    clone = project_dir / "clone"
    git(project_dir, "clone", "-q", str(child), str(clone))
    git(clone, "remote", "set-url", "origin", "https://example.invalid/fixture/selected.git")
    task, joined = claim(service, project, worktree=str(clone))
    with pytest.raises(DomainError) as error:
        report(service, project, task, joined, git(clone, "rev-parse", "HEAD"))
    assert error.value.code == "untrusted_repository"


def test_root_git_and_monorepo_subproject_stay_distinct(service, project_dir):
    repository(project_dir)
    scoped = project_dir / "packages" / "api"
    scoped.mkdir(parents=True)
    root_project = service.create_project(root_path=str(project_dir))
    scoped_project = service.create_project(root_path=str(scoped))
    assert root_project["id"] != scoped_project["id"]
    assert scoped_project["logical_path"] == "packages/api"
    assert service.inspect_repository_candidates(str(scoped))["candidates"][0]["relative_path"] == ""
    register_checkout_project(scoped, scoped_project)
    assert checkout_scope(scoped) == stored_project_scope(scoped_project)


def test_bootstrap_from_parent_keeps_selected_repository(service, project_dir):
    repository(project_dir / "repo")
    project = service.create_project(root_path=str(project_dir), repository_path="repo")
    register_checkout_project(project_dir, project)
    outcome = bootstrap_local_room(service, software_key="fixture-client", software_name="Fixture",
                                   client="test-client", workspace_roots=[project_dir], cwd=project_dir)
    assert outcome.public["status"] == "ready"
    assert outcome.binding.project_id == project["id"]
    assert service.get_project(project["id"])["repository_path"] == "repo"


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("database_first", [False, True])
def test_bootstrap_from_contained_worktree_collects_its_actual_files(service, project_dir, remote, database_first):
    child = project_dir / "selected"
    repository(child, remote=remote)
    project = service.create_project(root_path=str(project_dir), repository_path="selected")
    register_checkout_project(project_dir, project)
    branch = project_dir / "branches" / "feature"
    git(child, "worktree", "add", "-qb", "fixture-feature", str(branch))
    (branch / "code.txt").write_text("bootstrap worktree change\n", encoding="utf-8")
    git(branch, "add", "code.txt")
    git(branch, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "worktree change")
    outcome = bootstrap_local_room(service, software_key="fixture-client", software_name="Fixture",
                                   client="test-client", workspace_roots=[branch], database_first=database_first,
                                   selected_project_id=project["id"] if database_first else None)
    assert outcome.public["status"] == "ready"
    binding = outcome.binding
    task = service.create_task(project["id"], title="Bootstrapped worktree",
                               acceptance_criteria=["Actual worktree files"],
                               actor_session_id=binding.session_id, token=binding.token)["task"]
    service.claim_task(project["id"], task["id"], binding.session_id, binding.token)
    service.submit_work_report(project["id"], task["id"], session_id=binding.session_id,
                               token=binding.token, summary="Actual contained worktree evidence",
                               files=["code.txt"], tests=[{"command":"fixture validation", "exit_code":0}],
                               commit_hash=git(branch, "rev-parse", "HEAD"))
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["worktree"] == str(branch)
    assert evidence["file_evidence"][0]["sha256"] == hashlib.sha256((branch / "code.txt").read_bytes()).hexdigest()
    assert service.get_project(project["id"])["root_path"] == str(project_dir)


def test_contained_worktree_registered_to_another_room_is_not_rebound(service, project_dir):
    child = project_dir / "selected"
    repository(child)
    project = service.create_project(root_path=str(project_dir), repository_path="selected")
    branch = project_dir / "branches" / "feature"
    git(child, "worktree", "add", "-qb", "fixture-feature", str(branch))
    other = service.create_project(root_path=str(branch), name="Worktree Room")
    register_checkout_project(branch, other)
    registration = (branch / ".agentchatroom/project.json").read_bytes()
    assert not service.matches_contained_project_worktree(project, branch)
    outcome = bootstrap_local_room(service, software_key="fixture-client", software_name="Fixture",
                                   client="test-client", workspace_roots=[branch], database_first=True,
                                   selected_project_id=project["id"])
    assert outcome.public["status"] == "project_workspace_mismatch"
    assert (branch / ".agentchatroom/project.json").read_bytes() == registration


def test_bounded_discovery_signals_partial_results_and_does_not_poll(service, project_dir, monkeypatch):
    repository(project_dir / "deep" / "deeper" / "repo")
    service.settings = replace(service.settings, repository_scan_max_depth=1)
    inspection = service.inspect_repository_candidates(str(project_dir))
    assert inspection["truncated"] and not inspection["complete"]
    project = service.create_project(root_path=str(project_dir), repository_path="")
    monkeypatch.setattr(service, "inspect_repository_candidates", lambda *_: pytest.fail("recursive scan in polling"))
    service.list_projects()
    service.get_project(project["id"])


def test_unreadable_directory_is_observable(service, project_dir, monkeypatch):
    original = os.scandir
    (project_dir / "denied").mkdir()
    def scandir(path):
        if Path(path).name == "denied":
            raise PermissionError("fixture permission error")
        return original(path)
    monkeypatch.setattr(os, "scandir", scandir)
    result = service.inspect_repository_candidates(str(project_dir))
    assert not result["complete"]
    assert result["errors"] == [{"relative_path": "denied", "reason": "unreadable_directory"}]


def test_symlink_escape_is_not_discovered_or_selected(service, project, project_dir, tmp_path):
    external = tmp_path / "external"
    repository(external)
    link = project_dir / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not allow creating directory symlinks")
    assert not service.inspect_repository_candidates(str(project_dir))["candidates"]
    with pytest.raises(DomainError) as error:
        service.select_project_repository(project["id"], "linked")
    assert error.value.code == "untrusted_repository"


def test_rest_discovery_and_existing_room_association(settings, project_dir):
    repository(project_dir / "one")
    repository(project_dir / "two")
    with TestClient(create_app(settings)) as client:
        inspected = client.post("/api/v1/local/repositories/inspect", json={"root_path": str(project_dir)})
        assert inspected.status_code == 200 and len(inspected.json()["candidates"]) == 2
        refused = client.post("/api/v1/projects", json={"root_path": str(project_dir)})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "repository_selection_required"
        created = client.post("/api/v1/projects", json={"root_path": str(project_dir), "repository_path": ""}).json()
        selected = client.put(f"/api/v1/projects/{created['id']}/repository", json={"repository_path": "two"})
        assert selected.status_code == 200
        assert selected.json()["project"]["id"] == created["id"]
        assert selected.json()["project"]["repository_path"] == "two"
        assert client.post(f"/api/v1/projects/{created['id']}/repository/inspect").status_code == 200


def test_schema_25_migration_preserves_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    old_schema = SCHEMA.replace("    repository_path TEXT NOT NULL DEFAULT '',\n", "")
    with sqlite3.connect(path) as connection:
        connection.executescript(old_schema)
        connection.execute("INSERT INTO schema_meta VALUES (25)")
        connection.execute("INSERT INTO projects(id, project_key, name, root_path, created_at, updated_at) VALUES ('room', 'prj_room', 'Existing', ?, 'before', 'before')", (str(tmp_path),))
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        project = dict(connection.execute("SELECT * FROM projects").fetchone())
    assert project["id"] == "room" and project["repository_path"] == ""
    assert project["created_at"] == "before"


def test_repository_scan_configuration_is_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_REPOSITORY_SCAN_MAX_DEPTH", "2")
    monkeypatch.setenv("AGENTCHATROOM_REPOSITORY_SCAN_EXCLUDES", "cache,generated")
    settings = load_settings(data_dir=tmp_path)
    assert settings.repository_scan_max_depth == 2
    assert settings.repository_scan_excludes == ("cache", "generated")
    monkeypatch.setenv("AGENTCHATROOM_REPOSITORY_SCAN_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="repository_scan_timeout_seconds"):
        load_settings(data_dir=tmp_path)


def test_generated_rules_allow_contained_independent_repositories():
    instructions = build_project_coordination_instructions({"name": "Fixture"})
    assert "user-authorized target inside the current workspace is allowed" in instructions
    assert "Workspace root, Git repository root, and Room Project scope are distinct boundaries" in instructions
    assert "targets outside the current workspace or owned by another Room" in instructions


def test_workspace_rooted_directly_at_selected_repository_also_reports(service, project_dir):
    child = project_dir / "repo"
    commit = repository(child)
    project = service.create_project(root_path=str(project_dir), repository_path="repo")
    registered = service.register_workspace(project["id"], host_key="direct-host", host_name="Direct",
                                            local_path=str(child), workspace_metadata={"repository_path": "../foreign"})
    assert registered["workspace"]["metadata"]["repository_path"] == ""
    joined = service.join_room(project["id"], agent_key="direct-builder", name="Direct builder", client="fixture",
                               model="unknown", worktree=str(child), host_id=registered["host"]["id"],
                               workspace_id=registered["workspace"]["id"])
    task = service.create_task(project["id"], title="Direct workspace evidence", acceptance_criteria=["Repository is trusted"],
                               actor_session_id=joined["agent"]["id"], token=joined["token"])["task"]
    service.claim_task(project["id"], task["id"], joined["agent"]["id"], joined["token"])
    report(service, project, task, joined, commit)
    assert service.snapshot(project["id"])["reports"][0]["system_evidence"]["reported_commit_exists"]


def test_selected_local_repository_can_upgrade_origin_without_new_room(service, project_dir):
    child = project_dir / "repo"
    repository(child)
    project = service.create_project(root_path=str(project_dir), repository_path="repo")
    register_checkout_project(project_dir, project)
    git(child, "remote", "add", "origin", "https://example.invalid/fixture/repo.git")
    assert resolve_checkout_project_key(project_dir) == (project["project_key"], True)
    resolved = service.resolve_project_for_join(root_path=str(project_dir), registered_project_key=project["project_key"])
    assert resolved["id"] == project["id"]
    assert resolved["repository_path"] == "repo" and resolved["git_remote"]


def test_existing_root_git_cannot_be_retargeted_to_independent_child(service, project_dir):
    repository(project_dir)
    project = service.create_project(root_path=str(project_dir))
    repository(project_dir / "child")
    with pytest.raises(DomainError) as error:
        service.select_project_repository(project["id"], "child")
    assert error.value.code == "repository_selection_conflict"


def test_failed_checkout_write_leaves_database_and_files_unchanged(service, project, project_dir, monkeypatch):
    from agentchatroom import project_registration
    register_checkout_project(project_dir, project)
    before = (project_dir / ".agentchatroom" / "project.json").read_bytes()
    repository(project_dir / "repo")
    original = project_registration._write_document
    def failing_write(path, document):
        original(path, document)
        raise DomainError("project_registration_write_failed", "Fixture failed after file replacement")
    monkeypatch.setattr(project_registration, "_write_document", failing_write)
    with pytest.raises(DomainError, match="Fixture failed"):
        service.select_project_repository(project["id"], "repo")
    assert (project_dir / ".agentchatroom" / "project.json").read_bytes() == before
    assert service.get_project(project["id"])["repository_path"] == ""


def test_artifact_evidence_never_reads_outside_repository(service, project_dir, tmp_path):
    repository(project_dir / "repo")
    external = tmp_path / "outside.txt"
    external.write_text("private\n", encoding="utf-8")
    evidence = service._repository_file_evidence(project_dir / "repo", ["../../outside.txt"])
    assert evidence == [{"path": "../../outside.txt", "status": "unreadable_or_outside_repository"}]
    service.settings = replace(service.settings, repository_evidence_max_file_bytes=1)
    evidence = service._repository_file_evidence(project_dir / "repo", ["code.txt"])
    assert evidence == [{"path": "code.txt", "status": "byte_budget_exceeded"}]
