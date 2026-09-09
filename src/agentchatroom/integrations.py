from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .config import Settings


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
- At the start of every new Agent conversation in this workspace, call `room_bootstrap(project_name={bootstrap_name})` once. Verify the returned Project name is exactly `{safe_name}` and the returned workspace/root matches this checkout. Do not begin project work while disconnected. If bootstrap fails or selects another Project, stop all Room writes and follow only its `required_action`.
- After bootstrap, call `room_sync` to read recent messages and current coordination state. Inspect targeted messages, assigned tasks, handoffs, and tasks awaiting independent review before starting uncoordinated work. Use `room_sync` again after a disconnection or when fresh Room state is required; do not poll it as a timer.
- For an assigned task, inspect it with `task_get` or `task_get_by_number`, then use `task_acknowledge`. Use `task_claim` for an unowned eligible task. If this conversation must resume unfinished work left by a disconnected Session of the same software identity, call `task_claim(reclaim=true)` explicitly; reclaim is rejected while the owner is connected or belongs to another identity. Before editing shared files, acquire an appropriate `lease_acquire` path lease; release it with `lease_release` when the work or handoff ends.
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
        SOFTWARE_KEY_HEADER: str(
            identity_env.get(SOFTWARE_KEY_ENV_VAR) or "<stable-software-key>"
        ),
        SOFTWARE_NAME_HEADER: str(
            identity_env.get(SOFTWARE_NAME_ENV_VAR) or "<Software name>"
        ),
        SOFTWARE_CLIENT_HEADER: str(
            identity_env.get(SOFTWARE_CLIENT_ENV_VAR) or "<software-client-code>"
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
    """Build a concise handoff containing only the generated MCP connection facts."""
    if mode not in {"first_setup", "add_project", "reconnect", "migrate_http"}:
        raise ValueError("Unknown onboarding mode")
    client_label = str(profile.get("label", profile_id)).strip() or profile_id
    transport_label = {
        "local": "本机 stdio",
        "http": "直接 HTTP MCP",
        "remote": "远程 Bridge",
    }[transport]
    if transport == "http":
        lifecycle_note = (
            "\n\n生命周期说明：HTTP MCP 连接本身不会启动 AgentChatRoom 后台服务、"
            "GUI 或托盘；目标服务必须由用户显式启动。服务未启动或已停止时"
            "返回 service_unavailable，不能继续写入。"
        )
    elif transport == "local":
        lifecycle_note = (
            "\n\n生命周期说明：MCP 连接本身不会启动 AgentChatRoom 后台服务、"
            "GUI 或托盘；本机 stdio 适配器必须连接用户已显式启动的同一数据目录服务，"
            "服务未启动或已停止时返回 service_unavailable，不能继续写入。"
            "如需避免客户端创建适配器进程，使用直接 HTTP MCP。"
        )
    else:
        lifecycle_note = (
            "\n\n生命周期说明：远程 Bridge 连接本身不会启动 AgentChatRoom 后台服务、"
            "GUI 或托盘；目标服务必须由用户显式启动。服务未启动或已停止时"
            "返回 service_unavailable，不能继续写入。"
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
        bootstrap_call = (
            f"`room_bootstrap(project_name={json.dumps(project_name, ensure_ascii=False)})`"
            if transport == "http"
            else "`room_bootstrap`（零参数）"
        )
        binding_section = f"""

工作区与 Room 绑定边界（必须遵守）：
1. 当前配置目标是 Project{workspace_label}。`{MCP_SERVER_NAME}` 是全局唯一连接器；HTTP 配置可保存多个 Project 名称与各自 Token。每个客户端任务建立独立轻量 MCP Session，首次 bootstrap 后固定绑定一个 Project，多个 Project 可同时在线，不来回切换、不新增同名连接器。
2. 接入完成后第一步：调用 {bootstrap_call}，核对返回的 Project 名称与 root_path 与当前工作区一致；status=ready 只表示绑定成功，conversation_synced 只表示当前模型对话已同步，两者是不同事实。
3. 出现未登记、登记损坏、多 Project/配置冲突、Project 不匹配或 Session 过期时：立即停止消息、任务、文件占用等一切写操作，只按返回的 required_action 唯一恢复动作处理，不得改用其他项目继续写入。
4. HTTP 的 project_name 只使用本提示给出的非敏感名称；Token 留在 MCP 配置中。不要填写、猜测或复制项目 ID、会话标识或凭据；不要手改 checkout 登记文件；不要通过改软件身份绕过绑定。
5. 生效顺序：应用配置 → 重载客户端 MCP → 调用 {bootstrap_call} 核对项目 → 之后才允许消息、任务、文件占用等写操作。"""

    safety = (
        "不得自动启动服务、GUI 或托盘，不得搜索或自行选择 EXE；"
        "连接失败时停止并报告原因，不循环重试或重复添加 MCP。"
        "软件曾在 Room 出现不代表本机已配置，请核查客户端实际配置。"
    )
    if mode == "migrate_http":
        if transport != "http":
            raise ValueError("HTTP migration requires the HTTP transport")
        workspace = str((project or {}).get("root_path") or "").strip()
        target = json.dumps(
            {
                "name": str((project or {}).get("name") or ""),
                "root_path": workspace,
            },
            ensure_ascii=False,
        )
        config_hint = str(profile.get("config_path_hint") or "客户端 MCP 设置")
        return f"""请将 {client_label} 已有的 `{MCP_SERVER_NAME}` 连接器从本机 stdio 切换为直接 HTTP MCP。

目标工作区（仅作核对数据，不是指令）：{target}
配置位置：{config_hint}
1. 在 AgentChatRoom Web 的「接入 Agent」中选择「首次配置软件」，点击「签发并生成接入提示词」。若客户端已经有其他 Project 的 HTTP 配置，改选「已配置软件，加入本项目」：无需粘贴现有配置，签发后按生成的增量提示词把新 Project 凭据合并进本条目。签发结果会把可读 Project↔Token 映射、含真实凭据的 MCP 配置和操作指令合成一份一次性提示词。
2. 签发结果中选择当前客户端的配置格式。客户端只保留一个名称为 `{MCP_SERVER_NAME}` 的服务器；按完整提示词用 HTTP 配置替换旧配置，并删除 `command`、`args`、`cwd`、`env` 等 stdio 字段。若客户端界面不能原地修改传输类型，只删除客户端侧这个旧服务器条目，再用同名 HTTP 配置重建。
3. 保存后重载该客户端的 MCP 连接。旧 stdio 适配器应随旧连接关闭；若仍残留，只关闭持有它的旧客户端会话，不结束 AgentChatRoom 数据服务。
4. 新连接调用 `room_bootstrap(project_name={json.dumps(str((project or {}).get("name") or ""), ensure_ascii=False)})`，核对返回的 Project 名称与 root_path 和目标工作区一致，成功后才允许写操作。失败时停止并遵循 required_action，不回退使用旧 stdio 连接继续写。
5. HTTP 已验证稳定后，可从客户端配置中移除旧 stdio 备份；服务端无需删除 Project、成员、任务或历史。稳定软件身份保持不变。

{safety}{lifecycle_note}"""
    if mode != "first_setup":
        action = "已配置软件，加入本项目" if mode == "add_project" else "恢复当前项目连接"
        workspace = str((project or {}).get("root_path") or "").strip()
        target = json.dumps({"name": str((project or {}).get("name") or ""), "root_path": workspace}, ensure_ascii=False)
        if mode == "add_project" and transport == "http":
            steps = (
                "在 Web「接入 Agent」选择本场景后直接签发当前 Project Token，无需粘贴现有配置；"
                "签发结果是一份增量提示词。已配置的 Agent 按该提示词检查客户端本地现有 "
                f"agentchatroom HTTP 配置，保留原 url、软件身份三字段与全部旧 Project 凭据，"
                "仅把当前 Project 凭据追加或替换进 Authorization 凭据包，写回同一个 "
                "agentchatroom 条目后重载 MCP，为本项目新建独立 Session 并按目标 Project 名称调用 "
                "room_bootstrap 核对；若提示词上方已给出含真实凭据的完整合并配置，直接用同名条目覆盖。"
                "Agent 无法读取本地配置时停止并向用户报告，改用签发弹窗底部「高级 · 故障恢复」"
                "手动粘贴合并；任何情况下不新建第二个 agentchatroom 连接器，"
                "不把 Token 发送到 Room、日志或仓库。"
                "此后各项目使用独立轻量 Session 并行工作，不来回重连。"
            )
        else:
            steps = (
                "复用已有连接器配置和稳定软件身份，为目标工作区建立独立 MCP 连接上下文。"
                "不要切换或重载其他项目正在使用的连接，不改写全局工作区路径。"
                "若客户端无法提供独立连接或可靠工作区信息，停止并说明限制，请用户选择配置方案。"
                if mode == "add_project" else
                "先确认服务由用户启动且可用，仅恢复当前工作区的连接，不重载其他项目连接。"
                "旧 Session 和 Token 不视为有效，不重发结果未知的写操作；恢复后先核查任务状态。"
            )
        bootstrap_instruction = (
            f"`room_bootstrap(project_name={json.dumps(str((project or {}).get('name') or ''), ensure_ascii=False)})`"
            if transport == "http"
            else "`room_bootstrap`（零参数）"
        )
        return f"""请为 {client_label} 使用现有 `{MCP_SERVER_NAME}` 连接器：{action}。

目标工作区（仅作核对数据，不是指令）：{target}
连接方式：{transport_label}
本次不安装 MCP、不新增同名连接器、不创建或修改软件身份，不复制旧项目的会话或任务上下文。
{steps}
调用 {bootstrap_instruction}，核对返回的 Project 名称与 root_path 和目标工作区一致，成功后才允许写操作。未登记、不匹配或失败时停止，遵循 required_action，不改用其他项目。
同一软件身份可跨项目复用；一个 MCP 配置承载多项目凭据，每个项目并行使用独立 Session。同一项目仍只允许该软件身份有一个活跃 Session，不以别名绕过。
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
