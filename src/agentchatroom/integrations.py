from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .config import Settings
from .http_identity import encode_http_identity_value


MCP_SERVER_NAME = "agentchatroom"
MCP_MODULE = "agentchatroom.mcp_server"
MCP_BRIDGE_MODULE = "agentchatroom.mcp_bridge"
AGENT_TOKEN_PLACEHOLDER = "<paste-issued-project-credential-bundle>"
AGENT_TOKEN_ENV_VAR = "AGENTCHATROOM_AGENT_TOKEN"
SOFTWARE_KEY_ENV_VAR = "AGENTCHATROOM_SOFTWARE_KEY"
SOFTWARE_NAME_ENV_VAR = "AGENTCHATROOM_SOFTWARE_NAME"
SOFTWARE_CLIENT_ENV_VAR = "AGENTCHATROOM_SOFTWARE_CLIENT"
PROJECT_PATH_ENV_VAR = "AGENTCHATROOM_PROJECT_PATH"
SOFTWARE_KEY_HEADER = "X-AgentChatRoom-Software-Key"
SOFTWARE_NAME_HEADER = "X-AgentChatRoom-Software-Name"
SOFTWARE_CLIENT_HEADER = "X-AgentChatRoom-Software-Client"

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
    safe_name = (
        project_name.replace("`", "'").replace("\r", "\\r").replace("\n", "\\n")
    )
    bootstrap_name = json.dumps(project_name, ensure_ascii=False)
    return f"""## AgentChatRoom project coordination

- enabled: true
- Project name: `{safe_name}`. Treat this exact name as the Room identity for this workspace; do not select a similarly named Project.
- The backend owns the opaque Project key and keeps the checkout registration in ignored `.agentchatroom/project.json`. Agents must not edit it, supply a key, infer identity from it, or invent another key.
- Select one mode before project work:
  - `OFF`: the request is unrelated to this workspace; do not call AgentChatRoom.
  - `OBSERVE`: read-only inspection; bootstrap and sync, but do not claim tasks, acknowledge assignments, acquire leases, or write Room state.
  - `COORDINATE`: repository changes or delegated/review work; follow the workflow below and keep Room state current.
- Before project work, separate three facts and compare them: the workspace this Agent conversation is actually running in, the target project the user asked you to work on, and the Room Project this Session is bound to. A matching Room binding never substitutes for a matching conversation workspace; the user may have started or continued this conversation in the wrong project workspace.
- If the requested target project belongs to a different workspace than this conversation, stop and do not execute that work in this conversation: do not claim or acknowledge its tasks, do not acquire leases, and do not read or modify its files. Tell the user plainly which workspace this conversation is in and which project was requested, then ask them to switch to the correct project workspace in the Agent client and continue or start a new conversation there. When the requested target is unclear, ask for clarification first; never guess a project identity.
- Never resolve a workspace/project mismatch by rebinding the Room Session, switching the Room Project, changing the working directory, or operating on another directory. Rebinding the Room cannot change the workspace this conversation already loaded, and being bound to the Room Project of the current workspace is not authorization to work on a different requested project.
- At the start of every new Agent conversation in this workspace, call `room_bootstrap(project_name={bootstrap_name})` once. Verify the returned Project name is exactly `{safe_name}` and the returned workspace/root matches this checkout. Do not begin project work while disconnected. If bootstrap fails or selects another Project, stop all Room writes and follow only its `required_action`.
- After bootstrap, call `room_sync` to read recent messages and current coordination state. Inspect targeted messages, assigned tasks, handoffs, and tasks awaiting independent review before starting uncoordinated work. Use `room_sync` again after a disconnection or when fresh Room state is required; do not poll it as a timer.
- For an assigned task, inspect it with `task_get` or `task_get_by_number`, then use `task_acknowledge`. Use `task_claim` for an unowned eligible task. If this conversation must resume unfinished work left by a disconnected Session of the same software identity, call `task_claim(reclaim=true)` explicitly; reclaim is rejected while the owner is connected or belongs to another identity. Before editing shared files, acquire an appropriate `lease_acquire` path lease; release it with `lease_release` when the work or handoff ends.
- After this conversation is summarised, compacted, or restarted, call `room_bootstrap` again and read `connection.room_session`: `restored` means the same Room Session and unchanged task ownership, while `created` means a new Session that inherits no task. Never fabricate the previous binding — an explicit `project_id`, `session_id`, or `token` that differs from the current runtime binding is rejected (`runtime_context_mismatch`), and bootstrap itself is not proof that task ownership was recovered.
- Resuming work owned by a disconnected Session of your identity is bounded and explicit: `task_claim(reclaim=true)` succeeds once the owner is treated as disconnected (its transport is gone, or the heartbeat window lapses — follow the error's `required_action` and `retry_after_seconds`). While the owner is still connected it is rejected as `task_owner_session_connected`, and another software identity is always rejected (`task_reclaim_forbidden`). Do not try to bypass this with `task_update(status=todo)`.
- Only the owner's own credentials, or an authenticated management request, may release, reassign, or rewrite a task. A missing Session credential is not management authority: `task_update` / `task_release` / `task_assign` without owner credentials and without management authorization are rejected (`management_auth_required`). Never copy another Session's token, guess an identity, or use a management-only entry point to take over work.
- Post concise progress, decisions, and blockers with `message_post`; acknowledge targeted requests with `message_acknowledge` when appropriate. Every Agent-authored message must include `model_display_name` using the exact model label shown by the client, or `unknown` when the client exposes none.
- Keep task state accurate with `task_update`. When implementation is ready, submit `work_report` with changed files, checks, and concrete evidence. A Work Report requests verification; it does not complete independent review.
- When accepting a review task, first confirm the implementation was produced by a different software identity. Read the task contract and Work Report, inspect the actual diff/artifacts, run independent checks, then call `review_submit` with `approved` or `changes_requested` and criterion-level evidence. The implementing identity may not approve its own work. Use `integration_submit` only after required verification and integration are actually complete.
- Use the MCP tool schemas and structured error `required_action` as the protocol contract. Do not inspect AgentChatRoom source code or guess IDs/arguments to discover how the Room works. If a required tool is unavailable, stop and report that connection/tooling problem.
- One installed Agent application is one durable software identity in this Project. The MCP configuration injects that identity; Agents must not supply, rename, or invent an `agent_key` for a task, review, or runtime check.
- A software identity may keep multiple active Sessions in the same or different Projects, including parallel conversations. Each Session keeps its own task and lease ownership; joining a new Session never closes another Session or silently transfers work.
- Optional `room_bootstrap.model` is initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules.
- MCP connection Presence and task progress are different facts. `session_heartbeat` only refreshes connection liveness; Task events record claimed, in-progress, blocked, reported, reviewed, and completed work. Do not use heartbeats to represent task progress.
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


def _streamable_http_identity_headers(identity_env: Mapping[str, str]) -> dict[str, str]:
    return {
        SOFTWARE_KEY_HEADER: encode_http_identity_value(
            str(identity_env.get(SOFTWARE_KEY_ENV_VAR) or "<stable-software-key>")
        ),
        SOFTWARE_NAME_HEADER: encode_http_identity_value(
            str(identity_env.get(SOFTWARE_NAME_ENV_VAR) or "<Software name>")
        ),
        SOFTWARE_CLIENT_HEADER: encode_http_identity_value(
            str(
                identity_env.get(SOFTWARE_CLIENT_ENV_VAR)
                or "<software-client-code>"
            )
        ),
    }


def _build_streamable_http_toml(
    url: str,
    *,
    client: str,
    identity_env: Mapping[str, str] | None = None,
) -> str:
    """Build a native HTTP MCP profile without embedding an Agent Token.

    Grok expands environment references in headers, while Codex has a native
    bearer-token environment setting. Both are kept at this adapter boundary;
    the center and domain services remain unaware of client config syntax.
    """
    identity_headers = _streamable_http_identity_headers(identity_env or {})
    lines = [
        f"[mcp_servers.{MCP_SERVER_NAME}]",
        "enabled = true",
        f"url = {json.dumps(url)}",
    ]
    if client == "codex":
        lines.append(f"bearer_token_env_var = {json.dumps(AGENT_TOKEN_ENV_VAR)}")
        lines.extend(
            [
                "",
                f"[mcp_servers.{MCP_SERVER_NAME}.http_headers]",
                *(
                    f"{key} = {json.dumps(value)}"
                    for key, value in identity_headers.items()
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"[mcp_servers.{MCP_SERVER_NAME}.headers]",
                f'Authorization = "Bearer ${{{AGENT_TOKEN_ENV_VAR}}}"',
                *(
                    f"{key} = {json.dumps(value)}"
                    for key, value in identity_headers.items()
                ),
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
    """Build the minimal 3-step handoff: write config -> reload MCP -> bootstrap.

    #143：接入提示词只保留行动必需信息（配置块、服务器名、目标 Project、
    bootstrap 调用与凭据安全底线）；生命周期原理、会话过期说教与绑定
    边界细则由项目规则（AGENTS.md）与服务端 required_action 承担。
    """
    if mode not in {"first_setup", "add_project", "reconnect", "migrate_http"}:
        raise ValueError("Unknown onboarding mode")
    client_label = str(profile.get("label", profile_id)).strip() or profile_id
    transport_label = {
        "local": "本机 stdio",
        "http": "直接 HTTP MCP",
        "remote": "远程 Bridge",
    }[transport]
    project_name = str((project or {}).get("name") or "").strip()
    root_path = str((project or {}).get("root_path") or "").strip()
    bootstrap_call = (
        f"`room_bootstrap(project_name={json.dumps(project_name, ensure_ascii=False)})`"
        if transport == "http"
        else "`room_bootstrap`（零参数）"
    )
    verify_step = (
        f"3. 调用 {bootstrap_call}，核对返回的 Project 名称与 root_path 与当前工作区一致；"
        "核对通过后才执行消息、任务、文件占用等写操作。"
        if project
        else f"3. 调用 {bootstrap_call} 完成连接核对。"
    )
    reload_step = "2. 配置写入后，重启或重新加载客户端 MCP，使新配置生效。"
    security_line = "不要把 Token 发布到 Room、日志或仓库；失败时停止并报告原因，不循环重试。"
    unavailable_line = (
        "AgentChatRoom 服务由用户显式启动；未启动时调用会返回 service_unavailable，"
        "此时停止并报告，不要自动启动服务或搜索 EXE。"
    )

    if mode == "reconnect":
        return (
            f"恢复 {client_label} 与当前 Project 的连接（{transport_label}），"
            "不重新签发或改写 MCP 配置。\n\n"
            f"目标 Project：{project_name}\n\n"
            "1. 确认 AgentChatRoom 服务已由用户启动且可用。\n"
            f"2. 直接调用 {bootstrap_call} 核对返回的 Project 名称与 root_path。\n"
            "3. 连接恢复后再继续原任务；失败时停止并按 required_action 处理。\n\n"
            f"{security_line}"
        )

    if mode == "add_project":
        target_project_line = (
            f"目标 Project：{project_name}（本地工作区根目录 root_path: {root_path}）"
            if root_path
            else f"目标 Project：{project_name}"
        )
        step3_call = (
            f"3. 在目标 Project 本地工作区（{root_path}）中调用 {bootstrap_call}，核对返回的 Project 名称与 root_path 与当前工作区一致。"
            if root_path
            else f"3. 调用 {bootstrap_call}，核对返回的 Project 名称与 root_path 与当前工作区一致。"
        )
        merge_steps = (
            "1. 在客户端本地现有 `agentchatroom` HTTP 配置中，保留原 url、软件身份三字段与"
            "全部旧 Project 凭据，仅把本次签发的 `project_name_N` / `project_token_N` 追加或"
            "替换进 Authorization 凭据包（`acrb.v1.*`），写回同一个 agentchatroom 条目；"
            "不新建第二个 agentchatroom 连接器。\n"
            "2. 保存后重启或重新加载客户端 MCP。\n"
            f"{step3_call}"
        )
        return (
            f"{client_label} 已配置过 agentchatroom HTTP MCP：把本次签发的新 Project 凭据"
            f"增量合并进现有配置（{transport_label}）。\n\n"
            f"{target_project_line}\n\n"
            f"{merge_steps}\n\n"
            "无法读取本地配置时停止并向用户报告，不要凭空重建配置。\n"
            f"{security_line}"
        )

    if mode == "migrate_http":
        config_hint = str(profile.get("config_path_hint") or "客户端 MCP 设置")
        return (
            f"把 {client_label} 的 agentchatroom 连接器从本机 stdio 切换为直接 HTTP MCP。\n\n"
            f"1. 用下面的 HTTP 配置替换客户端中名为 `{MCP_SERVER_NAME}` 的旧条目"
            f"（配置位置：{config_hint}），删除 `command`、`args`、`cwd`、`env` 等 stdio 字段：\n\n"
            f"{config_text.rstrip()}\n\n"
            f"{reload_step}\n"
            "3. 调用 `room_bootstrap`，核对返回的 Project 名称与 root_path 与目标工作区一致；"
            "HTTP 稳定后再移除旧 stdio 备份，稳定软件身份保持不变。\n\n"
            f"{unavailable_line}\n{security_line}"
        )

    placeholder_note = ""
    if "<" in config_text and ">" in config_text:
        placeholder_note = (
            "配置中的软件身份占位符必须替换为用户确认的实际接入端显示名称"
            "（如 Hermes、Grok）；接入格式标签与占位符原文都不能充当名称。\n"
        )
    pin_note = ""
    if transport == "local" and PROJECT_PATH_ENV_VAR in config_text:
        pin_note = (
            f"若把该配置放进多工作区共用的用户级客户端配置，删除 "
            f"`{PROJECT_PATH_ENV_VAR}` 行，让每个工作区按自身 checkout 解析。\n"
        )
    return (
        f"请为 {client_label} 接入 AgentChatRoom MCP Server `{MCP_SERVER_NAME}`（{transport_label}）。\n\n"
        f"{placeholder_note}"
        "1. 将以下配置写入客户端（首次配置只应用一次）：\n\n"
        f"{config_text.rstrip()}\n\n"
        f"{reload_step}\n"
        f"{verify_step}\n"
        f"{pin_note}"
        f"{unavailable_line}\n{security_line}"
    )
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
    generic_identity = _profile_identity_environment(
        "generic", MCP_CLIENT_PROFILES["generic"]
    )
    streamable_http_json = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "url": remote_url,
                "headers": {
                    "Authorization": f"Bearer {AGENT_TOKEN_PLACEHOLDER}",
                    **_streamable_http_identity_headers(generic_identity),
                },
            }
        }
    }
    streamable_http_json_text = (
        json.dumps(streamable_http_json, ensure_ascii=False, indent=2) + "\n"
    )
    grok_streamable_http_toml = _build_streamable_http_toml(
        remote_url,
        client="grok_build",
        identity_env=_profile_identity_environment(
            "grok_build", MCP_CLIENT_PROFILES["grok_build"]
        ),
    )
    codex_streamable_http_toml = _build_streamable_http_toml(
        remote_url,
        client="codex",
        identity_env=_profile_identity_environment(
            "codex", MCP_CLIENT_PROFILES["codex"]
        ),
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
        identity_env = _profile_identity_environment(profile_id, profile)
        if profile["format"] == "json":
            profile_http_json = {
                "mcpServers": {
                    MCP_SERVER_NAME: {
                        "url": remote_url,
                        "headers": {
                            "Authorization": f"Bearer {AGENT_TOKEN_PLACEHOLDER}",
                            **_streamable_http_identity_headers(identity_env),
                        },
                    }
                }
            }
            http_config_text = (
                json.dumps(profile_http_json, ensure_ascii=False, indent=2) + "\n"
            )
        elif profile_id == "codex":
            http_config_text = _build_streamable_http_toml(
                remote_url, client="codex", identity_env=identity_env
            )
        else:
            http_config_text = _build_streamable_http_toml(
                remote_url, client="grok_build", identity_env=identity_env
            )
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
                "migrate_http": {
                    "http": build_onboarding_prompt(
                        profile_id=profile_id,
                        profile=profile,
                        transport="http",
                        config_text="",
                        project=project,
                        mode="migrate_http",
                    )
                },
            }
        if profile_id == "workbuddy":
            profiles[profile_id]["project_memory_text"] = project_instructions_text
    return {
        "schema_version": 5,
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
