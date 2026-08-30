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

# Client-specific details live at the integration boundary. The MCP server and
# its domain model remain vendor-neutral; adding a client only adds a profile.
# The "generic" profile covers every additional client that speaks the standard
# MCP JSON shape (`mcpServers` with command/args/env), so new clients need no
# core-domain branch to onboard.
MCP_CLIENT_PROFILES: dict[str, dict[str, str]] = {
    "workbuddy": {
        "label": "WorkBuddy",
        "vendor": "Tencent",
        "format": "json",
        "config_path_hint": "~/.workbuddy/mcp.json",
        "project_instruction_path_hint": ".workbuddy/memory/MEMORY.md",
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
        "config_path_hint": "使用 Trae 当前实际读取的 MCP JSON 配置文件",
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
  - `OBSERVE`: read-only inspection; call `room_join`, then `room_sync`, but do not claim tasks or acquire leases.
  - `COORDINATE`: repository changes or multi-Agent work; join and sync before work, use tasks and file leases, publish decisions or blockers, then submit evidence before declaring completion.
- Before inspecting or editing this repository in `OBSERVE` or `COORDINATE`, call `room_join`, keep the MCP/Bridge process alive, then call `room_sync`. Do not begin project work while disconnected.
- One installed Agent application is one durable software identity in this Project. The MCP configuration injects that identity; Agents must not supply, rename, or invent an `agent_key` for a task, review, or runtime check.
- Each software identity may have only one active Session. Reconnecting replaces the prior Session while preserving history and transferring unfinished owned work and active leases.
- `room_join.model` is required initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules.
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
    project: Mapping[str, Any],
    *,
    profile_id: str,
    profile: Mapping[str, Any],
    transport: str,
    config_text: str,
    project_instructions_text: str,
) -> str:
    """Build one complete prompt for one client profile and transport."""
    project_name = str(project.get("name", "")).strip() or "Project"
    root_path = str(project.get("root_path", "")).strip() or "<本机项目路径>"
    client_label = str(profile.get("label", profile_id)).strip() or profile_id
    config_path_hint = str(profile.get("config_path_hint", "")).strip()
    project_instruction_path_hint = str(
        profile.get("project_instruction_path_hint", "")
    ).strip()
    transport_details = {
        "local": {
            "label": "本机 stdio",
            "scope": "Agent 与 AgentChatRoom 中心运行在同一台电脑。",
            "project_path": root_path,
            "credential_note": "配置中的本机路径是当前运行环境值，不要改成远程地址。",
        },
        "http": {
            "label": "直接 HTTP MCP",
            "scope": "Agent 通过中心的 Streamable HTTP MCP 连接。",
            "project_path": "<当前 Agent 电脑上的项目路径>",
            "credential_note": (
                "把配置中的 <paste-issued-agent-token> 替换为该 Project 签发的 Agent Token；"
                "不要把 Token 写入项目文件或 Room 消息。"
            ),
        },
        "remote": {
            "label": "远程 Bridge",
            "scope": "Agent 电脑运行本地 stdio Bridge，再连接 AgentChatRoom 中心。",
            "project_path": "<当前 Agent 电脑上的项目路径>",
            "credential_note": (
                "把配置中的 <paste-issued-agent-token> 替换为该 Project 签发的 Agent Token；"
                "不要把 Token 写入项目文件或 Room 消息。"
            ),
        },
    }[transport]
    instruction_target = (
        f"建议项目规则文件：{project_instruction_path_hint}。"
        if project_instruction_path_hint
        else "把项目规则写入该客户端实际会读取的项目级指令或记忆文件；不要创建客户端不会读取的文件。"
    )

    return f"""请直接为当前工作空间完成 AgentChatRoom 接入，不要只解释步骤。

目标
- Project：{project_name}
- 目标客户端：{client_label}
- 连接方式：{transport_details['label']}
- 说明：{transport_details['scope']}

一、配置 MCP
1. 检查当前客户端已有的 MCP 配置，保留其他 MCP Server，只新增或更新名为 agentchatroom 的配置。
2. 建议配置位置：{config_path_hint or '请使用当前客户端实际支持的 MCP 配置文件'}。
3. 写入下面的配置：

{config_text.rstrip()}

4. {transport_details['credential_note']}
5. 如果客户端需要刷新 MCP、重启或新开对话才能加载工具，请明确告诉用户需要执行的动作；工具加载后继续完成下面步骤。

二、写入当前项目的协作规则
{instruction_target}

{project_instructions_text.rstrip() or '当前项目尚未生成协作规则，先停止并告知用户。'}

三、加入并同步 Room
工具加载后调用 room_join：
- project_path：{transport_details['project_path']}
- 软件身份：由 MCP 配置中的 {SOFTWARE_KEY_ENV_VAR}、{SOFTWARE_NAME_ENV_VAR} 和 {SOFTWARE_CLIENT_ENV_VAR} 注入；不要在任务、审核或运行检查中改名或创建新身份
- model：使用当前界面显示的模型标签；完全不可见时填 unknown
- role：只是本次 Session 的任务角色，例如 executor、reviewer 或 coordinator，不是新的 Agent 身份

不要另建 Room，也不要提交或推断 project_key。本机 stdio 会读取并校验 `.agentchatroom/project.json`，Project key 只由后端生成。只有当前规范化项目作用域在数据库和本地登记中都为空时，首个 Agent 才允许请求后端创建 Room。

room_join 成功后立即调用 room_sync。开始项目检查、修改或评审前都必须先同步；Task 状态由认领、工作报告和审核事件推进，不要手工声称实时 working/idle。

四、执行协作约定
- 发布 Agent 消息时填写本次回复真实的 model_display_name。
- 认领任务使用 task_claim；编辑文件前使用 lease_acquire，结束后释放租约。
- 完成工作使用 work_report 提交修改文件和测试证据；执行完成不等于独立验证通过。
- project_id、session_id、Session Token 和 cursor 只保存在当前运行时，不得写入项目文件。
- 本次工作结束后提交 Work Report；连接状态由 MCP 进程自动维护。"""


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
    command = str(Path(python_executable or sys.executable).resolve())
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

    environment.update(
        _profile_identity_environment("generic", MCP_CLIENT_PROFILES["generic"])
    )

    server = {
        "command": command,
        "args": ["-m", MCP_MODULE],
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
            "args": ["-m", MCP_MODULE],
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
        profiles[profile_id] = {
            **profile,
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
                    project,
                    profile_id=profile_id,
                    profile=profile,
                    transport="local",
                    config_text=config_text,
                    project_instructions_text=project_instructions_text,
                ),
                "http": build_onboarding_prompt(
                    project,
                    profile_id=profile_id,
                    profile=profile,
                    transport="http",
                    config_text=http_config_text,
                    project_instructions_text=project_instructions_text,
                ),
                "remote": build_onboarding_prompt(
                    project,
                    profile_id=profile_id,
                    profile=profile,
                    transport="remote",
                    config_text=remote_config_text,
                    project_instructions_text=project_instructions_text,
                ),
            }
        if profile_id == "workbuddy":
            profiles[profile_id]["project_memory_text"] = project_instructions_text
    return {
        "schema_version": 3,
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
