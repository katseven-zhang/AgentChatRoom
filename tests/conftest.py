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


@pytest.fixture()
def joined_agents(service, project):
    executor = service.join_room(
        project["id"],
        agent_key="builder-main",
        name="Builder",
        client="codex",
        model="test-model",
        role="executor",
    )
    reviewer = service.join_room(
        project["id"],
        agent_key="reviewer-main",
        name="Reviewer",
        client="qoder",
        model="test-model",
        role="reviewer",
    )
    return executor, reviewer
