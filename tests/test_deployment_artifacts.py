import subprocess
import sys
import tomllib
from pathlib import Path

from agentchatroom import __version__


ROOT = Path(__file__).parents[1]


def test_container_deployment_artifacts_are_present_and_secret_free() -> None:
    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "Caddyfile.example").read_text(encoding="utf-8")
    serverctl = (ROOT / "deploy" / "serverctl.py").read_text(encoding="utf-8")

    assert "AGENTCHATROOM_DATABASE_URL" in compose
    assert "AGENTCHATROOM_ADMIN_TOKEN" in compose
    server_config = (ROOT / "deploy" / "config.server.example.toml").read_text(
        encoding="utf-8"
    )
    assert "trusted_proxy_headers" in server_config
    assert "trusted_proxy_ips" in server_config
    assert "postgres:16" in compose
    assert "room-data" in compose
    assert "/health/ready" in compose
    assert '127.0.0.1:${AGENTCHATROOM_BIND_PORT:-8765}:8765' in compose
    assert '"${AGENTCHATROOM_BIND_PORT:-8765}:8765"' not in compose
    assert "postgresql-client" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "pip install --no-cache-dir \".[postgresql]\"" in dockerfile
    assert "replace-with" in env_example
    assert "room.example.com" in caddy
    assert "def deployment_preflight" in serverctl
    assert '"--volumes"' in serverctl
    assert "AGENTCHATROOM_DATABASE_URL" in serverctl
    assert "proxy_file" in serverctl
    assert "postgresql://user:password" not in compose + dockerfile + env_example
    assert "acr." not in compose + dockerfile + env_example


def test_deploy_config_is_ignored_but_example_is_trackable() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deploy/.env" in gitignore
    assert "deploy/config.server.toml" in gitignore
    assert "deploy/Caddyfile" in gitignore
    assert (ROOT / "deploy" / ".env.example").is_file()


def test_local_agent_runtime_state_is_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for path in [".codex/", ".grok/", ".trae/", ".workbuddy/"]:
        assert path in gitignore
    assert "docs/" in gitignore
    assert ".agentchatroom/" in gitignore
    assert "python/post_room_update.py" in gitignore


def test_runtime_and_package_versions_match() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == __version__ == "0.2.0"


def test_windows_launcher_generator_is_portable_and_config_driven(tmp_path) -> None:
    generator = ROOT / "python" / "regen_launcher_cmds.py"
    source = generator.read_text(encoding="utf-8")

    assert "D:\\\\claw" not in source
    subprocess.run(
        [sys.executable, str(generator), "--root", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    start_bytes = (tmp_path / "启动 AgentChatRoom.cmd").read_bytes()
    stop_bytes = (tmp_path / "关闭 AgentChatRoom.cmd").read_bytes()
    start = start_bytes.decode("gbk")
    stop = stop_bytes.decode("gbk")

    assert b"\r\n" in start_bytes
    assert b"\n" not in start_bytes.replace(b"\r\n", b"")
    assert b"\r\n" in stop_bytes
    assert b"\n" not in stop_bytes.replace(b"\r\n", b"")
    assert "serve --open-browser" in start
    assert "load_settings().port" in stop
    assert ":8765" not in stop
