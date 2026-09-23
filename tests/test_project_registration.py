from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentchatroom import mcp_server
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import (
    PROJECT_INSTRUCTIONS_BEGIN,
    PROJECT_INSTRUCTIONS_END,
    checkout_scope,
    derive_logical_path,
    load_checkout_registration,
    project_instructions_path,
    project_registration_path,
    register_checkout_project,
    remove_project_coordination_instructions,
    remove_checkout_project_registration,
    resolve_checkout_project_key,
    validate_logical_path,
    write_project_coordination_instructions,
)


def _configure_local_software(
    monkeypatch,
    *,
    key: str,
    name: str,
    client: str,
) -> None:
    monkeypatch.setenv(mcp_server.SOFTWARE_KEY_ENV, key)
    monkeypatch.setenv(mcp_server.SOFTWARE_NAME_ENV, name)
    monkeypatch.setenv(mcp_server.SOFTWARE_CLIENT_ENV, client)


def test_checkout_registration_persists_backend_generated_project_key(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir), name="Registered")
    path = register_checkout_project(project_dir, project)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert project["project_key"].startswith("prj_")
    assert "path:" not in project["project_key"]
    assert "workbuddy" not in project["project_key"].lower()
    assert document == {
        "schema_version": 1,
        "registrations": [
            {
                "logical_path": "",
                "project_key": project["project_key"],
                "scope": {
                    "kind": "path",
                    "identity": os.path.normcase(str(project_dir.resolve())),
                    "logical_path": "",
                },
            }
        ],
    }
    serialized = path.read_text(encoding="utf-8")
    assert project["id"] not in serialized
    assert "session" not in serialized.lower()
    assert "token" not in serialized.lower()


def test_checkout_registration_creates_managed_project_agents_instructions(
    service, project_dir
):
    project = service.create_project(
        root_path=str(project_dir),
        name="Project Alpha",
    )

    register_checkout_project(project_dir, project)

    path = project_instructions_path(project_dir)
    text = path.read_text(encoding="utf-8")
    assert text.count(PROJECT_INSTRUCTIONS_BEGIN) == 1
    assert text.count(PROJECT_INSTRUCTIONS_END) == 1
    assert "Project name: `Project Alpha`" in text
    assert 'room_bootstrap(project_name="Project Alpha")' in text
    assert "room_sync" in text
    assert "task_acknowledge" in text
    assert "message_post" in text
    assert "lease_acquire" in text
    assert "work_report" in text
    assert "review_submit" in text
    assert "Do not inspect AgentChatRoom source" in text
    assert project["id"] not in text
    assert project["project_key"] not in text
    assert "Bearer " not in text


# #135：托管规则必须覆盖“Agent 客户端对话选错项目工作空间”这一场景。
# 逐条短语对应任务验收标准：三事实核对、停止执行、明确提示与切换指引、
# 目标不明确先澄清、禁止重绑 Room/切换 Project/改工作目录绕过、Room 与错误
# 工作区匹配也不构成授权。
WORKSPACE_MISMATCH_RULE_PHRASES = (
    "the workspace this Agent conversation is actually running in",
    "the target project the user asked you to work on",
    "the Room Project this Session is bound to",
    "A matching Room binding never substitutes for a matching conversation workspace",
    "stop and do not execute that work in this conversation",
    "do not claim or acknowledge its tasks, do not acquire leases, and do not read or modify its files",
    "switch to the correct project workspace in the Agent client",
    "ask for clarification first; never guess a project identity",
    "Never resolve a workspace/project mismatch by rebinding the Room Session, switching the Room Project, changing the working directory, or operating on another directory",
    "not authorization to work on a different requested project",
)


def test_managed_instructions_stop_on_wrong_conversation_workspace(service, project_dir):
    project = service.create_project(root_path=str(project_dir), name="Project Alpha")

    register_checkout_project(project_dir, project)

    text = project_instructions_path(project_dir).read_text(encoding="utf-8")
    for phrase in WORKSPACE_MISMATCH_RULE_PHRASES:
        assert phrase in text
    # 工作空间核对必须排在 Room 接入之前，否则会先绑定再判断。
    assert text.index(WORKSPACE_MISMATCH_RULE_PHRASES[0]) < text.index(
        "call `room_bootstrap("
    )
    # 匹配时正常流程保留：规则不能退化成“任何情况都不做”。
    assert "`COORDINATE`" in text
    assert "task_claim" in text
    assert "work_report" in text
    # 不硬编码本机路径与具体项目身份。
    assert "C:\\" not in text
    assert "D:\\" not in text
    assert project["id"] not in text


def test_managed_instructions_keep_workspace_mismatch_rule_across_refresh(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir), name="Refresh One")

    first = write_project_coordination_instructions(project_dir, project)
    second = write_project_coordination_instructions(project_dir, project)
    renamed = write_project_coordination_instructions(
        project_dir, {**project, "name": "Refresh Two"}
    )

    text = project_instructions_path(project_dir).read_text(encoding="utf-8")
    assert first["action"] == "created"
    assert second["action"] == "unchanged"
    assert renamed["action"] == "updated"
    assert text.count(PROJECT_INSTRUCTIONS_BEGIN) == 1
    assert "Refresh Two" in text
    assert "Refresh One" not in text
    for phrase in WORKSPACE_MISMATCH_RULE_PHRASES:
        assert phrase in text


# #136/#205：托管规则必须说明恢复优先、显式同身份在线接管及禁止越权。
RECOVERY_RULE_PHRASES = (
    "`connection.room_session`",
    "`restored` means the same Room Session and unchanged task ownership",
    "a new Session that inherits no task",
    "Never fabricate the previous binding",
    "`runtime_context_mismatch`",
    "bootstrap itself is not proof that task ownership was recovered",
    "`task_claim(reclaim=true)` succeeds after the same-identity owner is treated as disconnected",
    "`task_claim(takeover=true)` as an explicit, audited alternative",
    "Only use live takeover when the user has directed this conversation",
    "`task_owner_session_connected`",
    "Never bypass ownership with `task_update(status=todo)`",
    "A missing Session credential is not management authority",
    "`management_auth_required`",
)


def test_managed_instructions_document_recovery_and_authority_rules(
    service, project_dir
):
    project = service.create_project(root_path=str(project_dir), name="Recovery")

    register_checkout_project(project_dir, project)

    text = project_instructions_path(project_dir).read_text(encoding="utf-8")
    for phrase in RECOVERY_RULE_PHRASES:
        assert phrase in text
    # 恢复语义必须排在“打断/新开会话”之后仍沿用普通任务流程，不能取消正常流程。
    assert "room_bootstrap" in text
    assert "task_claim" in text


def test_repository_agents_md_carries_recovery_and_authority_rules():
    text = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    managed = text[
        text.index(PROJECT_INSTRUCTIONS_BEGIN) : text.index(PROJECT_INSTRUCTIONS_END)
    ]
    for phrase in RECOVERY_RULE_PHRASES:
        assert phrase in managed


def test_repository_agents_md_carries_workspace_mismatch_stop_rule():
    """本仓库自带的托管区块（Agent 实际读取的规则）必须与生成源一致。"""
    text = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text(
        encoding="utf-8"
    )

    managed = text[
        text.index(PROJECT_INSTRUCTIONS_BEGIN) : text.index(PROJECT_INSTRUCTIONS_END)
    ]
    for phrase in WORKSPACE_MISMATCH_RULE_PHRASES:
        assert phrase in managed


def test_managed_project_agents_instructions_preserve_user_rules_and_update_name(
    service, project_dir
):
    path = project_instructions_path(project_dir)
    path.write_text("# User rules\n\n- Keep this line.\n", encoding="utf-8")
    project = service.create_project(root_path=str(project_dir), name="First Name")

    first = write_project_coordination_instructions(project_dir, project)
    second = write_project_coordination_instructions(project_dir, project)
    renamed = {**project, "name": "Second Name"}
    third = write_project_coordination_instructions(project_dir, renamed)

    text = path.read_text(encoding="utf-8")
    assert first["action"] == "appended"
    assert second["action"] == "unchanged"
    assert third["action"] == "updated"
    assert text.startswith("# User rules\n\n- Keep this line.\n")
    assert text.count(PROJECT_INSTRUCTIONS_BEGIN) == 1
    assert "Second Name" in text
    assert "First Name" not in text


def test_managed_project_agents_instructions_reject_broken_markers(
    service, project_dir
):
    path = project_instructions_path(project_dir)
    path.write_text(f"# User rules\n{PROJECT_INSTRUCTIONS_BEGIN}\n", encoding="utf-8")
    project = service.create_project(root_path=str(project_dir), name="Broken")

    with pytest.raises(DomainError) as invalid:
        write_project_coordination_instructions(project_dir, project)

    assert invalid.value.code == "project_instructions_invalid"
    assert path.read_text(encoding="utf-8").startswith("# User rules")


def test_managed_project_agents_instructions_keep_exact_unusual_project_name(
    service, project_dir
):
    name = f"Project  {PROJECT_INSTRUCTIONS_END}"
    project = service.create_project(root_path=str(project_dir), name=name)

    first = write_project_coordination_instructions(project_dir, project)
    second = write_project_coordination_instructions(project_dir, project)

    text = project_instructions_path(project_dir).read_text(encoding="utf-8")
    assert first["project_name"] == name
    assert second["action"] == "unchanged"
    assert f"room_bootstrap(project_name={json.dumps(name)})" in text
    assert text.count(f"\n{PROJECT_INSTRUCTIONS_END}\n") == 1


def test_remove_managed_project_agents_instructions_preserves_user_rules(
    service, project_dir
):
    path = project_instructions_path(project_dir)
    path.write_text("# User rules\n\n- Keep this line.\n", encoding="utf-8")
    project = service.create_project(root_path=str(project_dir), name="Temporary")
    write_project_coordination_instructions(project_dir, project)

    assert remove_project_coordination_instructions(project_dir) is True
    assert path.read_text(encoding="utf-8") == "# User rules\n\n- Keep this line.\n"
    assert remove_project_coordination_instructions(project_dir) is False


def test_checkout_scope_is_detected_by_backend(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert checkout_scope(plain)["kind"] == "path"

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", "git@example.invalid:team/repo.git"],
        check=True,
        capture_output=True,
    )
    scope = checkout_scope(repository)
    assert scope == {
        "kind": "git",
        "identity": "https://example.invalid/team/repo",
        "logical_path": "",
    }

    api = repository / "packages" / "api"
    api.mkdir(parents=True)
    assert checkout_scope(api) == {
        "kind": "git",
        "identity": "https://example.invalid/team/repo",
        "logical_path": "packages/api",
    }


@pytest.mark.parametrize(
    "logical_path",
    [r"D:\\claw\\agentchatroom", "/srv/agentchatroom", "../agentchatroom"],
)
def test_logical_path_must_be_repository_relative(project_dir, logical_path):
    with pytest.raises(DomainError) as invalid:
        checkout_scope(project_dir, logical_path=logical_path)

    assert invalid.value.code == "invalid_logical_path"


def test_logical_path_normalizes_safe_repository_relative_paths():
    assert validate_logical_path(r"packages\\api") == "packages/api"
    assert validate_logical_path(".") == ""
    assert validate_logical_path("Packages/MyLib") == "Packages/MyLib"


def test_logical_paths_equal_is_case_insensitive_across_platforms():
    from agentchatroom.project_registration import logical_paths_equal

    assert logical_paths_equal("Packages/MyLib", "packages/mylib")
    assert not logical_paths_equal("Packages/MyLib", "packages/other")


def test_logical_path_must_match_actual_project_directory(tmp_path):
    repository = tmp_path / "repository"
    api = repository / "packages" / "api"
    api.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)

    expected = "packages/api"
    assert derive_logical_path(api, repository) == expected
    with pytest.raises(DomainError) as mismatch:
        derive_logical_path(api, repository, "packages/web")
    assert mismatch.value.code == "invalid_logical_path"


def test_git_info_decodes_git_output_as_utf8_independent_of_locale(
    tmp_path, monkeypatch
):
    """Reported regression: Git for Windows writes UTF-8 path bytes to pipes.

    Decoding with the process locale (cp936 on Chinese Windows) turned the
    repository toplevel into mojibake, so ``derive_logical_path`` rejected the
    very directory the user selected with ``project_scope_conflict``.
    """
    from agentchatroom import project_registration

    project_root = tmp_path / "赞助管理工具"
    project_root.mkdir()
    toplevel_bytes = str(project_root).replace("\\", "/").encode("utf-8")
    remote_bytes = "git@example.invalid:team/赞助管理工具.git".encode("utf-8")

    def fake_run(command, **_kwargs):
        arguments = list(command)
        if "rev-parse" in arguments:
            stdout: bytes = toplevel_bytes
        elif "config" in arguments:
            stdout = remote_bytes
        else:
            stdout = b""
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(project_registration.subprocess, "run", fake_run)

    remote, git_root, is_work_tree = project_registration._git_info(project_root)

    assert remote == "git@example.invalid:team/赞助管理工具.git"
    assert git_root == project_root.resolve()
    assert is_work_tree is True
    assert checkout_scope(project_root) == {
        "kind": "git",
        "identity": "https://example.invalid/team/赞助管理工具",
        "logical_path": "",
    }


def test_service_project_git_info_shares_utf8_decoding(tmp_path, monkeypatch):
    """The web "添加项目" flow goes through services._project_git_info."""
    from agentchatroom import project_registration, services

    project_root = tmp_path / "赞助管理工具"
    project_root.mkdir()
    toplevel_bytes = str(project_root).replace("\\", "/").encode("utf-8")

    def fake_run(command, **_kwargs):
        arguments = list(command)
        stdout = toplevel_bytes if "rev-parse" in arguments else b""
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=stdout, stderr=b""
        )

    monkeypatch.setattr(project_registration.subprocess, "run", fake_run)

    remote, git_root, is_work_tree = services._project_git_info(project_root)

    assert remote == ""
    assert git_root == project_root.resolve()
    assert is_work_tree is True
    assert derive_logical_path(project_root, git_root) == ""


def test_checkout_scope_accepts_unicode_named_repository(tmp_path):
    project_root = tmp_path / "赞助管理工具"
    project_root.mkdir()
    subprocess.run(["git", "-C", str(project_root), "init"], check=True, capture_output=True)

    scope = checkout_scope(project_root)

    assert scope["kind"] == "path"
    assert scope["logical_path"] == ""
    assert Path(scope["identity"].replace("\\", "/")).resolve() == project_root.resolve()


def test_checkout_scope_accepts_unicode_repository_subdirectory(tmp_path):
    repository = tmp_path / "赞助管理工具"
    api = repository / "packages" / "接口"
    api.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)

    assert checkout_scope(api) == {
        "kind": "path",
        "identity": os.path.normcase(str(repository.resolve())),
        "logical_path": "packages/接口",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive path input")
def test_checkout_scope_normalizes_windows_case_variant_input(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)

    variant = str(repository).swapcase()
    assert Path(variant).resolve() == repository.resolve()
    assert checkout_scope(variant) == checkout_scope(repository)


def test_checkout_registration_rejects_a_file_copied_from_another_scope(
    service, tmp_path
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    project = service.create_project(root_path=str(first))
    source = register_checkout_project(first, project)
    target = project_registration_path(second)
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(DomainError) as conflict:
        resolve_checkout_project_key(second)

    assert conflict.value.code == "project_registration_scope_conflict"


def test_load_document_rejects_case_duplicate_logical_paths_both_orders(tmp_path):
    """#198: foo/Foo must collide regardless of registration order."""
    from agentchatroom.project_registration import _load_document

    def write_doc(path, first, second):
        path.write_text(
            __import__("json").dumps(
                {
                    "schema_version": 1,
                    "registrations": [
                        {
                            "logical_path": first,
                            "project_key": "key-a",
                            "scope": {
                                "kind": "path",
                                "identity": "id-a",
                                "logical_path": first,
                            },
                        },
                        {
                            "logical_path": second,
                            "project_key": "key-b",
                            "scope": {
                                "kind": "path",
                                "identity": "id-b",
                                "logical_path": second,
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    foo_first = tmp_path / "foo_first.json"
    write_doc(foo_first, "foo", "Foo")
    with pytest.raises(DomainError) as a:
        _load_document(foo_first)
    assert a.value.code == "project_registration_invalid"

    Foo_first = tmp_path / "Foo_first.json"
    write_doc(Foo_first, "Foo", "foo")
    with pytest.raises(DomainError) as b:
        _load_document(Foo_first)
    assert b.value.code == "project_registration_invalid"


def test_checkout_registration_supports_derived_logical_subprojects_and_removal(
    service, tmp_path
):
    repository = tmp_path / "repository"
    api_dir = repository / "packages" / "api"
    web_dir = repository / "packages" / "web"
    api_dir.mkdir(parents=True)
    web_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            "git@example.invalid:team/repo.git",
        ],
        check=True,
        capture_output=True,
    )
    api = service.create_project(root_path=str(api_dir))
    web = service.create_project(root_path=str(web_dir))
    register_checkout_project(api_dir, api)
    register_checkout_project(web_dir, web)

    api_logical = "packages/api"
    web_logical = "packages/web"
    assert api["project_key"] != web["project_key"]
    assert api["logical_path"] == api_logical
    assert web["logical_path"] == web_logical
    assert load_checkout_registration(api_dir, logical_path=api_logical)[
        "project_key"
    ] == api["project_key"]
    assert remove_checkout_project_registration(
        api_dir,
        project_key=api["project_key"],
        logical_path=api_logical,
    )
    assert load_checkout_registration(api_dir, logical_path=api_logical) is None
    assert load_checkout_registration(web_dir, logical_path=web_logical) is not None


def test_local_mcp_join_creates_then_joins_one_room(monkeypatch, service, project_dir):
    monkeypatch.setattr(mcp_server, "service", service)
    _configure_local_software(
        monkeypatch,
        key="generic-client",
        name="Generic Client",
        client="generic-client",
    )

    first = mcp_server.room_join(
        project_path=str(project_dir), model="unknown", agent_key="first-main",
        agent_name="First Agent", client="generic-client"
    )
    second = mcp_server.room_join(
        project_path=str(project_dir), model="unknown", agent_key="second-main",
        agent_name="Second Agent", client="generic-client"
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["result"]["project"]["id"] == first["result"]["project"]["id"]
    assert len(service.list_projects()) == 1
    registration = load_checkout_registration(project_dir)
    assert registration["project_key"] == first["result"]["project"]["project_key"]


def test_local_mcp_join_derives_monorepo_subproject_from_project_path(
    monkeypatch, service, tmp_path
):
    repository = tmp_path / "repository"
    api_dir = repository / "packages" / "api"
    api_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            "git@example.invalid:team/repo.git",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(mcp_server, "service", service)
    _configure_local_software(
        monkeypatch,
        key="generic-client",
        name="Generic Client",
        client="generic-client",
    )

    root_join = mcp_server.room_join(
        project_path=str(repository), model="unknown", agent_key="root-main",
        agent_name="Root Agent", client="generic-client"
    )
    api_join = mcp_server.room_join(
        project_path=str(api_dir), model="unknown", agent_key="api-main",
        agent_name="API Agent", client="generic-client"
    )
    api_repeat = mcp_server.room_join(
        project_path=str(api_dir), model="unknown", agent_key="api-review",
        agent_name="API Reviewer", client="generic-client"
    )

    expected = "packages/api"
    assert root_join["result"]["project"]["logical_path"] == ""
    assert api_join["result"]["project"]["logical_path"] == expected
    assert api_repeat["result"]["project"]["id"] == api_join["result"]["project"]["id"]
    assert api_join["result"]["project"]["id"] != root_join["result"]["project"]["id"]


def test_orphaned_checkout_registration_does_not_recreate_deleted_project(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    _configure_local_software(
        monkeypatch, key="trae", name="Trae", client="trae"
    )
    project = service.create_project(root_path=str(project_dir))
    register_checkout_project(project_dir, project)
    service.delete_project(project["id"])

    response = mcp_server.room_join(
        project_path=str(project_dir), model="unknown", agent_key="trae-main",
        agent_name="Trae", client="trae"
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "project_registration_orphaned"
    assert service.list_projects() == []


def test_backend_refreshes_registration_after_project_key_migration(
    monkeypatch, service, project_dir
):
    monkeypatch.setattr(mcp_server, "service", service)
    _configure_local_software(
        monkeypatch, key="codex", name="Codex", client="codex"
    )
    project = service.create_project(root_path=str(project_dir))
    path = register_checkout_project(project_dir, project)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["registrations"][0]["project_key"] = "legacy-agent-named-key"
    path.write_text(json.dumps(document), encoding="utf-8")

    joined = mcp_server.room_join(
        project_path=str(project_dir), model="unknown", agent_key="codex-main",
        agent_name="Codex", client="codex"
    )

    assert joined["ok"] is True
    assert joined["result"]["project"]["id"] == project["id"]
    assert load_checkout_registration(project_dir)["project_key"] == project["project_key"]


def test_mcp_schema_does_not_accept_agent_supplied_project_identity():
    room_join = mcp_server.mcp._tool_manager.get_tool("room_join")

    assert "Never supply, infer, or replace a project_key" in mcp_server.MCP_INSTRUCTIONS
    assert ".agentchatroom/project.json" in mcp_server.MCP_INSTRUCTIONS
    assert "project_key" not in room_join.parameters["properties"]
    assert "logical_path" not in room_join.parameters["properties"]
    assert "Agents also do not supply logical_path" in mcp_server.MCP_INSTRUCTIONS
