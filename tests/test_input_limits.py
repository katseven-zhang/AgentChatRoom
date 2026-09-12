"""Review batch: configurable input size limits (message/task text)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentchatroom.errors import DomainError
from agentchatroom.config import load_settings
from agentchatroom.services import AgentChatRoomService
from agentchatroom.database import Database


@pytest.fixture()
def service(settings):
    instance = AgentChatRoomService(
        Database(settings.database_path), settings
    )
    instance.initialize()
    return instance


@pytest.fixture()
def project(service, tmp_path):
    root = tmp_path / "limits-project"
    root.mkdir()
    return service.create_project(root_path=str(root), name="Limits")


def test_message_body_over_default_limit_is_rejected(service, project):
    """默认 65536 上限：超长正文被结构化拒绝并指明上限。"""
    sender = service.join_room(
        project["id"], name="Sender", client="codex", model="unknown"
    )
    oversized = "x" * (service.settings.message_max_body_length + 1)
    with pytest.raises(DomainError) as excinfo:
        service.post_message(
            project["id"],
            body=oversized,
            session_id=sender["agent"]["id"],
            token=sender["token"],
            model_display_name="test",
        )
    assert excinfo.value.code == "message_body_too_long"
    assert str(service.settings.message_max_body_length) in str(
        excinfo.value.details
    )


def test_task_text_over_default_limit_is_rejected(service, project):
    """任务 title/description/单条验收标准超限被结构化拒绝。"""
    limit = service.settings.task_text_max_length
    with pytest.raises(DomainError) as title_error:
        service.create_task(
            project["id"],
            title="y" * (limit + 1),
            acceptance_criteria=["criterion"],
        )
    assert title_error.value.code == "task_text_too_long"
    with pytest.raises(DomainError) as description_error:
        service.create_task(
            project["id"],
            title="ok",
            description="z" * (limit + 1),
            acceptance_criteria=["criterion"],
        )
    assert description_error.value.code == "task_text_too_long"
    with pytest.raises(DomainError) as criterion_error:
        service.create_task(
            project["id"],
            title="ok",
            acceptance_criteria=["c" * (limit + 1)],
        )
    assert criterion_error.value.code == "task_text_too_long"


def test_message_and_task_within_limits_are_accepted(service, project):
    sender = service.join_room(
        project["id"], name="Sender", client="codex", model="unknown"
    )
    body = "x" * 1000
    posted = service.post_message(
        project["id"],
        body=body,
        session_id=sender["agent"]["id"],
        token=sender["token"],
        model_display_name="test",
    )
    assert posted["event_id"]
    task = service.create_task(
        project["id"],
        title="Normal task",
        description="d" * 1000,
        acceptance_criteria=["criterion " + "c" * 100],
    )["task"]
    assert task["title"] == "Normal task"


def test_config_overrides_and_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MESSAGE_MAX_BODY_LENGTH", "1000")
    monkeypatch.setenv("AGENTCHATROOM_TASK_TEXT_MAX_LENGTH", "500")
    tuned = load_settings()
    assert tuned.message_max_body_length == 1000
    assert tuned.task_text_max_length == 500

    monkeypatch.setenv("AGENTCHATROOM_MESSAGE_MAX_BODY_LENGTH", "0")
    with pytest.raises(ValueError, match="message_max_body_length must be positive"):
        load_settings()
    monkeypatch.setenv("AGENTCHATROOM_MESSAGE_MAX_BODY_LENGTH", "1000")
    monkeypatch.setenv("AGENTCHATROOM_TASK_TEXT_MAX_LENGTH", "-1")
    with pytest.raises(ValueError, match="task_text_max_length must be positive"):
        load_settings()


def test_small_limit_actually_bounds_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MESSAGE_MAX_BODY_LENGTH", "50")
    settings = load_settings()
    assert settings.message_max_body_length == 50
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()
    root = tmp_path / "proj"
    root.mkdir()
    project = service.create_project(root_path=str(root), name="Small")
    sender = service.join_room(
        project["id"], name="Sender", client="codex", model="unknown"
    )
    with pytest.raises(DomainError) as excinfo:
        service.post_message(
            project["id"],
            body="x" * 51,
            session_id=sender["agent"]["id"],
            token=sender["token"],
            model_display_name="test",
        )
    assert excinfo.value.code == "message_body_too_long"
    posted = service.post_message(
        project["id"],
        body="x" * 50,
        session_id=sender["agent"]["id"],
        token=sender["token"],
        model_display_name="test",
    )
    assert posted["event_id"]
