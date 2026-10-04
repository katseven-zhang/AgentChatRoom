"""Task #258: an Agent token's lower bound belongs to configuration, not literals.

The 300-second floor was copied into three domain validators while the upper bound
came from Settings, and the error details published ``minimum: 300`` as if it were
configured. A deployment that wanted shorter-lived tokens could not express it, and
config.py's own check would then contradict the service.
"""

from __future__ import annotations

import pytest

from agentchatroom.config import Settings, load_settings
from agentchatroom.database import Database
from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


def _service(tmp_path, **overrides):
    settings = Settings(data_dir=tmp_path / f"data-{len(overrides)}-{id(overrides)}", **overrides)
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()
    project = service.create_project(root_path=str(tmp_path), name="TTL Room")
    return service, project


def test_a_deployment_can_lower_the_bound(tmp_path):
    service, project = _service(tmp_path, min_agent_token_ttl_seconds=60)

    issued = service.issue_agent_token(
        project["id"], name="Short lived", expires_in_seconds=60
    )
    assert issued["credential"]["expires_at"]

    with pytest.raises(DomainError) as error:
        service.issue_agent_token(
            project["id"], name="Too short", expires_in_seconds=30
        )
    assert error.value.code == "invalid_agent_token_ttl"
    assert error.value.details["minimum"] == 60


def test_the_default_bound_still_rejects_sixty_seconds(service, project):
    with pytest.raises(DomainError) as error:
        service.issue_agent_token(
            project["id"], name="Below default floor", expires_in_seconds=60
        )

    assert error.value.code == "invalid_agent_token_ttl"
    assert error.value.details["minimum"] == service.min_agent_token_ttl_seconds() == 300


def test_rotate_and_extend_read_the_same_configured_bound(tmp_path):
    service, project = _service(tmp_path, min_agent_token_ttl_seconds=60)
    issued = service.issue_agent_token(
        project["id"], name="Extendable", expires_in_seconds=120
    )
    credential_id = issued["credential"]["id"]

    extended = service.extend_agent_token(
        project["id"], credential_id, extend_by_seconds=60
    )
    assert extended["credential"]["id"] == credential_id

    with pytest.raises(DomainError) as error:
        service.extend_agent_token(
            project["id"], credential_id, extend_by_seconds=30
        )
    assert error.value.details["minimum"] == 60


def test_the_minimum_is_a_validated_setting_with_env_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_MIN_AGENT_TOKEN_TTL_SECONDS", "45")
    assert (
        load_settings(data_dir=tmp_path / "env").min_agent_token_ttl_seconds == 45
    )

    monkeypatch.setenv("AGENTCHATROOM_MIN_AGENT_TOKEN_TTL_SECONDS", "0")
    with pytest.raises(ValueError, match="minimum Agent token TTL"):
        load_settings(data_dir=tmp_path / "zero")

    monkeypatch.delenv("AGENTCHATROOM_MIN_AGENT_TOKEN_TTL_SECONDS", raising=False)
    inverted = tmp_path / "inverted.toml"
    inverted.write_text(
        "[security]\n"
        "min_agent_token_ttl_seconds = 10000\n"
        "agent_token_ttl_seconds = 3600\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="min_agent_token_ttl_seconds"):
        load_settings(inverted, data_dir=tmp_path / "inverted-data")
