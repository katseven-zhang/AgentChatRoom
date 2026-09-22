"""Tests for Task #166: Auto-syncing git_remote and project scope when a local path project initializes Git."""

from __future__ import annotations

import json
import subprocess
import pytest
from dataclasses import replace
from pathlib import Path

from agentchatroom import bootstrap as bootstrap_module
from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import (
    PROJECT_REGISTRATION_RELATIVE_PATH,
    _is_path_to_git_upgrade,
    load_checkout_registration,
    register_checkout_project,
    resolve_checkout_project_key,
)
from agentchatroom.services import AgentChatRoomService


@pytest.fixture()
def test_service(settings):
    service = AgentChatRoomService(
        Database(settings.database_path),
        replace(settings, heartbeat_timeout_seconds=0.3),
    )
    service.initialize()
    return service


def _init_git_with_remote(path: Path, remote_url: str):
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


def test_path_project_autosyncs_git_remote_and_scope_on_resolve(test_service, tmp_path):
    """Local path project later initialized with git + remote auto-heals during resolve and bootstrap."""
    service = test_service
    project_dir = tmp_path / "local_repo"
    project_dir.mkdir()

    # 1. Project created without git
    project = service.create_project(root_path=str(project_dir), name="LocalRepo")
    assert project["git_remote"] is None

    # 2. Registered in path mode
    reg_path = register_checkout_project(project_dir, project)
    assert reg_path.is_file()
    reg = load_checkout_registration(project_dir)
    assert reg["scope"]["kind"] == "path"

    # Verify initial resolve works in path mode
    key, present = resolve_checkout_project_key(project_dir)
    assert present is True
    assert key == project["project_key"]

    # 3. User initializes git and configures remote later
    remote_url = "https://github.com/test-org/local-repo.git"
    _init_git_with_remote(project_dir, remote_url)

    # 4. resolve_checkout_project_key detects path-to-git upgrade without conflict
    key_after, present_after = resolve_checkout_project_key(project_dir)
    assert present_after is True
    assert key_after == project["project_key"]

    # 5. resolve_project_for_join auto-syncs git_remote and does NOT throw project_registration_orphaned
    resolved = service.resolve_project_for_join(
        root_path=str(project_dir),
        registered_project_key=project["project_key"],
    )
    assert resolved["id"] == project["id"]
    assert resolved["git_remote"] == remote_url

    # Check database row was updated
    db_proj = service.get_project(project["id"])
    assert db_proj["git_remote"] == remote_url

    # Check project.updated event was emitted
    events = service.list_events(project["id"], after=0)["events"]
    updated_events = [e for e in events if e["event_type"] == "project.updated"]
    assert len(updated_events) >= 1
    assert updated_events[-1]["payload"]["git_remote"] == remote_url

    # 6. bootstrap_local_room succeeds and updates .agentchatroom/project.json scope to git
    outcome = bootstrap_local_room(
        service,
        software_key="boot-test",
        software_name="Boot Test",
        client="test",
        model="unknown",
        workspace_roots=[project_dir],
        explicit_project_path=project_dir,
    )
    assert outcome.binding is not None

    # Verify project.json was updated to git scope
    updated_reg = load_checkout_registration(project_dir)
    assert updated_reg["scope"]["kind"] == "git"
    assert "local-repo" in updated_reg["scope"]["identity"]


def test_get_project_autosyncs_git_remote_from_disk(test_service, tmp_path):
    """get_project checks disk and syncs git_remote if git was added post-creation."""
    service = test_service
    project_dir = tmp_path / "post_git_repo"
    project_dir.mkdir()

    project = service.create_project(root_path=str(project_dir), name="PostGit")
    assert project["git_remote"] is None

    remote_url = "https://github.com/test-org/post-git.git"
    _init_git_with_remote(project_dir, remote_url)

    # Calling get_project should detect git remote and update database
    fetched = service.get_project(project["id"])
    assert fetched["git_remote"] == remote_url


def test_resolve_project_for_join_without_registered_key_autosyncs(test_service, tmp_path):
    """Even without registered_project_key, matching active path project auto-heals when git is added."""
    service = test_service
    project_dir = tmp_path / "no_key_repo"
    project_dir.mkdir()

    project = service.create_project(root_path=str(project_dir), name="NoKey")
    assert project["git_remote"] is None

    remote_url = "https://github.com/test-org/no-key.git"
    _init_git_with_remote(project_dir, remote_url)

    resolved = service.resolve_project_for_join(root_path=str(project_dir))
    assert resolved["id"] == project["id"]
    assert resolved["git_remote"] == remote_url


def test_is_path_to_git_upgrade_safety():
    """_is_path_to_git_upgrade returns False if directory identity does not match."""
    # When identity matches:
    assert _is_path_to_git_upgrade(
        r"C:\my\project",
        {"kind": "path", "identity": r"c:\my\project"},
        {"kind": "git", "identity": "https://github.com/foo/bar"},
    ) is True

    # When registered scope is already git:
    assert _is_path_to_git_upgrade(
        r"C:\my\project",
        {"kind": "git", "identity": "https://github.com/old/repo"},
        {"kind": "git", "identity": "https://github.com/new/repo"},
    ) is False

    # When directory does not match registered identity:
    assert _is_path_to_git_upgrade(
        r"C:\different\project",
        {"kind": "path", "identity": r"c:\my\project"},
        {"kind": "git", "identity": "https://github.com/foo/bar"},
    ) is False


def test_resolve_rejects_when_registered_key_differs_from_git_scope_owner(
    test_service, tmp_path
):
    """#166 退回回归：登记指向 PathRoom A，而同 remote 的 git scope 已被
    活动中的 GitRoom B 占用时，resolve 必须显式 409，不得静默串房到 B。"""
    service = test_service
    dir_a = tmp_path / "repo_a"
    dir_a.mkdir()
    dir_b = tmp_path / "repo_b"
    dir_b.mkdir()
    remote_url = "https://github.com/test-org/shared.git"

    # GitRoom B 先登记并占用该 remote scope。
    _init_git_with_remote(dir_b, remote_url)
    project_b = service.create_project(root_path=str(dir_b), name="GitRoom B")
    assert project_b["git_remote"] == remote_url

    # PathRoom A 以 path scope 登记且尚无 git。
    project_a = service.create_project(root_path=str(dir_a), name="PathRoom A")
    register_checkout_project(dir_a, project_a)
    assert service.get_project(project_a["id"])["git_remote"] is None

    # 用户之后才在 A 初始化 git 并指向同一 remote。
    _init_git_with_remote(dir_a, remote_url)

    with pytest.raises(DomainError) as conflict:
        service.resolve_project_for_join(
            root_path=str(dir_a),
            registered_project_key=project_a["project_key"],
        )
    assert conflict.value.status_code == 409
    assert conflict.value.code == "project_registration_conflict"
    assert conflict.value.details["registered_project_id"] == project_a["id"]
    assert conflict.value.details["scope_project_ids"] == [project_b["id"]]

    # 失败封闭：A 未被 heal 成 git scope，事件历史也没有 project.updated。
    # （用裸只读连接检查——get_project 自身带磁盘自愈会污染断言。）
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT git_remote FROM projects WHERE id = ?", (project_a["id"],)
        ).fetchone()
    assert row["git_remote"] is None
    events = service.list_events(project_a["id"], after=0)["events"]
    assert not [e for e in events if e["event_type"] == "project.updated"]


def test_resolve_still_accepts_owner_of_claimed_git_scope(test_service, tmp_path):
    """#166 回归：同 remote 下，登记 key 与 scope 归属一致时正常解析，不误伤。"""
    service = test_service
    dir_b = tmp_path / "repo_owner"
    dir_b.mkdir()
    remote_url = "https://github.com/test-org/owner.git"

    _init_git_with_remote(dir_b, remote_url)
    project_b = service.create_project(root_path=str(dir_b), name="GitRoom B")
    register_checkout_project(dir_b, project_b)

    resolved = service.resolve_project_for_join(
        root_path=str(dir_b),
        registered_project_key=project_b["project_key"],
    )
    assert resolved["id"] == project_b["id"]
    assert resolved["git_remote"] == remote_url


def _init_git_only(path: Path):
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)


def test_get_project_reports_git_local_for_worktree_without_origin(
    test_service, tmp_path
):
    """#170：git init 后无 origin 也显示 Git（本地仓库），且不写库。"""
    service = test_service
    project_dir = tmp_path / "local_only"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="LocalOnly")
    assert project["project_source"] == "path"

    _init_git_only(project_dir)

    fetched = service.get_project(project["id"])
    assert fetched["project_source"] == "git_local"
    assert fetched["git_remote"] is None
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT git_remote FROM projects WHERE id = ?", (project["id"],)
        ).fetchone()
    assert row["git_remote"] is None
    events = service.list_events(project["id"], after=0)["events"]
    assert not [e for e in events if e["event_type"] == "project.updated"]

    listed = next(p for p in service.list_projects() if p["id"] == project["id"])
    assert listed["project_source"] == "git_local"


def test_get_project_heals_unique_path_project_when_remote_appears(
    test_service, tmp_path
):
    """#170/#172 无歧义：详情读取自愈升级，与 #166 加入路径语义一致。"""
    service = test_service
    project_dir = tmp_path / "heal_ok"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="HealOk")
    remote_url = "https://github.com/test-org/heal-ok.git"
    _init_git_with_remote(project_dir, remote_url)

    fetched = service.get_project(project["id"])
    assert fetched["git_remote"] == remote_url
    assert fetched["project_source"] == "git_remote"
    assert fetched["git_scope_recheck"]["status"] == "healed"
    events = service.list_events(project["id"], after=0)["events"]
    assert [
        e
        for e in events
        if e["event_type"] == "project.updated"
        and e["payload"].get("scope_healed") is True
    ]


def test_get_project_fails_closed_when_git_scope_owned_by_other_room(
    test_service, tmp_path
):
    """#172：path 项目 A 升级前若 git scope 已被 B 占用，不得静默双房。"""
    service = test_service
    dir_a = tmp_path / "owner_a"
    dir_b = tmp_path / "owner_b"
    dir_a.mkdir()
    dir_b.mkdir()
    remote_url = "https://github.com/test-org/owned.git"

    _init_git_with_remote(dir_b, remote_url)
    project_b = service.create_project(root_path=str(dir_b), name="OwnerB")
    assert project_b["git_remote"] == remote_url

    project_a = service.create_project(root_path=str(dir_a), name="PathA")
    register_checkout_project(dir_a, project_a)
    _init_git_with_remote(dir_a, remote_url)

    fetched = service.get_project(project_a["id"])
    assert fetched["git_remote"] is None
    assert fetched["project_source"] == "git_remote"
    assert fetched["git_scope_recheck"]["status"] == "blocked"
    assert fetched["git_scope_recheck"]["reason"] in {
        "project_registration_conflict",
        "project_scope_conflict",
    }
    assert project_b["id"] in fetched["git_scope_recheck"]["conflicting_project_ids"]

    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT git_remote FROM projects WHERE id = ?", (project_a["id"],)
        ).fetchone()
    assert row["git_remote"] is None
    events = service.list_events(project_a["id"], after=0)["events"]
    assert not [e for e in events if e["event_type"] == "project.updated"]


def test_get_project_without_git_opens_no_write_connection(
    test_service, tmp_path, monkeypatch
):
    """#172：纯路径候选读取不打开写连接、不跑 git 自愈写路径。"""
    service = test_service
    project_dir = tmp_path / "plain_read"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="PlainRead")

    write_flags: list[bool] = []
    original_connect = service.database.connect

    def spy_connect(write: bool = False, **kwargs):
        write_flags.append(write)
        return original_connect(write=write, **kwargs)

    monkeypatch.setattr(service.database, "connect", spy_connect)
    fetched = service.get_project(project["id"])
    assert fetched["project_source"] == "path"
    assert fetched["git_remote"] is None
    assert write_flags
    assert not any(write_flags)


def test_list_projects_ttl_avoids_repeat_git_probe(
    test_service, tmp_path, monkeypatch
):
    """#172：列表读取对纯路径候选用 TTL 抑制重复 git 子进程。"""
    service = test_service
    project_dir = tmp_path / "list_ttl"
    project_dir.mkdir()
    service.create_project(root_path=str(project_dir), name="ListTtl")

    probe_calls = {"n": 0}
    original_git_info = __import__(
        "agentchatroom.services", fromlist=["_project_git_info"]
    )._project_git_info

    def counting_git_info(root):
        probe_calls["n"] += 1
        return original_git_info(root)

    monkeypatch.setattr(
        "agentchatroom.services._project_git_info", counting_git_info
    )
    first = service.list_projects()
    second = service.list_projects()
    assert first[0]["project_source"] == "path"
    assert second[0]["project_source"] == "path"
    assert probe_calls["n"] == 1


def test_get_project_detail_ttl_avoids_repeat_git_probe(
    test_service, tmp_path, monkeypatch
):
    """#172：详情 GET 对纯路径候选同样走 TTL，不反复跑 git 子进程。"""
    service = test_service
    project_dir = tmp_path / "detail_ttl"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="DetailTtl")

    probe_calls = {"n": 0}
    original_git_info = __import__(
        "agentchatroom.services", fromlist=["_project_git_info"]
    )._project_git_info

    def counting_git_info(root):
        probe_calls["n"] += 1
        return original_git_info(root)

    monkeypatch.setattr(
        "agentchatroom.services._project_git_info", counting_git_info
    )
    first = service.get_project(project["id"])
    second = service.get_project(project["id"])
    third = service.get_project(project["id"])
    assert first["project_source"] == "path"
    assert second["project_source"] == "path"
    assert third["project_source"] == "path"
    assert probe_calls["n"] == 1, (
        "detail GET must reuse the TTL cache and not re-run git each read"
    )
