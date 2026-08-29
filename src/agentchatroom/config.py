from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .contracts import KNOWLEDGE_DEFAULT_KINDS, KNOWLEDGE_KIND_PATTERN


CONFIG_FILE_SCHEMA: dict[str, dict[str, tuple[type, ...]]] = {
    "application": {
        "schema_version": (int,),
        "deployment_profile": (str,),
    },
    "server": {
        "host": (str,),
        "port": (int,),
        "mcp_http_enabled": (bool,),
        "mcp_http_path": (str,),
        "mcp_http_stateless": (bool,),
        "mcp_http_json_response": (bool,),
        "mcp_bridge_command": (str,),
        "external_base_url": (str,),
        "trusted_proxy_headers": (bool,),
        "trusted_proxy_ips": (str,),
    },
    "database": {
        "backend": (str,),
        "url_env": (str,),
        "pool_min_size": (int,),
        "pool_max_size": (int,),
        "connect_timeout_seconds": (int, float),
    },
    "knowledge": {
        "kinds": (list,),
        "require_verified_task": (bool,),
    },
    "coordination": {
        "heartbeat_timeout_seconds": (int,),
        "presence_keepalive_enabled": (bool,),
        "presence_keepalive_interval_seconds": (int, float),
        "session_token_ttl_seconds": (int,),
        "default_lease_ttl_seconds": (int,),
        "max_lease_ttl_seconds": (int,),
        "sse_poll_interval_seconds": (int, float),
        "presence_refresh_interval_seconds": (int, float),
    },
    "security": {
        "agent_token_ttl_seconds": (int,),
        "max_agent_token_ttl_seconds": (int,),
        "mcp_http_auth_required": (bool,),
        "management_auth_required": (bool,),
        "management_token_env": (str,),
        "management_session_ttl_seconds": (int,),
        "management_cookie_name": (str,),
    },
    "interface": {"product_name": (str,), "default_theme": (str,)},
}


def _find_repository_root(start: Path) -> Path | None:
    """Return the nearest source checkout containing this application."""
    resolved = start.expanduser().resolve()
    if resolved.is_file():
        resolved = resolved.parent
    for candidate in (resolved, *resolved.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "src" / "agentchatroom").is_dir()
        ):
            return candidate
    return None


def default_application_root(explicit: str | Path | None = None) -> Path:
    """Resolve the local application root without relying on a user path."""
    explicit = explicit or os.getenv("AGENTCHATROOM_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    for start in (Path.cwd(), Path(__file__)):
        repository_root = _find_repository_root(start)
        if repository_root is not None:
            return repository_root
    return Path.cwd().resolve()


def default_data_dir(explicit: str | Path | None = None) -> Path:
    """Resolve one ignored runtime directory inside the current checkout."""
    explicit = explicit or os.getenv("AGENTCHATROOM_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (default_application_root() / ".agentchatroom" / "runtime").resolve()


def normalize_knowledge_kinds(values: Any) -> tuple[str, ...]:
    """Validate and normalize configured Knowledge kinds from any adapter."""
    if values is None:
        return tuple(KNOWLEDGE_DEFAULT_KINDS)
    if isinstance(values, str):
        values = [item for item in values.split(",")]
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("knowledge kinds must be a non-empty list")
    normalized: list[str] = []
    for value in values:
        kind = str(value).strip()
        if not KNOWLEDGE_KIND_PATTERN.fullmatch(kind):
            raise ValueError(
                "every knowledge kind must match ^[a-z][a-z0-9_-]{0,63}$"
            )
        normalized.append(kind)
    unique = tuple(dict.fromkeys(normalized))
    if not unique:
        raise ValueError("knowledge kinds must contain at least one kind")
    return unique


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    config_path: Path | None = None
    config_schema_version: int = 1
    deployment_profile: str = "local"
    host: str = "127.0.0.1"
    port: int = 8765
    mcp_http_enabled: bool = True
    mcp_http_path: str = "/mcp"
    mcp_http_stateless: bool = False
    mcp_http_json_response: bool = True
    mcp_bridge_command: str = "python"
    external_base_url: str = ""
    trusted_proxy_headers: bool = True
    trusted_proxy_ips: str = "127.0.0.1"
    database_backend: str = "sqlite"
    database_url_env: str = "AGENTCHATROOM_DATABASE_URL"
    database_pool_min_size: int = 1
    database_pool_max_size: int = 10
    database_connect_timeout_seconds: float = 10.0
    agent_token_ttl_seconds: int = 2592000
    max_agent_token_ttl_seconds: int = 31536000
    mcp_http_auth_required: bool = True
    management_auth_required: bool = False
    management_token_env: str = "AGENTCHATROOM_ADMIN_TOKEN"
    management_session_ttl_seconds: int = 43200
    management_cookie_name: str = "agentchatroom_admin"
    heartbeat_timeout_seconds: int = 45
    presence_keepalive_enabled: bool = True
    presence_keepalive_interval_seconds: float = 15.0
    session_token_ttl_seconds: int = 43200
    default_lease_ttl_seconds: int = 1800
    max_lease_ttl_seconds: int = 86400
    sse_poll_interval_seconds: float = 0.5
    presence_refresh_interval_seconds: float = 1.0
    knowledge_kinds: tuple[str, ...] = tuple(KNOWLEDGE_DEFAULT_KINDS)
    knowledge_require_verified_task: bool = True
    product_name: str = "AgentChatRoom"
    default_theme: str = "system"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "agentchatroom.db"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["data_dir"] = str(self.data_dir)
        data["database_path"] = (
            str(self.database_path) if self.database_backend == "sqlite" else None
        )
        data["database_target"] = (
            str(self.database_path)
            if self.database_backend == "sqlite"
            else f"env:{self.database_url_env}"
        )
        data["config_path"] = str(self.config_path) if self.config_path else None
        return data


def _merge_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    unknown_sections = sorted(set(raw) - set(CONFIG_FILE_SCHEMA))
    if unknown_sections:
        raise ValueError(f"Unknown configuration sections: {', '.join(unknown_sections)}")
    for section, values in raw.items():
        if not isinstance(values, dict):
            raise ValueError(f"Configuration section [{section}] must be a table")
        unknown_keys = sorted(set(values) - set(CONFIG_FILE_SCHEMA[section]))
        if unknown_keys:
            raise ValueError(
                f"Unknown keys in [{section}]: {', '.join(unknown_keys)}"
            )
        for key, value in values.items():
            expected = CONFIG_FILE_SCHEMA[section][key]
            accepts_bool = bool in expected
            invalid = (
                not isinstance(value, bool)
                if accepts_bool
                else isinstance(value, bool) or not isinstance(value, expected)
            )
            if invalid:
                names = " or ".join(item.__name__ for item in expected)
                raise ValueError(f"[{section}].{key} must be {names}")
        if section == "knowledge" and "kinds" in values:
            values["kinds"] = list(normalize_knowledge_kinds(values["kinds"]))
    return {
        "config_schema_version": raw.get("application", {}).get("schema_version"),
        "deployment_profile": raw.get("application", {}).get("deployment_profile"),
        "host": raw.get("server", {}).get("host"),
        "port": raw.get("server", {}).get("port"),
        "mcp_http_enabled": raw.get("server", {}).get("mcp_http_enabled"),
        "mcp_http_path": raw.get("server", {}).get("mcp_http_path"),
        "mcp_http_stateless": raw.get("server", {}).get("mcp_http_stateless"),
        "mcp_http_json_response": raw.get("server", {}).get(
            "mcp_http_json_response"
        ),
        "mcp_bridge_command": raw.get("server", {}).get("mcp_bridge_command"),
        "external_base_url": raw.get("server", {}).get("external_base_url"),
        "trusted_proxy_headers": raw.get("server", {}).get(
            "trusted_proxy_headers"
        ),
        "trusted_proxy_ips": raw.get("server", {}).get("trusted_proxy_ips"),
        "database_backend": raw.get("database", {}).get("backend"),
        "database_url_env": raw.get("database", {}).get("url_env"),
        "database_pool_min_size": raw.get("database", {}).get("pool_min_size"),
        "database_pool_max_size": raw.get("database", {}).get("pool_max_size"),
        "database_connect_timeout_seconds": raw.get("database", {}).get(
            "connect_timeout_seconds"
        ),
        "knowledge_kinds": raw.get("knowledge", {}).get("kinds"),
        "knowledge_require_verified_task": raw.get("knowledge", {}).get(
            "require_verified_task"
        ),
        "heartbeat_timeout_seconds": raw.get("coordination", {}).get(
            "heartbeat_timeout_seconds"
        ),
        "presence_keepalive_enabled": raw.get("coordination", {}).get(
            "presence_keepalive_enabled"
        ),
        "presence_keepalive_interval_seconds": raw.get("coordination", {}).get(
            "presence_keepalive_interval_seconds"
        ),
        "session_token_ttl_seconds": raw.get("coordination", {}).get(
            "session_token_ttl_seconds"
        ),
        "default_lease_ttl_seconds": raw.get("coordination", {}).get(
            "default_lease_ttl_seconds"
        ),
        "max_lease_ttl_seconds": raw.get("coordination", {}).get(
            "max_lease_ttl_seconds"
        ),
        "sse_poll_interval_seconds": raw.get("coordination", {}).get(
            "sse_poll_interval_seconds"
        ),
        "presence_refresh_interval_seconds": raw.get("coordination", {}).get(
            "presence_refresh_interval_seconds"
        ),
        "agent_token_ttl_seconds": raw.get("security", {}).get(
            "agent_token_ttl_seconds"
        ),
        "max_agent_token_ttl_seconds": raw.get("security", {}).get(
            "max_agent_token_ttl_seconds"
        ),
        "mcp_http_auth_required": raw.get("security", {}).get(
            "mcp_http_auth_required"
        ),
        "management_auth_required": raw.get("security", {}).get(
            "management_auth_required"
        ),
        "management_token_env": raw.get("security", {}).get(
            "management_token_env"
        ),
        "management_session_ttl_seconds": raw.get("security", {}).get(
            "management_session_ttl_seconds"
        ),
        "management_cookie_name": raw.get("security", {}).get(
            "management_cookie_name"
        ),
        "product_name": raw.get("interface", {}).get("product_name"),
        "default_theme": raw.get("interface", {}).get("default_theme"),
    }


def load_settings(
    config_path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
) -> Settings:
    """Load validated settings with an explicit data directory taking precedence."""
    resolved_data_dir = default_data_dir(data_dir)
    explicit_config = config_path or os.getenv("AGENTCHATROOM_CONFIG")
    path = Path(
        explicit_config or resolved_data_dir / "config.toml"
    ).expanduser()
    file_values = {key: value for key, value in _merge_toml(path).items() if value is not None}

    def environment_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        normalized = raw.strip().lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
        return normalized == "true"

    values: dict[str, Any] = {
        "data_dir": resolved_data_dir,
        "config_path": path.resolve() if explicit_config else None,
        "config_schema_version": int(
            os.getenv(
                "AGENTCHATROOM_CONFIG_SCHEMA_VERSION",
                file_values.get("config_schema_version", 1),
            )
        ),
        "deployment_profile": os.getenv(
            "AGENTCHATROOM_DEPLOYMENT_PROFILE",
            file_values.get("deployment_profile", "local"),
        ).strip().lower(),
        "host": os.getenv("AGENTCHATROOM_HOST", file_values.get("host", "127.0.0.1")),
        "port": int(os.getenv("AGENTCHATROOM_PORT", file_values.get("port", 8765))),
        "mcp_http_enabled": environment_bool(
            "AGENTCHATROOM_MCP_HTTP_ENABLED",
            file_values.get("mcp_http_enabled", True),
        ),
        "mcp_http_path": os.getenv(
            "AGENTCHATROOM_MCP_HTTP_PATH",
            file_values.get("mcp_http_path", "/mcp"),
        ),
        "mcp_http_stateless": environment_bool(
            "AGENTCHATROOM_MCP_HTTP_STATELESS",
            file_values.get("mcp_http_stateless", False),
        ),
        "mcp_http_json_response": environment_bool(
            "AGENTCHATROOM_MCP_HTTP_JSON_RESPONSE",
            file_values.get("mcp_http_json_response", True),
        ),
        "mcp_bridge_command": os.getenv(
            "AGENTCHATROOM_MCP_BRIDGE_COMMAND",
            file_values.get("mcp_bridge_command", "python"),
        ).strip(),
        "external_base_url": os.getenv(
            "AGENTCHATROOM_EXTERNAL_BASE_URL",
            file_values.get("external_base_url", ""),
        ).rstrip("/"),
        "trusted_proxy_headers": environment_bool(
            "AGENTCHATROOM_TRUSTED_PROXY_HEADERS",
            file_values.get("trusted_proxy_headers", True),
        ),
        "trusted_proxy_ips": os.getenv(
            "AGENTCHATROOM_TRUSTED_PROXY_IPS",
            file_values.get("trusted_proxy_ips", "127.0.0.1"),
        ).strip(),
        "database_backend": os.getenv(
            "AGENTCHATROOM_DATABASE_BACKEND",
            file_values.get("database_backend", "sqlite"),
        ).strip().lower(),
        "database_url_env": os.getenv(
            "AGENTCHATROOM_DATABASE_URL_ENV",
            file_values.get("database_url_env", "AGENTCHATROOM_DATABASE_URL"),
        ).strip(),
        "database_pool_min_size": int(
            os.getenv(
                "AGENTCHATROOM_DATABASE_POOL_MIN_SIZE",
                file_values.get("database_pool_min_size", 1),
            )
        ),
        "database_pool_max_size": int(
            os.getenv(
                "AGENTCHATROOM_DATABASE_POOL_MAX_SIZE",
                file_values.get("database_pool_max_size", 10),
            )
        ),
        "database_connect_timeout_seconds": float(
            os.getenv(
                "AGENTCHATROOM_DATABASE_CONNECT_TIMEOUT_SECONDS",
                file_values.get("database_connect_timeout_seconds", 10.0),
            )
        ),
        "agent_token_ttl_seconds": int(
            os.getenv(
                "AGENTCHATROOM_AGENT_TOKEN_TTL_SECONDS",
                file_values.get("agent_token_ttl_seconds", 2592000),
            )
        ),
        "max_agent_token_ttl_seconds": int(
            os.getenv(
                "AGENTCHATROOM_MAX_AGENT_TOKEN_TTL_SECONDS",
                file_values.get("max_agent_token_ttl_seconds", 31536000),
            )
        ),
        "mcp_http_auth_required": environment_bool(
            "AGENTCHATROOM_MCP_HTTP_AUTH_REQUIRED",
            file_values.get("mcp_http_auth_required", True),
        ),
        "management_auth_required": environment_bool(
            "AGENTCHATROOM_MANAGEMENT_AUTH_REQUIRED",
            file_values.get("management_auth_required", False),
        ),
        "management_token_env": os.getenv(
            "AGENTCHATROOM_MANAGEMENT_TOKEN_ENV",
            file_values.get("management_token_env", "AGENTCHATROOM_ADMIN_TOKEN"),
        ),
        "management_session_ttl_seconds": int(
            os.getenv(
                "AGENTCHATROOM_MANAGEMENT_SESSION_TTL_SECONDS",
                file_values.get("management_session_ttl_seconds", 43200),
            )
        ),
        "management_cookie_name": os.getenv(
            "AGENTCHATROOM_MANAGEMENT_COOKIE_NAME",
            file_values.get("management_cookie_name", "agentchatroom_admin"),
        ),
        "heartbeat_timeout_seconds": int(
            os.getenv(
                "AGENTCHATROOM_HEARTBEAT_TIMEOUT_SECONDS",
                file_values.get("heartbeat_timeout_seconds", 45),
            )
        ),
        "presence_keepalive_enabled": environment_bool(
            "AGENTCHATROOM_PRESENCE_KEEPALIVE_ENABLED",
            file_values.get("presence_keepalive_enabled", True),
        ),
        "presence_keepalive_interval_seconds": float(
            os.getenv(
                "AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS",
                file_values.get("presence_keepalive_interval_seconds", 15.0),
            )
        ),
        "session_token_ttl_seconds": int(
            os.getenv(
                "AGENTCHATROOM_SESSION_TOKEN_TTL_SECONDS",
                file_values.get("session_token_ttl_seconds", 43200),
            )
        ),
        "default_lease_ttl_seconds": int(
            os.getenv(
                "AGENTCHATROOM_DEFAULT_LEASE_TTL_SECONDS",
                file_values.get("default_lease_ttl_seconds", 1800),
            )
        ),
        "max_lease_ttl_seconds": int(
            os.getenv(
                "AGENTCHATROOM_MAX_LEASE_TTL_SECONDS",
                file_values.get("max_lease_ttl_seconds", 86400),
            )
        ),
        "sse_poll_interval_seconds": float(
            os.getenv(
                "AGENTCHATROOM_SSE_POLL_INTERVAL_SECONDS",
                file_values.get("sse_poll_interval_seconds", 0.5),
            )
        ),
        "presence_refresh_interval_seconds": float(
            os.getenv(
                "AGENTCHATROOM_PRESENCE_REFRESH_INTERVAL_SECONDS",
                file_values.get("presence_refresh_interval_seconds", 1.0),
            )
        ),
        "knowledge_kinds": normalize_knowledge_kinds(
            os.getenv("AGENTCHATROOM_KNOWLEDGE_KINDS")
            if os.getenv("AGENTCHATROOM_KNOWLEDGE_KINDS") is not None
            else file_values.get("knowledge_kinds")
        ),
        "knowledge_require_verified_task": environment_bool(
            "AGENTCHATROOM_KNOWLEDGE_REQUIRE_VERIFIED_TASK",
            file_values.get("knowledge_require_verified_task", True),
        ),
        "product_name": os.getenv(
            "AGENTCHATROOM_PRODUCT_NAME", file_values.get("product_name", "AgentChatRoom")
        ),
        "default_theme": os.getenv(
            "AGENTCHATROOM_DEFAULT_THEME", file_values.get("default_theme", "system")
        ),
    }
    if not 1 <= values["port"] <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not str(values["host"]).strip():
        raise ValueError("host must not be empty")
    if (
        not str(values["mcp_http_path"]).startswith("/")
        or str(values["mcp_http_path"]) == "/"
        or str(values["mcp_http_path"]).endswith("/")
    ):
        raise ValueError("MCP HTTP path must start with / and have no trailing /")
    if not str(values["mcp_bridge_command"]).strip():
        raise ValueError("MCP Bridge command must not be empty")
    external_base_url = str(values["external_base_url"])
    if external_base_url and not external_base_url.startswith(("http://", "https://")):
        raise ValueError("external base URL must start with http:// or https://")
    parsed_external_url = urlparse(external_base_url) if external_base_url else None
    if external_base_url and (
        parsed_external_url is None
        or not parsed_external_url.netloc
        or not parsed_external_url.hostname
    ):
        raise ValueError("external base URL must include a valid host")
    if values["trusted_proxy_headers"] and not str(values["trusted_proxy_ips"]).strip():
        raise ValueError(
            "trusted proxy IP list must not be empty when proxy headers are enabled"
        )
    if values["database_backend"] not in {"sqlite", "postgresql"}:
        raise ValueError("database backend must be sqlite or postgresql")
    if not values["database_url_env"]:
        raise ValueError("database URL environment variable name must not be empty")
    if values["database_pool_min_size"] < 1:
        raise ValueError("database pool minimum size must be at least 1")
    if values["database_pool_max_size"] < values["database_pool_min_size"]:
        raise ValueError("database pool maximum size must not be less than minimum size")
    if values["database_connect_timeout_seconds"] <= 0:
        raise ValueError("database connect timeout must be positive")
    if values["config_schema_version"] != 1:
        raise ValueError("unsupported config schema version")
    if values["deployment_profile"] not in {"local", "lan", "server"}:
        raise ValueError("deployment profile must be local, lan, or server")
    if values["heartbeat_timeout_seconds"] < 5:
        raise ValueError("heartbeat timeout must be at least 5 seconds")
    if values["presence_keepalive_interval_seconds"] <= 0:
        raise ValueError("presence keepalive interval must be positive")
    if (
        values["presence_keepalive_enabled"]
        and values["presence_keepalive_interval_seconds"]
        >= values["heartbeat_timeout_seconds"]
    ):
        raise ValueError(
            "presence keepalive interval must be less than heartbeat timeout"
        )
    if values["session_token_ttl_seconds"] < values["heartbeat_timeout_seconds"]:
        raise ValueError("session token TTL must not be less than heartbeat timeout")
    if values["agent_token_ttl_seconds"] < 300:
        raise ValueError("Agent token TTL must be at least 300 seconds")
    if values["max_agent_token_ttl_seconds"] < values["agent_token_ttl_seconds"]:
        raise ValueError("max Agent token TTL must not be less than the default")
    if values["management_session_ttl_seconds"] < 300:
        raise ValueError("management session TTL must be at least 300 seconds")
    if not str(values["management_token_env"]).strip():
        raise ValueError("management token environment variable name must not be empty")
    if not str(values["management_cookie_name"]).strip():
        raise ValueError("management cookie name must not be empty")
    if values["host"] not in {"127.0.0.1", "localhost", "::1"} and not values[
        "management_auth_required"
    ]:
        raise ValueError("non-loopback server binding requires management authentication")
    if values["deployment_profile"] in {"lan", "server"}:
        if not values["management_auth_required"]:
            raise ValueError(
                f"{values['deployment_profile']} deployment profile requires management authentication"
            )
        if not values["mcp_http_auth_required"]:
            raise ValueError(
                f"{values['deployment_profile']} deployment profile requires MCP authentication"
            )
        if not external_base_url:
            raise ValueError(
                f"{values['deployment_profile']} deployment profile requires an external base URL"
            )
        if parsed_external_url and parsed_external_url.hostname.lower() in {
            "127.0.0.1",
            "localhost",
            "::1",
            "0.0.0.0",
            "::",
        }:
            raise ValueError(
                f"{values['deployment_profile']} deployment profile cannot use a loopback or wildcard external base URL"
            )
    if values["deployment_profile"] == "server":
        if values["database_backend"] != "postgresql":
            raise ValueError("server deployment profile requires PostgreSQL")
        if not external_base_url.startswith("https://"):
            raise ValueError(
                "server deployment profile requires an https external base URL"
            )
    if values["default_lease_ttl_seconds"] < 1:
        raise ValueError("default lease TTL must be positive")
    if values["max_lease_ttl_seconds"] < values["default_lease_ttl_seconds"]:
        raise ValueError("max lease TTL must not be less than default lease TTL")
    if values["sse_poll_interval_seconds"] <= 0:
        raise ValueError("SSE poll interval must be positive")
    if not 0.25 <= values["presence_refresh_interval_seconds"] <= 10:
        raise ValueError("presence refresh interval must be between 0.25 and 10 seconds")
    if not str(values["product_name"]).strip():
        raise ValueError("product name must not be empty")
    if values["default_theme"] not in {"system", "light", "dark"}:
        raise ValueError("default theme must be system, light, or dark")
    return Settings(**values)
