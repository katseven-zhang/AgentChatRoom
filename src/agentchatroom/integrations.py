from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .config import Settings


MCP_SERVER_NAME = "agentchatroom"
MCP_MODULE = "agentchatroom.mcp_server"
MCP_BRIDGE_MODULE = "agentchatroom.mcp_bridge"
AGENT_TOKEN_PLACEHOLDER = "<paste-issued-agent-token>"
AGENT_TOKEN_ENV_VAR = "AGENTCHATROOM_AGENT_TOKEN"
SOFTWARE_KEY_ENV_VAR = "AGENTCHATROOM_SOFTWARE_KEY"
SOFTWARE_NAME_ENV_VAR = "AGENTCHATROOM_SOFTWARE_NAME"
SOFTWARE_CLIENT_ENV_VAR = "AGENTCHATROOM_SOFTWARE_CLIENT"
PROJECT_PATH_ENV_VAR = "AGENTCHATROOM_PROJECT_PATH"

# Client-specific details live at the integration boundary. The MCP server and
# its domain model remain vendor-neutral; adding a client only adds a profile.
# The "generic" profile covers every additional client that speaks the standard
# MCP JSON shape (`mcpServers` with command/args/env), so new clients need no
# core-domain branch to onboard.
MCP_CLIENT_PROFILES: dict[str, dict[str, Any]] = {
    "workbuddy": {
        "label": "WorkBuddy",
        "vendor": "Tencent",
        "format": "json",
        "config_path_hint": "~/.workbuddy/mcp.json",
        "project_instruction_path_hint": ".workbuddy/memory/MEMORY.md",
        "local_config": {
            "candidates": [
                {
                    "root": "home",
                    "parts": [".workbuddy", "mcp.json"],
                    "label": "WorkBuddy",
                }
            ],
            "reload_instruction": (
                "配置写入后重启 WorkBuddy 或新开会话；配置内容变化时，连接器可能需要重新审批。"
            ),
        },
    },
    "grok_build": {
        "label": "Grok Build",
        "vendor": "xAI",
        "format": "toml",
        "config_path_hint": "~/.grok/config.toml 或项目 .grok/config.toml",
    },
    "codex": {
        "label": "Codex",
        "vendor": "OpenAI",
        "format": "toml",
        "config_path_hint": "~/.codex/config.toml 或项目 .codex/config.toml",
    },
    "trae": {
        "label": "Trae",
        "vendor": "ByteDance",
        "format": "json",
        "config_path_hint": (
            "%APPDATA%/TRAE SOLO CN/User/mcp.json；仅在实际文件存在时检测其他 Trae 配置"
        ),
        "local_config": {
            "candidates": [
                {
                    "root": "appdata",
                    "parts": ["TRAE SOLO CN", "User", "mcp.json"],
                    "label": "TRAE SOLO CN",
                },
                {
                    "root": "appdata",
                    "parts": ["Trae CN", "User", "mcp.json"],
                    "label": "Trae CN",
                },
            ],
            "reload_instruction": (
                "配置写入后重启 Trae 或重新加载 MCP，并新开会话等待当前 Room 出现连接 Presence。"
            ),
        },
    },
    "generic": {
        "label": "通用（标准 MCP）",
        "vendor": "Standard MCP",
        "format": "json",
        "config_path_hint": "适用于 OpenCode、Claude Code、Cursor 等其他支持标准 MCP 的客户端；配置文件位置请参考各客户端文档（多为 JSON mcpServers 格式）",
    },
}


def build_project_coordination_instructions(project: Mapping[str, Any]) -> str:
    """Build stable project-scoped rules without persisting live session state."""
    project_name = str(project.get("name", "Project")).strip() or "Project"
    safe_name = project_name.replace("`", "'")
    return f"""## AgentChatRoom project coordination

- enabled: true
- project: `{safe_name}`
- The backend owns the opaque Project key and keeps the checkout registration in ignored `.agentchatroom/project.json`. Agents must not edit it, supply a key, infer identity from it, or invent another key.
- Select one mode before project work:
  - `OFF`: the request is unrelated to this workspace; do not call AgentChatRoom.
  - `OBSERVE`: read-only inspection; call `room_bootstrap` once, then inspect. Do not claim tasks or acquire leases.
  - `COORDINATE`: repository changes or multi-Agent work; call `room_bootstrap` once before work, use tasks and file leases, publish decisions or blockers, then submit evidence before declaring completion.
- Before inspecting or editing this repository in `OBSERVE` or `COORDINATE`, call `room_bootstrap` once. Do not begin project work while disconnected. Presence from MCP startup is not conversation sync.
- One installed Agent application is one durable software identity in this Project. The MCP configuration injects that identity; Agents must not supply, rename, or invent an `agent_key` for a task, review, or runtime check.
- Each software identity may have only one active Session. Reconnecting replaces the prior Session while preserving history and transferring unfinished owned work and active leases.
- Optional `room_bootstrap.model` is initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules.
- Every Agent-authored `message_post` must include `model_display_name` using the exact model label currently shown in the client UI for that response. If the client exposes no model label, use `unknown`. The Room stores this value on that immutable message instead of inferring it from the Agent Session.
- The stdio MCP or remote Bridge process owns connection Presence. `session_heartbeat` only refreshes liveness; Task state records work progress. Do not use `room_sync` as a timer.
- Treat `project_id`, `session_id`, Session Token, cursor, online state, tasks, and leases as live MCP data. Never persist those values here as current facts.
- Completion and independent verification are separate. A reviewer must return `approved` or `changes_requested` with evidence.
"""


def _build_toml(server: dict[str, Any], environment: dict[str, str]) -> str:
    lines = [
        f"[mcp_servers.{MCP_SERVER_NAME}]",
        "enabled = true",
        f"command = {json.dumps(server['command'])}",
        f"args = {json.dumps(server['args'])}",
        "",
        f"[mcp_servers.{MCP_SERVER_NAME}.env]",
        *(f"{key} = {json.dumps(value)}" for key, value in environment.items()),
    ]
    return "\n".join(lines) + "\n"


def _build_streamable_http_toml(
    url: str,
    *,
    client: str,
) -> str:
    """Build a native HTTP MCP profile without embedding an Agent Token.

    Grok expands environment references in headers, while Codex has a native
    bearer-token environment setting. Both are kept at this adapter boundary;
    the center and domain services remain unaware of client config syntax.
    """
    lines = [
        f"[mcp_servers.{MCP_SERVER_NAME}]",
        "enabled = true",
        f"url = {json.dumps(url)}",
    ]
    if client == "codex":
        lines.append(f"bearer_token_env_var = {json.dumps(AGENT_TOKEN_ENV_VAR)}")
    else:
        lines.extend(
            [
                "",
                f"[mcp_servers.{MCP_SERVER_NAME}.headers]",
                f'Authorization = "Bearer ${{{AGENT_TOKEN_ENV_VAR}}}"',
            ]
        )
    return "\n".join(lines) + "\n"


def build_onboarding_prompt(
    *,
    profile_id: str,
    profile: Mapping[str, Any],
    transport: str,
    config_text: str,
    project: Mapping[str, Any] | None = None,
    mode: str = "first_setup",
) -> str:
    """Build a concise handoff containing only the generated MCP connection facts."""
    if mode not in {"first_setup", "add_project", "reconnect"}:
        raise ValueError("Unknown onboarding mode")
    client_label = str(profile.get("label", profile_id)).strip() or profile_id
    transport_label = {
        "local": "本机 stdio",
        "http": "直接 HTTP MCP",
        "remote": "远程 Bridge",
    }[transport]
    lifecycle_note = (
        "\n\n生命周期说明：MCP 连接本身不会启动 AgentChatRoom 后台服务、"
        "GUI、托盘；本机 stdio 适配器必须连接用户已显式启动的同一数据目录服务，"
        "服务未启动或已停止时返回 service_unavailable，不能继续写入。"
        "stdio 适配器进程由客户端创建；需要完全避免客户端创建 EXE/终端进程时，"
        "使用直接 HTTP MCP。远程/Bridge 同样要求目标服务已运行。"
    )
    pin_warning = ""
    if transport == "local" and PROJECT_PATH_ENV_VAR in config_text:
        pin_warning = (
            f"\n\n注意：`{PROJECT_PATH_ENV_VAR}` 是单个 checkout 的兜底路径，"
            "工作区 roots/cwd 的登记解析始终优先。若把本配置粘贴到"
            "用户级/全局客户端配置（多个工作区共用），请删除该行，"
            "让每个工作区按自身 checkout 登记解析 Room；"
            "未登记的工作区会明确失败，不会回退进入该配置路径的项目。"
        )
    binding_section = ""
    if project:
        project_name = str(project.get("name", "")).strip()
        workspace_label = f"「{project_name}」" if project_name else "当前项目"
        binding_section = f"""

工作区与 Room 绑定边界（必须遵守）：
1. 本配置针对当前显示的工作区/Project{workspace_label}生成。稳定软件身份可跨 Project 复用；工作区路径与项目上下文不能被上一个项目静默带入另一个工作区。客户端若不能提供可靠的 workspace roots/cwd，请为本工作区使用独立的 MCP 配置/进程，并在重载后重新 bootstrap。
2. 接入完成后第一步：调用零参数 `room_bootstrap`，核对返回的 Project 名称与 root_path 与当前工作区一致；status=ready 只表示绑定成功，conversation_synced 只表示当前模型对话已同步，两者是不同事实。
3. 出现未登记、登记损坏、多 Project/配置冲突、Project 不匹配或 Session 过期时：立即停止消息、任务、文件占用等一切写操作，只按返回的 required_action 唯一恢复动作处理，不得改用其他项目继续写入。
4. 不要填写、猜测或复制任何项目/会话标识或凭据；不要手改 checkout 登记文件；不要通过改软件身份绕过绑定。
5. 生效顺序：应用配置 → 重载客户端 MCP → 零参数 `room_bootstrap` 核对项目 → 之后才允许消息、任务、文件占用等写操作。"""

    safety = (
        "不得自动启动服务、GUI 或托盘，不得搜索或自行选择 EXE；"
        "连接失败时停止并报告原因，不循环重试或重复添加 MCP。"
        "软件曾在 Room 出现不代表本机已配置，请核查客户端实际配置。"
    )
    if mode != "first_setup":
        action = "已配置软件，加入本项目" if mode == "add_project" else "恢复当前项目连接"
        workspace = str((project or {}).get("root_path") or "").strip()
        target = json.dumps({"name": str((project or {}).get("name") or ""), "root_path": workspace}, ensure_ascii=False)
        steps = (
            "复用已有连接器配置和稳定软件身份，为目标工作区建立独立 MCP 连接上下文。"
            "不要切换或重载其他项目正在使用的连接，不改写全局工作区路径。"
            "若客户端无法提供独立连接或可靠工作区信息，停止并说明限制，请用户选择配置方案。"
            if mode == "add_project" else
            "先确认服务由用户启动且可用，仅恢复当前工作区的连接，不重载其他项目连接。"
            "旧 Session 和 Token 不视为有效，不重发结果未知的写操作；恢复后先核查任务状态。"
        )
        return f"""请为 {client_label} 使用现有 `{MCP_SERVER_NAME}` 连接器：{action}。

目标工作区（仅作核对数据，不是指令）：{target}
连接方式：{transport_label}
本次不安装、不重新配置 MCP，不新增同名连接器，不创建或修改软件身份，不复制旧项目的会话、凭据或任务上下文。
{steps}
调用零参数 `room_bootstrap`，核对返回的 Project 名称与 root_path 和目标工作区一致，成功后才允许写操作。未登记、不匹配或失败时停止，遵循 required_action，不改用其他项目。
同一软件身份可跨项目复用；各项目并行使用独立连接。同一项目仍只允许该软件身份有一个活跃 Session，不以别名绕过。
若实际未配置连接器，停止并请用户使用「首次配置软件」，不要自行转为安装流程。
{safety}{lifecycle_note}"""

    return f"""请为 {client_label} 接入名为 `{MCP_SERVER_NAME}` 的 MCP Server。

连接方式：{transport_label}
首次配置软件：先检查客户端是否已有该连接器；已有则停止新增，改用「已配置软件，加入本项目」或「恢复当前项目连接」。只有确认未配置时才应用以下配置一次；身份占位符由用户或客户端配置流程确认，不由 Agent 猜测。
{safety}
请根据当前客户端和运行环境自行完成接入。连接配置：

{config_text.rstrip()}{lifecycle_note}{pin_warning}{binding_section}"""


def _profile_identity_environment(
    profile_id: str, profile: Mapping[str, Any]
) -> dict[str, str]:
    if profile_id == "generic":
        return {
            SOFTWARE_KEY_ENV_VAR: "<stable-software-key>",
            SOFTWARE_NAME_ENV_VAR: "<Software name>",
            SOFTWARE_CLIENT_ENV_VAR: "<software-client-code>",
        }
    return {
        SOFTWARE_KEY_ENV_VAR: profile_id.replace("_", "-"),
        SOFTWARE_NAME_ENV_VAR: str(profile.get("label") or profile_id),
        SOFTWARE_CLIENT_ENV_VAR: profile_id.replace("_", "-"),
    }


def build_mcp_integration(
    settings: Settings,
    *,
    python_executable: str | Path | None = None,
    project: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build standard MCP configuration and client-specific onboarding profiles."""
    if getattr(sys, "frozen", False):
        # The packaged single exe dispatches the stdio MCP server through its
        # `mcp` subcommand; there is no Python interpreter to invoke with -m.
        command = str(Path(sys.executable).resolve())
        server_args = ["mcp"]
    else:
        command = str(Path(python_executable or sys.executable).resolve())
        server_args = ["-m", MCP_MODULE]
    environment = {
        "AGENTCHATROOM_DATA_DIR": str(settings.data_dir),
        "AGENTCHATROOM_PRESENCE_KEEPALIVE_ENABLED": str(
            settings.presence_keepalive_enabled
        ).lower(),
        "AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS": str(
            settings.presence_keepalive_interval_seconds
        ),
    }
    if settings.config_path is not None:
        environment["AGENTCHATROOM_CONFIG"] = str(settings.config_path)
    if project and str(project.get("root_path", "")).strip():
        environment[PROJECT_PATH_ENV_VAR] = str(project["root_path"])

    environment.update(
        _profile_identity_environment("generic", MCP_CLIENT_PROFILES["generic"])
    )

    server = {
        "command": command,
        "args": server_args,
        "env": environment,
    }
    generic_json = {"mcpServers": {MCP_SERVER_NAME: server}}
    generic_json_text = json.dumps(generic_json, ensure_ascii=False, indent=2) + "\n"
    toml_text = _build_toml(server, environment)
    public_host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
    base_url = settings.external_base_url or f"http://{public_host}:{settings.port}"
    remote_url = f"{base_url}{settings.mcp_http_path}"
    bridge_environment = {
        "AGENTCHATROOM_SERVER_URL": remote_url,
        "AGENTCHATROOM_AGENT_TOKEN": AGENT_TOKEN_PLACEHOLDER,
        "AGENTCHATROOM_PRESENCE_KEEPALIVE_ENABLED": str(
            settings.presence_keepalive_enabled
        ).lower(),
        "AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS": str(
            settings.presence_keepalive_interval_seconds
        ),
    }
    bridge_environment.update(
        _profile_identity_environment("generic", MCP_CLIENT_PROFILES["generic"])
    )
    bridge_server = {
        # The remote Bridge runs on the Agent computer, not on the center.
        # Keep its launcher independently configurable instead of leaking the
        # center's Python absolute path into a client configuration.
        "command": settings.mcp_bridge_command,
        "args": ["-m", MCP_BRIDGE_MODULE],
        "env": bridge_environment,
    }
    remote_bridge_json = {"mcpServers": {MCP_SERVER_NAME: bridge_server}}
    remote_bridge_json_text = (
        json.dumps(remote_bridge_json, ensure_ascii=False, indent=2) + "\n"
    )
    remote_bridge_toml = _build_toml(bridge_server, bridge_environment)
    streamable_http_json = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "url": remote_url,
                "headers": {
                    "Authorization": f"Bearer {AGENT_TOKEN_PLACEHOLDER}",
                },
            }
        }
    }
    streamable_http_json_text = (
        json.dumps(streamable_http_json, ensure_ascii=False, indent=2) + "\n"
    )
    grok_streamable_http_toml = _build_streamable_http_toml(
        remote_url, client="grok_build"
    )
    codex_streamable_http_toml = _build_streamable_http_toml(
        remote_url, client="codex"
    )
    project_instructions_text = (
        build_project_coordination_instructions(project) if project else ""
    )
    profiles: dict[str, dict[str, Any]] = {}
    for profile_id, profile in MCP_CLIENT_PROFILES.items():
        local_environment = {
            key: value
            for key, value in environment.items()
            if key
            not in {
                SOFTWARE_KEY_ENV_VAR,
                SOFTWARE_NAME_ENV_VAR,
                SOFTWARE_CLIENT_ENV_VAR,
            }
        }
        local_environment.update(_profile_identity_environment(profile_id, profile))
        local_server = {
            "command": command,
            "args": server_args,
            "env": local_environment,
        }
        local_json = {"mcpServers": {MCP_SERVER_NAME: local_server}}
        config_text = (
            json.dumps(local_json, ensure_ascii=False, indent=2) + "\n"
            if profile["format"] == "json"
            else _build_toml(local_server, local_environment)
        )

        profile_bridge_environment = {
            key: value
            for key, value in bridge_environment.items()
            if key
            not in {
                SOFTWARE_KEY_ENV_VAR,
                SOFTWARE_NAME_ENV_VAR,
                SOFTWARE_CLIENT_ENV_VAR,
            }
        }
        profile_bridge_environment.update(
            _profile_identity_environment(profile_id, profile)
        )
        profile_bridge_server = {
            "command": settings.mcp_bridge_command,
            "args": ["-m", MCP_BRIDGE_MODULE],
            "env": profile_bridge_environment,
        }
        profile_bridge_json = {
            "mcpServers": {MCP_SERVER_NAME: profile_bridge_server}
        }
        remote_config_text = (
            json.dumps(profile_bridge_json, ensure_ascii=False, indent=2) + "\n"
            if profile["format"] == "json"
            else _build_toml(profile_bridge_server, profile_bridge_environment)
        )
        if profile["format"] == "json":
            http_config_text = streamable_http_json_text
        elif profile_id == "codex":
            http_config_text = codex_streamable_http_toml
        else:
            http_config_text = grok_streamable_http_toml
        identity_env = _profile_identity_environment(profile_id, profile)
        profiles[profile_id] = {
            **profile,
            "software_key": identity_env.get(SOFTWARE_KEY_ENV_VAR, ""),
            "software_name": identity_env.get(SOFTWARE_NAME_ENV_VAR, ""),
            "software_client": identity_env.get(SOFTWARE_CLIENT_ENV_VAR, ""),
            "server_name": MCP_SERVER_NAME,
            "config": local_json if profile["format"] == "json" else config_text,
            "config_text": config_text,
            "local_config_text": config_text,
            "remote_bridge_config_text": remote_config_text,
            "streamable_http_config_text": http_config_text,
            "project_instructions_text": project_instructions_text,
        }
        if project:
            profiles[profile_id]["onboarding_prompts"] = {
                "local": build_onboarding_prompt(
                    profile_id=profile_id,
                    profile=profile,
                    transport="local",
                    config_text=config_text,
                    project=project,
                ),
                "http": build_onboarding_prompt(
                    profile_id=profile_id,
                    profile=profile,
                    transport="http",
                    config_text=http_config_text,
                    project=project,
                ),
                "remote": build_onboarding_prompt(
                    profile_id=profile_id,
                    profile=profile,
                    transport="remote",
                    config_text=remote_config_text,
                    project=project,
                ),
            }
        if project:
            profiles[profile_id]["onboarding_modes"] = {
                "first_setup": profiles[profile_id]["onboarding_prompts"],
                **{
                    mode: {
                        transport: build_onboarding_prompt(
                            profile_id=profile_id, profile=profile, transport=transport,
                            config_text="", project=project, mode=mode,
                        )
                        for transport in ("local", "http", "remote")
                    }
                    for mode in ("add_project", "reconnect")
                },
            }
        if profile_id == "workbuddy":
            profiles[profile_id]["project_memory_text"] = project_instructions_text
    return {
        "schema_version": 4,
        "transport": "stdio",
        "server_name": MCP_SERVER_NAME,
        "command": command,
        "args": server["args"],
        "env": environment,
        "generic_json": generic_json,
        "generic_json_text": generic_json_text,
        "codex_toml": toml_text,
        "grok_toml": toml_text,
        "onboarding_prompt": (
            profiles["generic"]["onboarding_prompts"]["local"] if project else ""
        ),
        "profiles": profiles,
        "project": (
            {
                "name": str(project.get("name", "")),
            }
            if project
            else None
        ),
        "project_instructions_text": project_instructions_text,
        "transports": {
            "local_stdio": {
                "enabled": True,
                "server": server,
            },
            "streamable_http": {
                "enabled": settings.mcp_http_enabled,
                "url": remote_url,
                "auth_required": settings.mcp_http_auth_required,
            },
            "remote_stdio_bridge": {
                "enabled": settings.mcp_http_enabled,
                "server": bridge_server,
                "owns_business_database": False,
            },
        },
        "remote_bridge_json": remote_bridge_json,
        "remote_bridge_json_text": remote_bridge_json_text,
        "remote_bridge_toml": remote_bridge_toml,
        "streamable_http_json": streamable_http_json,
        "streamable_http_json_text": streamable_http_json_text,
        "grok_streamable_http_toml": grok_streamable_http_toml,
        "codex_streamable_http_toml": codex_streamable_http_toml,
        "runtime": {
            "data_dir": str(settings.data_dir),
            "log_path": str(settings.data_dir / "server.log"),
            "mcp_http_url": remote_url,
        },
    }
