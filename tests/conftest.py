from __future__ import annotations

import pytest

from agentchatroom.config import Settings
from agentchatroom.database import Database
from agentchatroom.services import AgentChatRoomService


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
