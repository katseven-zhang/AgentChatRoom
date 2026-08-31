from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import DomainError


@dataclass(frozen=True, slots=True)
class LocalMcpEnvironment:
    home_dir: Path
    appdata_dir: Path | None

    @classmethod
    def from_process(cls) -> "LocalMcpEnvironment":
        appdata = os.getenv("APPDATA", "").strip()
        return cls(
            home_dir=Path.home().resolve(),
            appdata_dir=Path(appdata).resolve() if appdata else None,
        )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _changed_fields(
    current: Any,
    proposed: Any,
    *,
    prefix: str = "mcpServers.agentchatroom",
) -> list[str]:
    if isinstance(current, Mapping) and isinstance(proposed, Mapping):
        changed: list[str] = []
        for key in sorted(set(current) | set(proposed)):
            child = f"{prefix}.{key}"
            if key not in current or key not in proposed:
                changed.append(child)
            else:
                changed.extend(
                    _changed_fields(current[key], proposed[key], prefix=child)
                )
        return changed
    if current != proposed:
        return [prefix]
    return []


class LocalMcpConfigurator:
    """Plan and safely apply verified local JSON MCP client configuration."""

    def __init__(
        self,
        environment: LocalMcpEnvironment | None = None,
        *,
        writable_check: Callable[[Path], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.environment = environment or LocalMcpEnvironment.from_process()
        self._writable_check = writable_check or self._default_writable_check
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _default_writable_check(path: Path) -> bool:
        return os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)

    def _candidate_paths(
        self, profile: Mapping[str, Any]
    ) -> list[tuple[str, Path]]:
        local_config = profile.get("local_config")
        if not isinstance(local_config, Mapping):
            return []
        candidates = local_config.get("candidates")
        if not isinstance(candidates, list):
            return []
        resolved: list[tuple[str, Path]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            root_name = str(candidate.get("root") or "").strip()
            parts = candidate.get("parts")
            if not isinstance(parts, list) or not all(
                isinstance(part, str) and part for part in parts
            ):
                continue
            if root_name == "home":
                root = self.environment.home_dir
            elif root_name == "appdata":
                root = self.environment.appdata_dir
            else:
                root = None
            if root is None:
                continue
            label = str(candidate.get("label") or profile.get("label") or "")
            resolved.append((label, root.joinpath(*parts).resolve()))
        return resolved

    @staticmethod
    def _proposed_server(profile: Mapping[str, Any]) -> dict[str, Any]:
        config = profile.get("config")
        if not isinstance(config, Mapping):
            raise DomainError(
                "local_mcp_profile_invalid",
                "The selected MCP profile does not contain structured JSON configuration",
                status_code=500,
            )
        servers = config.get("mcpServers")
        server = servers.get("agentchatroom") if isinstance(servers, Mapping) else None
        if not isinstance(server, Mapping):
            raise DomainError(
                "local_mcp_profile_invalid",
                "The selected MCP profile does not contain the agentchatroom server",
                status_code=500,
            )
        return copy.deepcopy(dict(server))

    @staticmethod
    def _load_json(content: bytes) -> dict[str, Any]:
        decoded = content.decode("utf-8-sig")
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict):
            raise ValueError("MCP configuration root must be a JSON object")
        servers = parsed.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            raise ValueError("mcpServers must be a JSON object")
        return parsed

    @staticmethod
    def _merge_server(
        current_server: Any, proposed_server: Mapping[str, Any]
    ) -> dict[str, Any]:
        merged_server = (
            copy.deepcopy(dict(current_server))
            if isinstance(current_server, Mapping)
            else {}
        )
        for key, value in proposed_server.items():
            if (
                key == "env"
                and isinstance(value, Mapping)
                and isinstance(merged_server.get("env"), Mapping)
            ):
                merged_environment = copy.deepcopy(dict(merged_server["env"]))
                merged_environment.update(copy.deepcopy(dict(value)))
                merged_server["env"] = merged_environment
            else:
                merged_server[key] = copy.deepcopy(value)
        return merged_server

    @staticmethod
    def _merge_config(
        current: Mapping[str, Any], proposed_server: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Any, dict[str, Any], bytes]:
        merged = copy.deepcopy(dict(current))
        servers = merged.setdefault("mcpServers", {})
        current_server = copy.deepcopy(servers.get("agentchatroom"))
        merged_server = LocalMcpConfigurator._merge_server(
            current_server, proposed_server
        )
        servers["agentchatroom"] = merged_server
        serialized = (
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        return merged, current_server, merged_server, serialized

    @staticmethod
    def _manual_plan(
        *,
        profile_id: str,
        profile: Mapping[str, Any],
        state: str,
        message: str,
        candidate_paths: list[tuple[str, Path]],
        detected_path: Path | None = None,
        detected_profile: str = "",
        configuration_state: str | None = None,
        current_sha256: str | None = None,
        changed_fields: list[str] | None = None,
        mode: str = "manual",
    ) -> dict[str, Any]:
        local_config = profile.get("local_config")
        reload_instruction = (
            str(local_config.get("reload_instruction") or "")
            if isinstance(local_config, Mapping)
            else ""
        )
        return {
            "schema_version": 1,
            "profile_id": profile_id,
            "client_label": str(profile.get("label") or profile_id),
            "detected_profile": detected_profile,
            "state": state,
            "configuration_state": configuration_state or state,
            "mode": mode,
            "managed_apply_available": False,
            "config_path": str(detected_path) if detected_path else None,
            "candidate_paths": [str(path) for _label, path in candidate_paths],
            "current_sha256": current_sha256,
            "proposed_sha256": None,
            "changed_fields": changed_fields or [],
            "message": message,
            "reload_instruction": reload_instruction,
            "configuration_written": state == "current",
            "connection_status": "not_verified",
            "connection_message": (
                "配置文件状态不等于 Room 连接状态；必须等待客户端 MCP 进程建立 Presence。"
            ),
        }

    def plan(
        self,
        *,
        deployment_profile: str,
        profile_id: str,
        profile: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if profile is None:
            raise DomainError(
                "local_mcp_profile_unknown",
                "The selected MCP client profile does not exist",
                status_code=404,
            )
        if str(profile.get("format") or "") != "json" or not isinstance(
            profile.get("local_config"), Mapping
        ):
            raise DomainError(
                "local_mcp_profile_unsupported",
                "This MCP client does not support managed local JSON configuration",
                status_code=404,
            )
        candidates = self._candidate_paths(profile)
        if deployment_profile != "local":
            return self._manual_plan(
                profile_id=profile_id,
                profile=profile,
                state="unavailable",
                message=(
                    "Only a local AgentChatRoom deployment may inspect or update client configuration."
                ),
                candidate_paths=candidates,
            )

        detected: tuple[str, Path] | None = None
        inaccessible: tuple[str, Path] | None = None
        for label, path in candidates:
            try:
                if path.is_file():
                    detected = (label, path)
                    break
            except OSError:
                inaccessible = inaccessible or (label, path)
        if detected is None:
            if inaccessible is not None:
                detected_profile, path = inaccessible
                return self._manual_plan(
                    profile_id=profile_id,
                    profile=profile,
                    state="unreadable",
                    message="The MCP configuration path cannot be inspected.",
                    candidate_paths=candidates,
                    detected_path=path,
                    detected_profile=detected_profile,
                    mode="assisted",
                )
            return self._manual_plan(
                profile_id=profile_id,
                profile=profile,
                state="missing",
                message=(
                    "No verified existing MCP configuration file was found; use the client UI or manual configuration."
                ),
                candidate_paths=candidates,
            )
        detected_profile, path = detected
        try:
            content = path.read_bytes()
        except OSError:
            return self._manual_plan(
                profile_id=profile_id,
                profile=profile,
                state="unreadable",
                message="The detected MCP configuration file cannot be read.",
                candidate_paths=candidates,
                detected_path=path,
                detected_profile=detected_profile,
                mode="assisted",
            )

        current_sha256 = _sha256(content)
        try:
            current = self._load_json(content)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self._manual_plan(
                profile_id=profile_id,
                profile=profile,
                state="invalid",
                message=(
                    "The detected MCP configuration is not valid JSON and was not modified."
                ),
                candidate_paths=candidates,
                detected_path=path,
                detected_profile=detected_profile,
                current_sha256=current_sha256,
                mode="assisted",
            )

        proposed_server = self._proposed_server(profile)
        _merged, current_server, merged_server, serialized = self._merge_config(
            current, proposed_server
        )
        changed = _changed_fields(current_server, merged_server)
        configuration_state = (
            "unconfigured"
            if current_server is None
            else "current"
            if not changed
            else "outdated"
        )
        writable = self._writable_check(path)
        if configuration_state != "current" and not writable:
            return self._manual_plan(
                profile_id=profile_id,
                profile=profile,
                state="unwritable",
                configuration_state=configuration_state,
                message=(
                    "The detected MCP configuration is not writable; no elevation was attempted."
                ),
                candidate_paths=candidates,
                detected_path=path,
                detected_profile=detected_profile,
                current_sha256=current_sha256,
                changed_fields=changed,
                mode="assisted",
            )

        local_config = profile["local_config"]
        return {
            "schema_version": 1,
            "profile_id": profile_id,
            "client_label": str(profile.get("label") or profile_id),
            "detected_profile": detected_profile,
            "state": configuration_state,
            "configuration_state": configuration_state,
            "mode": "managed_write",
            "managed_apply_available": configuration_state in {
                "unconfigured",
                "outdated",
            },
            "config_path": str(path),
            "candidate_paths": [str(candidate) for _label, candidate in candidates],
            "current_sha256": current_sha256,
            "proposed_sha256": _sha256(serialized),
            "changed_fields": changed,
            "message": {
                "current": "The agentchatroom MCP configuration is already current.",
                "unconfigured": "The file is valid and agentchatroom can be added safely.",
                "outdated": "The existing agentchatroom entry can be updated safely.",
            }[configuration_state],
            "reload_instruction": str(local_config.get("reload_instruction") or ""),
            "configuration_written": configuration_state == "current",
            "connection_status": "not_verified",
            "connection_message": (
                "配置文件状态不等于 Room 连接状态；必须等待客户端 MCP 进程建立 Presence。"
            ),
        }

    def apply(
        self,
        *,
        deployment_profile: str,
        profile_id: str,
        profile: Mapping[str, Any] | None,
        expected_current_sha256: str,
    ) -> dict[str, Any]:
        plan = self.plan(
            deployment_profile=deployment_profile,
            profile_id=profile_id,
            profile=profile,
        )
        if not plan["managed_apply_available"]:
            raise DomainError(
                "local_mcp_apply_unavailable",
                "The selected MCP configuration cannot be applied automatically",
                status_code=409,
                details={"state": plan["state"]},
            )
        if expected_current_sha256 != plan["current_sha256"]:
            raise DomainError(
                "local_mcp_config_changed",
                "The MCP configuration changed after preview; refresh before applying",
                status_code=409,
            )

        path = Path(str(plan["config_path"]))
        try:
            current_content = path.read_bytes()
        except OSError as error:
            raise DomainError(
                "local_mcp_apply_failed",
                "The MCP configuration could not be read for update",
                status_code=409,
            ) from error
        if _sha256(current_content) != expected_current_sha256:
            raise DomainError(
                "local_mcp_config_changed",
                "The MCP configuration changed after preview; refresh before applying",
                status_code=409,
            )

        try:
            current = self._load_json(current_content)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise DomainError(
                "local_mcp_config_changed",
                "The MCP configuration is no longer valid; refresh before applying",
                status_code=409,
            ) from error
        proposed_server = self._proposed_server(profile or {})
        _merged, _current_server, merged_server, serialized = self._merge_config(
            current, proposed_server
        )
        self._load_json(serialized)

        temp_path: Path | None = None
        timestamp = self._now().astimezone(timezone.utc).strftime(
            "%Y%m%d-%H%M%S-%f"
        )
        backup_path = path.with_name(
            f"{path.name}.agentchatroom-backup-{timestamp}"
        )
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.agentchatroom-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            self._load_json(temp_path.read_bytes())
            latest_content = path.read_bytes()
            if _sha256(latest_content) != expected_current_sha256:
                raise DomainError(
                    "local_mcp_config_changed",
                    "The MCP configuration changed during apply; refresh before retrying",
                    status_code=409,
                )
            with backup_path.open("xb") as backup:
                backup.write(latest_content)
                backup.flush()
                os.fsync(backup.fileno())
            os.replace(temp_path, path)
            temp_path = None
            updated = self._load_json(path.read_bytes())
            updated_server = updated.get("mcpServers", {}).get("agentchatroom")
            if updated_server != merged_server:
                raise DomainError(
                    "local_mcp_apply_failed",
                    "The MCP configuration failed post-write validation",
                    status_code=500,
                )
        except DomainError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise DomainError(
                "local_mcp_apply_failed",
                "The MCP configuration could not be updated safely",
                status_code=409,
            ) from error
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        updated_plan = self.plan(
            deployment_profile=deployment_profile,
            profile_id=profile_id,
            profile=profile,
        )
        return {
            "applied": True,
            "backup_path": str(backup_path),
            "reload_required": True,
            "plan": updated_plan,
        }
