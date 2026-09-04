from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentchatroom.config import Settings
from agentchatroom.database import Database
from agentchatroom.services import AgentChatRoomService

_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
_REGISTRATION_WARNED = False


def _warn_if_tempdir_inside_checkout(config: pytest.Config) -> None:
    """pytest 的临时目录（--basetemp / tmp_path）必须放在本 checkout 之外。

    临时目录位于 checkout 内时，room_bootstrap 的工作区解析会从测试
    临时目录向上查找 .agentchatroom/project.json 并命中仓库自身的登记
    文件，使 test_bootstrap / test_project_registration 中依赖
    project_not_registered / 空 checkout 作用域的用例误报
    registration_invalid 或读取到真实 project_key。这里只提示，不改变
    任何测试行为。
    """
    global _REGISTRATION_WARNED
    if _REGISTRATION_WARNED:
        return
    _REGISTRATION_WARNED = True
    basetemp = config.getoption("--basetemp", "")
    basetemp_path = Path(basetemp).resolve() if basetemp else None
    if basetemp_path is None:
        return
    try:
        basetemp_path.relative_to(_CHECKOUT_ROOT)
    except ValueError:
        return
    print(
        "\n[agentchatroom 测试提示] pytest 临时目录（--basetemp）位于本 checkout 内："
        f"{basetemp_path}。bootstrap 的工作区解析会向上查找 .agentchatroom/project.json "
        "并命中仓库自身登记，test_bootstrap / test_project_registration 会因此误报 "
        "registration_invalid。请改用 checkout 之外的 --basetemp（例如系统临时目录或 "
        "仓库外的专用目录）后重跑。\n",
        file=sys.stderr,
    )


def pytest_configure(config: pytest.Config) -> None:
    _warn_if_tempdir_inside_checkout(config)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        heartbeat_timeout_seconds=30,
        session_token_ttl_seconds=3600,
        default_lease_ttl_seconds=300,
        max_lease_ttl_seconds=3600,
        sse_poll_interval_seconds=0.01,
    )


@pytest.fixture()
def service(settings):
    instance = AgentChatRoomService(Database(settings.database_path), settings)
    instance.initialize()
    return instance


@pytest.fixture()
def project_dir(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture()
def project(service, project_dir):
    return service.create_project(root_path=str(project_dir), name="Test Project")


def bind_workspace(service, project):
    return service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )


def join_room_with_workspace(service, project, **kwargs):
    registered = bind_workspace(service, project)
    kwargs.setdefault("worktree", project["root_path"])
    kwargs.setdefault("host_id", registered["host"]["id"])
    kwargs.setdefault("workspace_id", registered["workspace"]["id"])
    return service.join_room(project["id"], **kwargs)


@pytest.fixture()
def joined_agents(service, project):
    executor = join_room_with_workspace(
        service,
        project,
        agent_key="builder-main",
        name="Builder",
        client="codex",
        model="test-model",
        role="executor",
    )
    reviewer = join_room_with_workspace(
        service,
        project,
        agent_key="reviewer-main",
        name="Reviewer",
        client="qoder",
        model="test-model",
        role="reviewer",
    )
    return executor, reviewer
