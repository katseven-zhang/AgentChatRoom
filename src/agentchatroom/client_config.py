"""User-level client configuration and ServerTarget abstraction for AgentChatRoom.

This module establishes the architectural seams for GUI shell and client targets:
1. ServerTarget abstraction: local vs remote mode with unified version handshake probe.
2. User-level client configuration: stored in ~/.agentchatroom/client.toml,
   strictly separated from repository checkout config.toml.
3. Zero authentication assumption: probe handles status codes gracefully and
   leaves credentials/tokens entirely to the in-webview SPA login flow.
"""

from __future__ import annotations

import json
import os
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import Settings, load_settings


class TargetError(ValueError):
    """Raised when a server target or URL is invalid."""


class ProbeError(RuntimeError):
    """Raised when probing a server target fails."""


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing a server target's /api/v1/version endpoint."""

    ok: bool
    version: str | None = None
    schema_version: int | None = None
    product_name: str | None = None
    status_code: int = 200
    error: str | None = None


@dataclass(frozen=True)
class ServerTarget:
    """Target server configuration for GUI shell and client adapters."""

    mode: Literal["local", "remote"] = "local"
    base_url: str = ""

    @classmethod
    def local(cls, base_url: str) -> ServerTarget:
        normalized = cls.normalize_url(base_url)
        return cls(mode="local", base_url=normalized)

    @classmethod
    def remote(cls, base_url: str) -> ServerTarget:
        normalized = cls.normalize_url(base_url)
        return cls(mode="remote", base_url=normalized)

    @staticmethod
    def normalize_url(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            raise TargetError("Server target URL cannot be empty")
        if "://" not in text:
            text = f"http://{text}"
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme not in ("http", "https"):
            raise TargetError(f"Unsupported URL scheme: {parsed.scheme}")
        if not parsed.netloc:
            raise TargetError(f"Invalid server target address: {raw}")
        return f"{parsed.scheme}://{parsed.netloc}"

    def probe(self, timeout: float = 3.0) -> ProbeResult:
        """Probe the server version and schema_version.

        Local and remote modes share the exact same probe path.
        Operates with zero auth assumption: if a server responds with 401/403,
        it is still reachable and the SPA inside webview will present the login UI.
        """
        version_url = f"{self.base_url.rstrip('/')}/api/v1/version"
        req = urllib.request.Request(
            version_url,
            headers={"User-Agent": "AgentChatRoom-Shell"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status_code = resp.status
                data = json.loads(resp.read().decode("utf-8"))
                return ProbeResult(
                    ok=True,
                    version=data.get("version"),
                    schema_version=data.get("schema_version"),
                    product_name=data.get("product_name"),
                    status_code=status_code,
                )
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
            except Exception:
                body = {}
            msg = body.get("error", {}).get("message", error.reason)
            return ProbeResult(
                ok=False,
                status_code=error.code,
                error=f"HTTP {error.code}: {msg}",
            )
        except urllib.error.URLError as error:
            return ProbeResult(
                ok=False,
                status_code=0,
                error=f"Connection failed: {error.reason}",
            )
        except Exception as error:
            return ProbeResult(
                ok=False,
                status_code=0,
                error=f"Probe failed: {error}",
            )


def default_client_config_path() -> Path:
    """Return user-level client config path in user directory (~/.agentchatroom/client.toml)."""
    override = os.getenv("AGENTCHATROOM_CLIENT_CONFIG", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".agentchatroom" / "client.toml").resolve()


@dataclass
class ClientConfig:
    """User-level client preferences stored in ~/.agentchatroom/client.toml."""

    mode: Literal["local", "remote"] = "local"
    remote_url: str = ""
    auto_start: bool = False
    restore_timeout_seconds: float = 3.0

    def __post_init__(self):
        if not 0 < self.restore_timeout_seconds <= 60:
            raise ValueError("restore_timeout_seconds must be between 0 and 60")

    def to_toml(self) -> str:
        lines = [
            "# AgentChatRoom User-Level Client Configuration",
            "# Reserved for GUI shell and client targets.",
            "# Strictly separated from project checkout config.toml.",
            "",
            "[target]",
            f'mode = "{self.mode}"',
            f"auto_start = {str(self.auto_start).lower()}",
            f"restore_timeout_seconds = {self.restore_timeout_seconds}",
        ]
        if self.remote_url:
            lines.append(f'remote_url = "{self.remote_url}"')
        lines.append("")
        return "\n".join(lines)


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def load_client_config(config_path: Path | None = None) -> ClientConfig:
    """Load user client configuration, defaulting to local mode."""
    path = config_path or default_client_config_path()
    if not path.exists():
        return ClientConfig(mode="local")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        target_section = data.get("target", {})
        mode = target_section.get("mode", "local")
        remote_url = str(target_section.get("remote_url", "")).strip()
        if mode not in ("local", "remote"):
            mode = "local"
        auto_start = _parse_bool(target_section.get("auto_start", False))
        return ClientConfig(mode=mode, remote_url=remote_url, auto_start=auto_start,
                            restore_timeout_seconds=float(target_section.get("restore_timeout_seconds", 3.0)))
    except Exception:
        return ClientConfig(mode="local")


def save_client_config(config: ClientConfig, config_path: Path | None = None) -> Path:
    """Persist client configuration to disk."""
    path = config_path or default_client_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_toml(), encoding="utf-8")
    return path


def resolve_server_target(
    settings: Settings,
    client_config: ClientConfig | None = None,
    *,
    override_mode: str | None = None,
    override_url: str | None = None,
    effective_port: int | None = None,
) -> ServerTarget:
    """Resolve the effective ServerTarget without hardcoded localhost literals."""
    cfg = client_config or load_client_config()
    mode = override_mode or cfg.mode
    if mode == "remote":
        url = override_url or cfg.remote_url
        if not url:
            raise TargetError("Remote mode requires a target URL in client config or via arguments")
        return ServerTarget.remote(url)

    # Local mode: resolve from settings and effective port
    port = effective_port or settings.port
    host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
    base_url = f"http://{host}:{port}"
    return ServerTarget.local(base_url)
