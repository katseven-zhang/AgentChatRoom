from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from agentchatroom.config import (
    CONFIG_FILE_SCHEMA,
    default_data_dir,
    load_settings,
)


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
mcp_http_session_idle_timeout_seconds = 90
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
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("AGENTCHATROOM_MCP_BRIDGE_COMMAND", "python-from-env")

    settings = load_settings(config_path)

    assert settings.host == "0.0.0.0"
    assert settings.port == 9200
    assert settings.deployment_profile == "local"
    assert settings.mcp_http_json_response is True
    assert settings.mcp_http_session_idle_timeout_seconds == 120
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


def test_frozen_login_launch_uses_executable_directory_not_working_directory(
    monkeypatch, tmp_path
):
    import agentchatroom.config as config

    bundle = tmp_path / "安装 目录" / "agentchatroom"
    bundle.mkdir(parents=True)
    other_cwd = tmp_path / "unrelated"
    other_cwd.mkdir()
    monkeypatch.delenv("AGENTCHATROOM_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_ROOT", raising=False)
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(bundle / "agentchatroom.exe"))
    monkeypatch.chdir(other_cwd)

    assert default_data_dir() == bundle / ".agentchatroom" / "runtime"


def test_frozen_executable_inside_checkout_preserves_existing_runtime(
    monkeypatch, tmp_path
):
    import agentchatroom.config as config

    checkout = tmp_path / "checkout"
    bundle = checkout / "dist" / "agentchatroom"
    (checkout / "src" / "agentchatroom").mkdir(parents=True)
    bundle.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='agentchatroom'\n", encoding="utf-8")
    monkeypatch.delenv("AGENTCHATROOM_DATA_DIR", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_ROOT", raising=False)
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(bundle / "agentchatroom.exe"))
    monkeypatch.chdir(tmp_path)

    assert default_data_dir() == checkout / ".agentchatroom" / "runtime"


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


def test_mcp_http_session_idle_timeout_must_be_positive(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="session idle timeout must be positive"):
        load_settings()


def test_default_http_session_idle_timeout_covers_long_local_runs(
    monkeypatch, tmp_path
):
    """#116: 默认闲置上限覆盖常规 8-15 分钟本地构建/测试，并仍可覆盖面。"""
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))

    settings = load_settings()

    # MCP SDK 建议多数部署使用 1800 秒（30 分钟）。
    assert settings.mcp_http_session_idle_timeout_seconds == 1800.0

    # 公开示例配置必须与服务端默认值一致，避免运维按过期文档调小上限。
    example = (Path(__file__).parents[1] / "config.example.toml").read_text(
        encoding="utf-8"
    )
    assert "mcp_http_session_idle_timeout_seconds = 1800.0" in example

    # TOML 与环境变量仍可覆盖默认值。
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS", "600")
    assert load_settings().mcp_http_session_idle_timeout_seconds == 600.0


def test_mcp_http_session_adoption_defaults_and_bounds(monkeypatch, tmp_path):
    """#117: 默认开启透明收养；严格模式与有界墓碑表都可配置。"""
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))

    settings = load_settings()

    assert settings.mcp_http_session_adoption is True
    assert settings.mcp_http_tombstone_limit == 256
    assert settings.mcp_http_tombstone_ttl_seconds == 86400.0

    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_SESSION_ADOPTION", "false")
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_TOMBSTONE_LIMIT", "8")
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_TOMBSTONE_TTL_SECONDS", "60")

    tuned = load_settings()

    assert tuned.mcp_http_session_adoption is False
    assert tuned.mcp_http_tombstone_limit == 8
    assert tuned.mcp_http_tombstone_ttl_seconds == 60.0

    example = (Path(__file__).parents[1] / "config.example.toml").read_text(
        encoding="utf-8"
    )
    assert "mcp_http_session_adoption = true" in example
    assert "mcp_http_tombstone_limit = 256" in example
    assert "mcp_http_tombstone_ttl_seconds = 86400.0" in example


def test_mcp_http_tombstone_bounds_must_be_positive(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_TOMBSTONE_LIMIT", "0")

    with pytest.raises(ValueError, match="tombstone limit must be positive"):
        load_settings()

    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_TOMBSTONE_LIMIT", "8")
    monkeypatch.setenv("AGENTCHATROOM_MCP_HTTP_TOMBSTONE_TTL_SECONDS", "0")

    with pytest.raises(ValueError, match="tombstone TTL must be positive"):
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


def test_management_login_keys_load_from_example_toml(tmp_path, monkeypatch):
    """#188: management_login_* keys must load from a real example-style TOML."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[application]",
                "schema_version = 1",
                'deployment_profile = "local"',
                "[security]",
                "management_login_max_failures = 3",
                "management_login_window_seconds = 45",
                "management_login_lockout_seconds = 90",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENTCHATROOM_MANAGEMENT_LOGIN_MAX_FAILURES", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_MANAGEMENT_LOGIN_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_MANAGEMENT_LOGIN_LOCKOUT_SECONDS", raising=False)
    settings = load_settings(config_path)
    assert settings.management_login_max_failures == 3
    assert settings.management_login_window_seconds == 45.0
    assert settings.management_login_lockout_seconds == 90.0


def test_management_login_keys_load_from_shipped_example_configs():
    """#188: both shipped example configs must parse and carry the three keys."""
    root = __import__("pathlib").Path(__file__).parents[1]
    for relative in ("config.example.toml", "deploy/config.server.example.toml"):
        data = tomllib.loads((root / relative).read_text(encoding="utf-8"))
        security = data.get("security", {})
        assert "management_login_max_failures" in security, relative
        assert "management_login_window_seconds" in security, relative
        assert "management_login_lockout_seconds" in security, relative


def test_documents_inject_max_chars_file_value_takes_effect(monkeypatch, tmp_path):
    """#178: [documents].inject_max_chars must map through _merge_toml into Settings."""
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGENTCHATROOM_PROJECT_DOC_INJECT_MAX_CHARS", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[documents]\ninject_max_chars = 4096\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.project_doc_inject_max_chars == 4096


def test_documents_inject_max_chars_priority_env_over_file_over_default(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGENTCHATROOM_PROJECT_DOC_INJECT_MAX_CHARS", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[documents]\ninject_max_chars = 4096\n",
        encoding="utf-8",
    )

    assert load_settings(config_path).project_doc_inject_max_chars == 4096

    monkeypatch.setenv("AGENTCHATROOM_PROJECT_DOC_INJECT_MAX_CHARS", "8192")
    assert load_settings(config_path).project_doc_inject_max_chars == 8192

    monkeypatch.delenv("AGENTCHATROOM_PROJECT_DOC_INJECT_MAX_CHARS", raising=False)
    config_path.write_text(
        "[application]\nschema_version = 1\n",
        encoding="utf-8",
    )
    assert load_settings(config_path).project_doc_inject_max_chars == 12000


def _settings_attr_for(section: str, key: str) -> str:
    """Map CONFIG_FILE_SCHEMA keys onto Settings field names."""
    if (section, key) == ("application", "schema_version"):
        return "config_schema_version"
    if (section, key) == ("documents", "inject_max_chars"):
        return "project_doc_inject_max_chars"
    if section == "database":
        return f"database_{key}"
    if section == "knowledge":
        return f"knowledge_{key}"
    if (section, key) == ("coordination", "mcp_message_context_limit"):
        return "mcp_message_limit"
    return key


def test_every_config_file_schema_key_maps_into_settings(monkeypatch, tmp_path):
    """Walk CONFIG_FILE_SCHEMA: each file key must land on a Settings field."""
    samples: dict[str, dict[str, object]] = {
        "application": {
            "schema_version": 1,
            "deployment_profile": "local",
        },
        "server": {
            "host": "127.0.0.1",
            "port": 9101,
            "mcp_http_enabled": False,
            "mcp_http_path": "/mcp-custom",
            "mcp_http_stateless": True,
            "mcp_http_json_response": False,
            "mcp_http_session_idle_timeout_seconds": 600.0,
            "mcp_http_session_adoption": False,
            "mcp_http_tombstone_limit": 32,
            "mcp_http_tombstone_ttl_seconds": 3600.0,
            "mcp_bridge_command": "python3",
            "external_base_url": "",
            "trusted_proxy_headers": False,
            "trusted_proxy_ips": "10.0.0.9",
        },
        "database": {
            "backend": "sqlite",
            "url_env": "ROOM_TEST_DATABASE_URL",
            "pool_min_size": 2,
            "pool_max_size": 5,
            "connect_timeout_seconds": 2.5,
        },
        "knowledge": {
            "kinds": ["decision", "pitfall"],
            "require_verified_task": False,
        },
        "coordination": {
            "mcp_roots_timeout_seconds": 4.0,
            "heartbeat_timeout_seconds": 60,
            "presence_keepalive_enabled": True,
            "presence_keepalive_interval_seconds": 15.0,
            "session_token_ttl_seconds": 3600,
            "default_lease_ttl_seconds": 600,
            "max_lease_ttl_seconds": 7200,
            "sse_poll_interval_seconds": 1.5,
            "presence_refresh_interval_seconds": 2.5,
            "max_sse_clients_per_project": 16,
            "sse_per_ip_limit": 4,
            "audit_window_size": 50,
            "token_touch_interval_seconds": 30.0,
            "token_touch_min_calls": 8,
            "mcp_message_limit": 7,
            "message_max_body_length": 4096,
            "task_text_max_length": 2048,
            "list_input_max_count": 25,
            "test_entry_notes_max_length": 512,
            "project_document_max_bytes": 4096,
            "snapshot_recent_limit": 50,
            "idempotency_retention_days": 7,
            # mcp_message_context_limit is an alias; covered in a dedicated load below.
        },
        "security": {
            "agent_token_ttl_seconds": 3600,
            "max_agent_token_ttl_seconds": 7200,
            "mcp_http_auth_required": False,
            "management_auth_required": False,
            "management_token_env": "ROOM_TEST_ADMIN_TOKEN",
            "management_session_ttl_seconds": 3600,
            "management_cookie_name": "room_test_admin",
            "management_login_max_failures": 4,
            "management_login_window_seconds": 30.0,
            "management_login_lockout_seconds": 45.0,
        },
        "backup": {
            "auto_backup_enabled": True,
            "auto_backup_interval_seconds": 120,
            "auto_backup_max_kept": 5,
        },
        "documents": {
            "inject_max_chars": 4096,
        },
        "interface": {
            "product_name": "Mapped Room",
            "default_theme": "dark",
        },
    }

    for name in list(os.environ):
        if name.startswith("AGENTCHATROOM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))

    schema_keys = {
        (section, key)
        for section, keys in CONFIG_FILE_SCHEMA.items()
        for key in keys
    }
    covered = {
        (section, key)
        for section, keys in samples.items()
        for key in keys
    }
    # Every schema key must be exercised; the context-limit alias is asserted separately.
    alias_key = ("coordination", "mcp_message_context_limit")
    assert covered | {alias_key} == schema_keys

    lines: list[str] = []
    for section, values in samples.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, str):
                rendered = f'"{value}"'
            elif isinstance(value, list):
                rendered = "[" + ", ".join(f'"{item}"' for item in value) + "]"
            else:
                rendered = repr(value)
            lines.append(f"{key} = {rendered}")
        lines.append("")

    config_path = tmp_path / "mapped.toml"
    config_path.write_text("\n".join(lines), encoding="utf-8")
    settings = load_settings(config_path)

    for section, values in samples.items():
        for key, file_value in values.items():
            attr = _settings_attr_for(section, key)
            expected = file_value
            if attr == "knowledge_kinds":
                expected = tuple(file_value)  # type: ignore[arg-type]
            assert getattr(settings, attr) == expected, (
                f"[{section}].{key} did not map to Settings.{attr}"
            )

    # Alias: [coordination].mcp_message_context_limit -> Settings.mcp_message_limit
    # when mcp_message_limit is absent from the file.
    alias_path = tmp_path / "alias.toml"
    alias_path.write_text(
        "[coordination]\nmcp_message_context_limit = 9\n",
        encoding="utf-8",
    )
    assert load_settings(alias_path).mcp_message_limit == 9
