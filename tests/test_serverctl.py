from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("agentchatroom_serverctl", ROOT / "deploy" / "serverctl.py")
assert SPEC and SPEC.loader
serverctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = serverctl
SPEC.loader.exec_module(serverctl)


def _paths(tmp_path: Path) -> serverctl.DeploymentPaths:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").write_text(
        "POSTGRES_DB=room\n"
        "POSTGRES_USER=room\n"
        "POSTGRES_PASSWORD=not-a-real-secret\n"
        "AGENTCHATROOM_DATABASE_URL=postgresql://room:secret@postgres:5432/room\n"
        "AGENTCHATROOM_ADMIN_TOKEN=admin-token-with-more-than-24-characters\n"
        "AGENTCHATROOM_EXTERNAL_BASE_URL=https://room.internal\n",
        encoding="utf-8",
    )
    (deploy / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (deploy / "config.server.toml").write_text("[application]\n", encoding="utf-8")
    return serverctl.resolve_paths(repo_root=tmp_path)


def test_preflight_is_redacted_and_detects_placeholders(tmp_path):
    paths = _paths(tmp_path)
    result = serverctl.deployment_preflight(paths)
    assert result["valid"] is True
    assert result["environment"]["AGENTCHATROOM_ADMIN_TOKEN"] == "configured"
    assert "admin-token" not in json.dumps(result)
    paths.env_file.write_text(
        paths.env_file.read_text(encoding="utf-8").replace(
            "https://room.internal", "https://room.example.com"
        ),
        encoding="utf-8",
    )
    result = serverctl.deployment_preflight(paths)
    assert result["valid"] is False
    assert "AGENTCHATROOM_EXTERNAL_BASE_URL" in result["missing_or_placeholder"]


def test_init_creates_examples_without_overwriting(tmp_path):
    paths = _paths(tmp_path)
    paths.env_file.unlink()
    paths.config_file.unlink()
    paths.example_env_file.write_text("PLACEHOLDER=one\n", encoding="utf-8")
    paths.example_config_file.write_text("[application]\n", encoding="utf-8")
    paths.example_proxy_file.write_text(
        "room.example.com { reverse_proxy room:8765 { flush_interval -1 } }\n",
        encoding="utf-8",
    )

    result = serverctl.init_deployment(paths)
    assert len(result["created"]) == 3
    paths.env_file.write_text("KEEP=me\n", encoding="utf-8")
    second = serverctl.init_deployment(paths)
    assert str(paths.env_file) in second["existing"]
    assert paths.env_file.read_text(encoding="utf-8") == "KEEP=me\n"


def test_proxy_preflight_checks_streaming_and_public_host(tmp_path):
    paths = _paths(tmp_path)
    paths.proxy_file.write_text(
        "room.internal {\n"
        "    reverse_proxy room:8765 {\n"
        "        flush_interval -1\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    result = serverctl._validate_proxy_config(
        paths, {"AGENTCHATROOM_EXTERNAL_BASE_URL": "https://room.internal"}
    )
    assert result["configured"] is True
    paths.proxy_file.write_text("room.internal { reverse_proxy room:8765 }\n", encoding="utf-8")
    with pytest.raises(serverctl.DeploymentError, match="flush_interval"):
        serverctl._validate_proxy_config(
            paths, {"AGENTCHATROOM_EXTERNAL_BASE_URL": "https://room.internal"}
        )


def test_up_and_backup_commands_use_configured_paths(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(serverctl.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(serverctl, "_validate_application_config", lambda *args: {"valid": True})
    monkeypatch.setattr(
        serverctl,
        "_run",
        lambda command, *, cwd, capture=False: calls.append(tuple(command))
        or serverctl.CommandResult(tuple(command), 0),
    )

    assert serverctl.run_action("up", paths, engine="docker")["ok"] is True
    serverctl.run_action(
        "backup", paths, engine="docker", output="/var/lib/agentchatroom/backups/test.dump"
    )
    assert calls[0][-2:] == ("config", "--quiet")
    assert calls[1][-3:] == ("up", "-d", "--build")
    assert calls[2][-4:] == ("--config", "/app/config.server.toml", "--output", "/var/lib/agentchatroom/backups/test.dump")
    assert all("secret" not in " ".join(call) for call in calls)


def test_down_with_volumes_requires_confirmation(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(serverctl, "_validate_application_config", lambda *args: {"valid": True})
    with pytest.raises(serverctl.DeploymentError, match="--confirm"):
        serverctl.run_action("down", paths, engine="docker", volumes=True)


def test_application_config_check_redacts_environment(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = '{"valid": true, "checks": {"database": "postgresql"}}'

    def fake_run(command, **kwargs):
        captured["command"] = tuple(command)
        captured["env"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(serverctl.subprocess, "run", fake_run)
    result = serverctl._validate_application_config(
        paths, {"AGENTCHATROOM_ADMIN_TOKEN": "super-secret-value"}
    )
    assert result["valid"] is True
    assert "super-secret-value" not in json.dumps(result)
    assert "config-check" in captured["command"]


def test_restore_stops_and_restarts_room_container(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(serverctl.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(serverctl, "_validate_application_config", lambda *args: {"valid": True})
    monkeypatch.setattr(
        serverctl,
        "_run",
        lambda command, *, cwd, capture=False: calls.append(tuple(command))
        or serverctl.CommandResult(tuple(command), 0),
    )

    serverctl.run_action(
        "restore",
        paths,
        engine="docker",
        input_path="/var/lib/agentchatroom/backups/room.dump",
        confirm=True,
    )
    assert calls[0][-2:] == ("stop", "room")
    assert calls[1][-6:] == (
        "restore",
        "--config",
        "/app/config.server.toml",
        "--input",
        "/var/lib/agentchatroom/backups/room.dump",
        "--confirm",
    )
    assert calls[2][-2:] == ("start", "room")


def test_main_verify_uses_release_preflight_script(monkeypatch, tmp_path, capsys):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        serverctl,
        "_run",
        lambda command, *, cwd, capture=False: calls.append(tuple(command))
        or serverctl.CommandResult(tuple(command), 0),
    )
    assert (
        serverctl.main(
            [
                "--repo-root",
                str(tmp_path),
                "verify",
                "--url",
                "https://room.internal",
                "--project-id",
                "project_demo",
            ]
        )
        == 0
    )
    assert calls == [
        (
            serverctl.sys.executable,
            "scripts/verify_release.py",
            "--url",
            "https://room.internal",
            "--no-source-checks",
            "--project-id",
            "project_demo",
        )
    ]
    assert "room.internal" in capsys.readouterr().out
