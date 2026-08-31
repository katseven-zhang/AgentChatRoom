from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentchatroom.config import Settings
from agentchatroom.errors import DomainError
from agentchatroom.integrations import build_mcp_integration
from agentchatroom.local_mcp import LocalMcpConfigurator, LocalMcpEnvironment


def _profile(tmp_path, profile_id):
    project_path = tmp_path / "project"
    project_path.mkdir(exist_ok=True)
    integration = build_mcp_integration(
        Settings(data_dir=tmp_path / "runtime"),
        python_executable=tmp_path / "venv" / "python.exe",
        project={"name": "Example", "root_path": str(project_path)},
    )
    return integration["profiles"][profile_id]


def _configurator(tmp_path, **kwargs):
    return LocalMcpConfigurator(
        LocalMcpEnvironment(
            home_dir=tmp_path / "home",
            appdata_dir=tmp_path / "appdata",
        ),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("profile_id", "relative_path", "detected_profile"),
    [
        ("workbuddy", ("home", ".workbuddy", "mcp.json"), "WorkBuddy"),
        (
            "trae",
            ("appdata", "TRAE SOLO CN", "User", "mcp.json"),
            "TRAE SOLO CN",
        ),
    ],
)
def test_local_mcp_plan_detects_verified_client_files(
    tmp_path, profile_id, relative_path, detected_profile
):
    config_path = tmp_path.joinpath(*relative_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {"existing": {"command": "keep-me"}},
                "clientSetting": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    plan = _configurator(tmp_path).plan(
        deployment_profile="local",
        profile_id=profile_id,
        profile=_profile(tmp_path, profile_id),
    )

    assert plan["state"] == "unconfigured"
    assert plan["mode"] == "managed_write"
    assert plan["managed_apply_available"] is True
    assert plan["config_path"] == str(config_path.resolve())
    assert plan["detected_profile"] == detected_profile
    assert plan["changed_fields"] == ["mcpServers.agentchatroom"]
    assert plan["connection_status"] == "not_verified"


def test_local_mcp_apply_preserves_other_servers_and_creates_exact_backup(tmp_path):
    config_path = tmp_path / "home" / ".workbuddy" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    original = (
        json.dumps(
            {
                "mcpServers": {
                    "existing": {
                        "command": "keep-me",
                        "env": {"EXISTING_SECRET": "not-returned-by-plan"},
                    },
                    "agentchatroom": {
                        "command": "old-python",
                        "args": [],
                        "disabled": False,
                        "env": {"CLIENT_FLAG": "keep-me"},
                    },
                },
                "clientSetting": {"enabled": True},
            },
            indent=4,
        )
        + "\n"
    ).encode("utf-8")
    config_path.write_bytes(original)
    configurator = _configurator(
        tmp_path,
        now=lambda: datetime(2026, 8, 30, 12, 34, 56, tzinfo=timezone.utc),
    )
    profile = _profile(tmp_path, "workbuddy")
    plan = configurator.plan(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
    )
    assert "not-returned-by-plan" not in json.dumps(plan)

    result = configurator.apply(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
        expected_current_sha256=plan["current_sha256"],
    )

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    expected_server = profile["config"]["mcpServers"]["agentchatroom"]
    assert updated["mcpServers"]["agentchatroom"]["command"] == expected_server[
        "command"
    ]
    assert updated["mcpServers"]["agentchatroom"]["args"] == expected_server["args"]
    assert updated["mcpServers"]["agentchatroom"]["disabled"] is False
    assert updated["mcpServers"]["agentchatroom"]["env"]["CLIENT_FLAG"] == (
        "keep-me"
    )
    for key, value in expected_server["env"].items():
        assert updated["mcpServers"]["agentchatroom"]["env"][key] == value
    assert updated["mcpServers"]["existing"] == {
        "command": "keep-me",
        "env": {"EXISTING_SECRET": "not-returned-by-plan"},
    }
    assert updated["clientSetting"] == {"enabled": True}
    backup_path = config_path.with_name(
        "mcp.json.agentchatroom-backup-20260830-123456-000000"
    )
    assert result["backup_path"] == str(backup_path)
    assert backup_path.read_bytes() == original
    assert result["reload_required"] is True
    assert result["plan"]["state"] == "current"
    assert not list(config_path.parent.glob("*.tmp"))


def test_local_mcp_plan_uses_an_existing_standard_trae_file_only(tmp_path):
    config_path = tmp_path / "appdata" / "Trae CN" / "User" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"mcpServers": {}}\n', encoding="utf-8")

    plan = _configurator(tmp_path).plan(
        deployment_profile="local",
        profile_id="trae",
        profile=_profile(tmp_path, "trae"),
    )

    assert plan["config_path"] == str(config_path.resolve())
    assert plan["detected_profile"] == "Trae CN"


def test_local_mcp_apply_rejects_a_concurrent_change(tmp_path):
    config_path = tmp_path / "home" / ".workbuddy" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    configurator = _configurator(tmp_path)
    profile = _profile(tmp_path, "workbuddy")
    plan = configurator.plan(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
    )
    config_path.write_text(
        '{"mcpServers": {}, "changedElsewhere": true}\n', encoding="utf-8"
    )

    with pytest.raises(DomainError) as captured:
        configurator.apply(
            deployment_profile="local",
            profile_id="workbuddy",
            profile=profile,
            expected_current_sha256=plan["current_sha256"],
        )

    assert captured.value.code == "local_mcp_config_changed"
    assert not list(config_path.parent.glob("*.agentchatroom-backup-*"))


def test_local_mcp_plan_degrades_without_guessing_or_elevation(tmp_path):
    configurator = _configurator(tmp_path)
    profile = _profile(tmp_path, "workbuddy")

    missing = configurator.plan(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
    )
    assert missing["state"] == "missing"
    assert missing["mode"] == "manual"
    assert missing["managed_apply_available"] is False

    config_path = tmp_path / "home" / ".workbuddy" / "mcp.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("not-json", encoding="utf-8")
    invalid = configurator.plan(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
    )
    assert invalid["state"] == "invalid"
    assert invalid["mode"] == "assisted"

    config_path.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    unwritable = _configurator(tmp_path, writable_check=lambda _path: False).plan(
        deployment_profile="local",
        profile_id="workbuddy",
        profile=profile,
    )
    assert unwritable["state"] == "unwritable"
    assert unwritable["configuration_state"] == "unconfigured"
    assert unwritable["mode"] == "assisted"
    assert unwritable["managed_apply_available"] is False

    remote = configurator.plan(
        deployment_profile="server",
        profile_id="workbuddy",
        profile=profile,
    )
    assert remote["state"] == "unavailable"
    assert remote["config_path"] is None


def test_local_mcp_plan_degrades_when_candidate_metadata_is_permission_denied(
    tmp_path, monkeypatch
):
    profile = _profile(tmp_path, "trae")
    original_is_file = Path.is_file

    def guarded_is_file(path):
        if path.name == "mcp.json":
            raise PermissionError("denied")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    plan = _configurator(tmp_path).plan(
        deployment_profile="local",
        profile_id="trae",
        profile=profile,
    )

    assert plan["state"] == "unreadable"
    assert plan["mode"] == "assisted"
    assert plan["detected_profile"] == "TRAE SOLO CN"
    assert "denied" not in json.dumps(plan)


def test_local_mcp_plan_rejects_unknown_or_non_json_profiles(tmp_path):
    configurator = _configurator(tmp_path)
    with pytest.raises(DomainError) as unknown:
        configurator.plan(
            deployment_profile="local",
            profile_id="unknown",
            profile=None,
        )
    assert unknown.value.code == "local_mcp_profile_unknown"

    with pytest.raises(DomainError) as unsupported:
        configurator.plan(
            deployment_profile="local",
            profile_id="codex",
            profile=_profile(tmp_path, "codex"),
        )
    assert unsupported.value.code == "local_mcp_profile_unsupported"
