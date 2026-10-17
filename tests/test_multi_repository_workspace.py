"""#377: the workspace, not the selected child repository, is the work boundary.

A Project rooted at a non-Git workspace names one primary repository for the
Room's identity and display. Its sibling repositories inside the same registered
workspace are first-class work targets: a Session rooted there submits its own
commit automatically and records the repository it actually proved.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import register_checkout_project
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
        title="Workspace repository delivery",
        acceptance_criteria=["Evidence names the repository it proved"],
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
        summary="Delivered in a workspace repository",
        tests=[{"command": "fixture validation", "exit_code": 0}],
        **kwargs,
    )


@pytest.mark.parametrize("sibling", ["b", "c"])
def test_sibling_repository_session_reports_its_own_commit(
    service, project_dir, sibling
):
    repository(project_dir / "a", remote=True)
    sibling_commit = repository(project_dir / sibling)
    (project_dir / "deploy").mkdir()
    deployed = project_dir / "deploy" / "run.txt"
    deployed.write_text("deployed\n", encoding="utf-8")
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    task, joined = claim(service, project, worktree=str(project_dir / sibling))

    result = report(
        service,
        project,
        task,
        joined,
        files=[f"{sibling}/code.txt", "deploy/run.txt"],
        commit_hash=sibling_commit,
    )

    assert result["delivery_mode"] == "git"
    assert "commit_repo" not in result or result["commit_repo"] == ""
    assert service.get_task(project["id"], task["id"])["phase"] == "awaiting_review"
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["captured"] is True
    assert evidence["reported_commit_exists"] is True
    assert evidence["repository_path"] == sibling
    assert evidence["repository_root"] == str(
        (project_dir / sibling).resolve()
    )
    assert evidence["worktree"] == str((project_dir / sibling).resolve())
    assert evidence["commit_files"] == ["code.txt"]
    by_path = {item["path"]: item for item in evidence["file_evidence"]}
    assert by_path[f"{sibling}/code.txt"]["status"] == "captured"
    assert by_path[f"{sibling}/code.txt"]["repository_relative_path"] == "code.txt"
    assert by_path[f"{sibling}/code.txt"]["sha256"] == hashlib.sha256(
        (project_dir / sibling / "code.txt").read_bytes()
    ).hexdigest()
    assert by_path["deploy/run.txt"]["status"] == "outside_repository"


def test_repository_subdirectory_session_records_its_repository(
    service, project_dir
):
    repository(project_dir / "a", remote=True)
    sibling = project_dir / "b"
    repository(sibling)
    (sibling / "pkg").mkdir()
    (sibling / "pkg" / "code.txt").write_text("subdir\n", encoding="utf-8")
    git(sibling, "add", "pkg/code.txt")
    git(
        sibling,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "subdir change",
    )
    subdir_commit = git(sibling, "rev-parse", "HEAD")
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    task, joined = claim(service, project, worktree=str(sibling / "pkg"))

    report(
        service,
        project,
        task,
        joined,
        files=["b/pkg/code.txt"],
        commit_hash=subdir_commit,
    )

    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["repository_path"] == "b"
    assert evidence["repository_root"] == str(sibling.resolve())
    assert evidence["worktree"] == str((sibling / "pkg").resolve())
    assert evidence["commit_files"] == ["pkg/code.txt"]
    item = next(
        entry
        for entry in evidence["file_evidence"]
        if entry["path"] == "b/pkg/code.txt"
    )
    assert item["status"] == "captured"
    assert item["repository_relative_path"] == "pkg/code.txt"


def test_bootstrap_from_a_sibling_repository_binds_to_the_workspace_room(
    service, project_dir
):
    repository(project_dir / "a", remote=True)
    sibling_commit = repository(project_dir / "b", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    register_checkout_project(project_dir, project)

    outcome = bootstrap_local_room(
        service,
        software_key="fixture-client",
        software_name="Fixture",
        client="test-client",
        workspace_roots=[project_dir / "b"],
        cwd=project_dir / "b",
    )

    assert outcome.public["status"] == "ready", outcome.public
    binding = outcome.binding
    assert binding is not None
    assert binding.project_id == project["id"]
    task = service.create_task(
        project["id"],
        title="Started in a sibling repository",
        acceptance_criteria=["The workspace Room accepts the work"],
        actor_session_id=binding.session_id,
        token=binding.token,
    )["task"]
    service.claim_task(
        project["id"], task["id"], binding.session_id, binding.token
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=binding.session_id,
        token=binding.token,
        summary="Delivered from the sibling checkout",
        files=["b/code.txt"],
        tests=[{"command": "fixture validation", "exit_code": 0}],
        commit_hash=sibling_commit,
    )
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["reported_commit_exists"] is True
    assert evidence["repository_path"] == "b"
    assert evidence["repository_root"] == str((project_dir / "b").resolve())


def test_bootstrap_prefers_a_repository_dedicated_room(service, project_dir):
    repository(project_dir / "a", remote=True)
    repository(project_dir / "b", remote=True)
    service.create_project(root_path=str(project_dir), repository_path="a")
    dedicated = service.create_project(root_path=str(project_dir / "b"))
    register_checkout_project(project_dir / "b", dedicated)

    outcome = bootstrap_local_room(
        service,
        software_key="fixture-client",
        software_name="Fixture",
        client="test-client",
        workspace_roots=[project_dir / "b"],
        cwd=project_dir / "b",
    )

    assert outcome.public["status"] == "ready", outcome.public
    assert outcome.binding is not None
    assert outcome.binding.project_id == dedicated["id"]


def test_diagnostics_separate_usable_from_another_rooms_repositories(
    service, project_dir
):
    repository(project_dir / "a", remote=True)
    repository(project_dir / "b", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    service.create_project(root_path=str(project_dir / "b"))

    diagnostics = service._repository_diagnostics(
        service.get_project(project["id"])
    )

    available = {
        item["relative_path"] for item in diagnostics["repositories_available"]
    }
    excluded = {
        item["relative_path"] for item in diagnostics["repositories_excluded"]
    }
    assert "a" in available
    assert "b" in excluded
    b_entry = next(
        item
        for item in diagnostics["repositories_excluded"]
        if item["relative_path"] == "b"
    )
    assert b_entry["reason"] == "owned_by_another_room"


def test_workspace_root_session_uses_the_commit_repository(service, project_dir):
    """The reported commit chooses its repository when the Session is outside Git.

    This is the upstream #113 shape: the Session runs at the workspace root, the
    commit lives in a sibling repository, and no extra ``commit_repo`` should be
    required to record it.
    """
    repository(project_dir / "a", remote=True)
    sibling_commit = repository(project_dir / "b")
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    task, joined = claim(service, project)

    report(
        service,
        project,
        task,
        joined,
        files=["b/code.txt"],
        commit_hash=sibling_commit,
    )

    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["reported_commit_exists"] is True
    assert evidence["repository_path"] == "b"
    assert evidence["repository_root"] == str((project_dir / "b").resolve())
    assert evidence["worktree"] == str((project_dir / "b").resolve())
    assert service.get_task(project["id"], task["id"])["phase"] == "awaiting_review"


def test_workspace_root_session_keeps_primary_evidence_for_its_own_commit(
    service, project_dir
):
    primary_commit = repository(project_dir / "a", remote=True)
    repository(project_dir / "b")
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    task, joined = claim(service, project)

    report(
        service,
        project,
        task,
        joined,
        files=["a/code.txt"],
        commit_hash=primary_commit,
    )

    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["reported_commit_exists"] is True
    assert evidence["repository_path"] == "a"
    assert evidence["repository_root"] == str((project_dir / "a").resolve())
    item = next(
        entry
        for entry in evidence["file_evidence"]
        if entry["path"] == "a/code.txt"
    )
    assert item["status"] == "captured"
    assert item["repository_relative_path"] == "code.txt"


def test_ambiguous_commit_between_workspace_repositories_is_refused(
    service, project_dir
):
    repository(project_dir / "a", remote=True)
    source = project_dir / "b"
    shared_commit = repository(source)
    git(project_dir, "clone", "-q", str(source), str(project_dir / "c"))
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["b/code.txt"],
            commit_hash=shared_commit,
        )

    assert error.value.code == "ambiguous_commit_repository"
    assert error.value.details["required_action"] == (
        "pass_commit_repo_to_choose_the_repository"
    )
    assert {"b", "c"} <= set(error.value.details["repositories"])
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_commit_absent_from_the_workspace_is_refused_with_primary_diagnostics(
    service, project_dir, tmp_path
):
    repository(project_dir / "a", remote=True)
    outside_commit = repository(tmp_path / "outside")
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
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
    assert error.value.details["searched_worktree"] == str(
        (project_dir / "a").resolve()
    )
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_repository_owned_by_another_room_is_refused(service, project_dir):
    repository(project_dir / "a", remote=True)
    sibling_commit = repository(project_dir / "b", remote=True)
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    other = service.create_project(root_path=str(project_dir / "b"))
    task, joined = claim(service, project, worktree=str(project_dir / "b"))

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["b/code.txt"],
            commit_hash=sibling_commit,
        )

    assert error.value.code == "repository_owned_by_another_room"
    assert error.value.details["required_action"] == "use_that_repository_room"
    assert error.value.details["project_id"] == other["id"]
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_local_delivery_cannot_cover_a_sibling_repository_file(
    service, project_dir
):
    repository(project_dir / "a", remote=True)
    repository(project_dir / "b")
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    (project_dir / "deploy").mkdir()
    (project_dir / "deploy" / "run.txt").write_text("local\n", encoding="utf-8")
    task, joined = claim(service, project)

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["b/code.txt"],
            delivery_mode="local",
        )

    assert error.value.code == "delivery_mode_conflict"
    assert error.value.details["files"] == ["b/code.txt"]

    report(
        service,
        project,
        task,
        joined,
        files=["deploy/run.txt"],
        delivery_mode="local",
    )
    evidence = service.snapshot(project["id"])["reports"][0]["system_evidence"]
    assert evidence["file_evidence"][0]["status"] == "captured"


def test_worktree_outside_the_registered_workspace_is_still_refused(
    service, project_dir, tmp_path
):
    repository(project_dir / "a", remote=True)
    outside = tmp_path / "outside"
    outside_commit = repository(outside)
    project = service.create_project(
        root_path=str(project_dir), repository_path="a"
    )
    task, joined = claim(service, project, worktree=str(outside))

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["code.txt"],
            commit_hash=outside_commit,
        )

    assert error.value.code == "untrusted_worktree"
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"


def test_git_root_project_keeps_single_repository_evidence(service, project_dir):
    repository(project_dir, remote=True)
    nested_commit = repository(project_dir / "nested")
    project = service.create_project(root_path=str(project_dir))
    task, joined = claim(service, project, worktree=str(project_dir / "nested"))

    with pytest.raises(DomainError) as error:
        report(
            service,
            project,
            task,
            joined,
            files=["nested/code.txt"],
            commit_hash=nested_commit,
        )

    assert error.value.code == "untrusted_repository"
    assert service.get_task(project["id"], task["id"])["phase"] == "claimed"
