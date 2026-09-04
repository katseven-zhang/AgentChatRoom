from __future__ import annotations

import pytest

from agentchatroom.config import default_data_dir, load_settings


def test_environment_overrides_config_file(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[application]
schema_version = 1

[server]
host = "0.0.0.0"
port = 9100
mcp_http_json_response = false
mcp_bridge_command = "python-from-file"

[database]
backend = "postgresql"
url_env = "ROOM_DATABASE_URL"
pool_min_size = 2
pool_max_size = 12
connect_timeout_seconds = 7.5

[coordination]
heartbeat_timeout_seconds = 60
presence_keepalive_enabled = true
presence_keepalive_interval_seconds = 20
session_token_ttl_seconds = 3600
default_lease_ttl_seconds = 120
max_lease_ttl_seconds = 600
sse_poll_interval_seconds = 1.0
presence_refresh_interval_seconds = 1.5

[security]
management_auth_required = true

[interface]
product_name = "Configured Room"
default_theme = "dark"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_PORT", "9200")
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_JSON_RESPONSE", "true")
    monkeypatch.setenv("AGENTCHATROOM_MCP_BRIDGE_COMMAND", "python-from-env")

    settings = load_settings(config_path)

    assert settings.host == "0.0.0.0"
    assert settings.port == 9200
    assert settings.deployment_profile == "local"
    assert settings.mcp_http_json_response is True
    assert settings.mcp_bridge_command == "python-from-env"
    assert settings.product_name == "Configured Room"
    assert settings.default_lease_ttl_seconds == 120
    assert settings.presence_keepalive_enabled is True
    assert settings.presence_keepalive_interval_seconds == 20
    assert settings.presence_refresh_interval_seconds == 1.5
    assert settings.max_sse_clients_per_project == 64
    assert settings.sse_per_ip_limit == 16
    assert settings.token_touch_interval_seconds == 60.0
    assert settings.token_touch_min_calls == 32
    assert settings.management_auth_required is True
    assert settings.database_backend == "postgresql"
    assert settings.database_url_env == "ROOM_DATABASE_URL"
    assert settings.database_pool_min_size == 2
    assert settings.database_pool_max_size == 12
    assert settings.database_connect_timeout_seconds == 7.5
    assert settings.trusted_proxy_headers is True
    assert settings.trusted_proxy_ips == "127.0.0.1"
    assert settings.public_dict()["database_path"] is None
    assert settings.public_dict()["database_target"] == "env:ROOM_DATABASE_URL"


def test_explicit_data_dir_overrides_environment(monkeypatch, tmp_path):
    environment_dir = tmp_path / "environment-runtime"
    explicit_dir = tmp_path / "explicit-runtime"
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(environment_dir))

    settings = load_settings(data_dir=explicit_dir)

    assert settings.data_dir == explicit_dir.resolve()
    assert settings.database_path == explicit_dir.resolve() / "agentchatroom.db"


def test_default_data_dir_follows_the_cloned_repository(monkeypatch, tmp_path):
    checkout = tmp_path / "renamed-agentchatroom-checkout"
    nested = checkout / "docs" / "notes"
    (checkout / "src" / "agentchatroom").mkdir(parents=True)
    nested.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "agentchatroom"\n', encoding="utf-8"
    )
    monkeypatch.delenv("AGENTCHATROOM_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_ROOT", raising=False)
    monkeypatch.chdir(nested)

    assert default_data_dir() == (checkout / ".agentchatroom" / "runtime").resolve()


def test_application_root_override_keeps_runtime_under_that_root(
    monkeypatch, tmp_path
):
    checkout = tmp_path / "configured-checkout"
    monkeypatch.delenv("AGENTCHATROOM_DATA_DIR", raising=False)
    monkeypatch.setenv("AGENTCHATROOM_ROOT", str(checkout))

    assert default_data_dir() == (checkout / ".agentchatroom" / "runtime").resolve()


def test_invalid_config_fails_with_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_PORT", "70000")

    with pytest.raises(ValueError, match="port must be between"):
        load_settings()


def test_empty_bridge_command_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MCP_BRIDGE_COMMAND", "   ")

    with pytest.raises(ValueError, match="MCP Bridge command must not be empty"):
        load_settings()


def test_presence_keepalive_interval_must_be_less_than_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_HEARTBEAT_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS", "10")

    with pytest.raises(ValueError, match="less than heartbeat timeout"):
        load_settings()


def test_unknown_or_mistyped_file_configuration_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    config_path = tmp_path / "invalid.toml"
    config_path.write_text('[server]\nport = "8765"\nunknown = true\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown keys"):
        load_settings(config_path)


def test_non_loopback_binding_requires_management_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="requires management authentication"):
        load_settings()


def test_server_profile_requires_and_accepts_production_prerequisites(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_DEPLOYMENT_PROFILE", "server")
    monkeypatch.setenv("AGENTCHATROOM_DATABASE_BACKEND", "postgresql")
    monkeypatch.setenv("AGENTCHATROOM_EXTERNAL_BASE_URL", "https://room.example.com")
    monkeypatch.setenv("AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_AUTH_REQUIRED", "true")

    settings = load_settings()

    assert settings.deployment_profile == "server"
    assert settings.database_backend == "postgresql"

    monkeypatch.setenv("AGENTCHATROOM_EXTERNAL_BASE_URL", "http://room.example.com")
    with pytest.raises(ValueError, match="https external base URL"):
        load_settings()


def test_lan_profile_requires_management_and_mcp_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_DEPLOYMENT_PROFILE", "lan")
    monkeypatch.setenv("AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED", "false")

    with pytest.raises(ValueError, match="lan deployment profile requires management authentication"):
        load_settings()


def test_lan_profile_requires_a_reachable_external_base_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_DEPLOYMENT_PROFILE", "lan")
    monkeypatch.setenv("AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED", "true")
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_AUTH_REQUIRED", "true")

    with pytest.raises(ValueError, match="requires an external base URL"):
        load_settings()

    monkeypatch.setenv("AGENTCHATROOM_EXTERNAL_BASE_URL", "http://127.0.0.1:8765")
    with pytest.raises(ValueError, match="cannot use a loopback"):
        load_settings()

    monkeypatch.setenv("AGENTCHATROOM_EXTERNAL_BASE_URL", "http://192.0.2.10:8767")
    settings = load_settings()
    assert settings.external_base_url == "http://192.0.2.10:8767"


def test_proxy_header_trust_is_explicit_and_validated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_TRUSTED_PROXY_HEADERS", "true")
    monkeypatch.setenv("AGENTCHATROOM_TRUSTED_PROXY_IPS", " 10.0.0.10,10.0.0.11 ")

    settings = load_settings()

    assert settings.trusted_proxy_headers is True
    assert settings.trusted_proxy_ips == "10.0.0.10,10.0.0.11"

    monkeypatch.setenv("AGENTCHATROOM_TRUSTED_PROXY_IPS", "")
    with pytest.raises(ValueError, match="trusted proxy IP list"):
        load_settings()


def test_invalid_database_configuration_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_DATABASE_BACKEND", "mysql")

    with pytest.raises(ValueError, match="database backend must be sqlite or postgresql"):
        load_settings()

    monkeypatch.setenv("AGENTCHATROOM_DATABASE_BACKEND", "postgresql")
    monkeypatch.setenv("AGENTCHATROOM_DATABASE_POOL_MIN_SIZE", "4")
    monkeypatch.setenv("AGENTCHATROOM_DATABASE_POOL_MAX_SIZE", "2")

    with pytest.raises(ValueError, match="maximum size must not be less than minimum"):
        load_settings()
