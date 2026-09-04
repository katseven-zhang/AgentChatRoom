from __future__ import annotations

import json

from agentchatroom.config import Settings
from agentchatroom.integrations import build_mcp_integration


def test_mcp_integration_uses_explicit_runtime_configuration(tmp_path):
    config_path = tmp_path / "custom.toml"
    settings = Settings(data_dir=tmp_path / "data", config_path=config_path)

    result = build_mcp_integration(
        settings, python_executable=tmp_path / "runtime" / "python.exe"
    )

    server = result["generic_json"]["mcpServers"]["agentchatroom"]
    assert server["command"].endswith("python.exe")
    assert server["args"] == ["-m", "agentchatroom.mcp_server"]
    assert server["env"]["AGENTCHATROOM_DATA_DIR"] == str(settings.data_dir)
    assert server["env"]["AGENTCHATROOM_CONFIG"] == str(config_path)
    assert server["env"]["AGENTCHATROOM_SOFTWARE_KEY"] == "<stable-software-key>"
    assert server["env"]["AGENTCHATROOM_SOFTWARE_NAME"] == "<Software name>"
    assert server["env"]["AGENTCHATROOM_SOFTWARE_CLIENT"] == "<software-client-code>"
    assert result["runtime"]["log_path"] == str(settings.data_dir / "server.log")
    assert result["profiles"]["workbuddy"]["vendor"] == "Tencent"
    assert result["profiles"]["workbuddy"]["format"] == "json"
    assert result["profiles"]["workbuddy"]["local_config"]["candidates"] == [
        {
            "root": "home",
            "parts": [".workbuddy", "mcp.json"],
            "label": "WorkBuddy",
        }
    ]
    assert '"mcpServers"' in result["profiles"]["workbuddy"]["config_text"]
    assert "[mcp_servers.agentchatroom]" in result["profiles"]["grok_build"]["config_text"]
    assert "[mcp_servers.agentchatroom]" in result["profiles"]["codex"]["config_text"]
    assert result["profiles"]["trae"]["vendor"] == "ByteDance"
    assert result["profiles"]["trae"]["local_config"]["candidates"][0][
        "parts"
    ] == ["TRAE SOLO CN", "User", "mcp.json"]
    assert '"AGENTCHATROOM_SOFTWARE_KEY": "trae"' in result["profiles"]["trae"][
        "config_text"
    ]
    assert result["profiles"]["generic"]["format"] == "json"
    assert '"mcpServers"' in result["profiles"]["generic"]["config_text"]
    assert "OpenCode" in result["profiles"]["generic"]["config_path_hint"]
    assert '"mcpServers"' in result["profiles"]["generic"]["streamable_http_config_text"]
    assert "agentchatroom.mcp_bridge" in result["profiles"]["generic"][
        "remote_bridge_config_text"
    ]
    remote = result["remote_bridge_json"]["mcpServers"]["agentchatroom"]
    assert remote["command"] == "python"
    assert remote["args"] == ["-m", "agentchatroom.mcp_bridge"]
    assert remote["env"]["AGENTCHATROOM_SERVER_URL"] == "http://127.0.0.1:8765/mcp"
    assert remote["env"]["AGENTCHATROOM_AGENT_TOKEN"] == "<paste-issued-agent-token>"
    assert remote["env"]["AGENTCHATROOM_PRESENCE_KEEPALIVE_ENABLED"] == "true"
    assert remote["env"]["AGENTCHATROOM_PRESENCE_KEEPALIVE_INTERVAL_SECONDS"] == "15.0"
    assert result["transports"]["remote_stdio_bridge"]["owns_business_database"] is False
    assert "agentchatroom.mcp_bridge" in result["profiles"]["workbuddy"][
        "remote_bridge_config_text"
    ]


def test_streamable_http_profiles_use_center_url_without_real_tokens(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        external_base_url="https://room.example.test",
    )

    result = build_mcp_integration(settings)

    assert result["streamable_http_json"]["mcpServers"]["agentchatroom"]["url"] == (
        "https://room.example.test/mcp"
    )
    assert "<paste-issued-agent-token>" in result["streamable_http_json_text"]
    assert "bearer_token_env_var" in result["codex_streamable_http_toml"]
    assert "AGENTCHATROOM_AGENT_TOKEN" in result["grok_streamable_http_toml"]
    assert "<paste-issued-agent-token>" in result["profiles"]["workbuddy"][
        "streamable_http_config_text"
    ]
    assert "<paste-issued-agent-token>" not in result["grok_streamable_http_toml"]


def test_remote_bridge_command_is_configurable(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        mcp_bridge_command=r"C:\AgentTools\Python\python.exe",
    )

    result = build_mcp_integration(settings)

    remote = result["remote_bridge_json"]["mcpServers"]["agentchatroom"]
    local = result["generic_json"]["mcpServers"]["agentchatroom"]
    assert remote["command"] == r"C:\AgentTools\Python\python.exe"
    assert local["command"] != remote["command"]


def test_project_integration_builds_stable_workbuddy_memory_without_live_state(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    project = {
        "id": "project_runtime_only",
        "name": "AgentChatRoom",
        "project_key": "sample-project",
        "root_path": str(tmp_path),
    }

    result = build_mcp_integration(settings, project=project)
    memory = result["profiles"]["workbuddy"]["project_memory_text"]

    assert result["schema_version"] == 4
    assert result["project"] == {"name": "AgentChatRoom"}
    assert "sample-project" not in memory
    assert "`OFF`" in memory and "`OBSERVE`" in memory and "`COORDINATE`" in memory
    assert "session_heartbeat" in memory
    assert "must not supply, rename, or invent an `agent_key`" in memory
    assert "Do not begin project work while disconnected" in memory
    assert "room_bootstrap" in memory
    assert "use `unknown`" in memory
    assert ".agentchatroom/project.json" in memory
    assert "must not edit it, supply a key" in memory

    prompt = result["onboarding_prompt"]
    assert "接入名为 `agentchatroom` 的 MCP Server" in prompt
    assert "请根据当前客户端和运行环境自行完成接入" in prompt
    assert "project_key：sample-project" not in prompt
    assert "room_join" not in prompt
    assert "room_bootstrap" not in prompt
    assert "model_display_name" not in prompt
    assert "协作规则" not in prompt
    assert ".agentchatroom/project.json" not in prompt
    assert "Task" not in prompt
    assert "Lease" not in prompt
    assert "Work Report" not in prompt
    assert "AGENTCHATROOM_SOFTWARE_KEY" in prompt
    assert '"mcpServers"' in prompt
    assert "无法自动配置时" not in prompt
    assert "不要提权" not in prompt
    assert "配置文件：" not in prompt
    assert "操作要求" not in prompt
    assert "重启" not in prompt
    assert "。。" not in prompt
    assert "project_runtime_only" not in memory
    assert "Session Token" in memory

    workbuddy_prompts = result["profiles"]["workbuddy"]["onboarding_prompts"]
    assert set(workbuddy_prompts) == {"local", "http", "remote"}
    assert "请为 WorkBuddy 接入名为 `agentchatroom`" in workbuddy_prompts["local"]
    assert "~/.workbuddy/mcp.json" not in workbuddy_prompts["local"]
    assert "AGENTCHATROOM_SOFTWARE_KEY" in workbuddy_prompts["local"]
    assert '"AGENTCHATROOM_SOFTWARE_KEY": "workbuddy"' in workbuddy_prompts["local"]
    assert '"mcpServers"' in workbuddy_prompts["local"]
    assert "直接 HTTP MCP" in workbuddy_prompts["http"]
    assert "<paste-issued-agent-token>" in workbuddy_prompts["http"]
    assert "远程 Bridge" in workbuddy_prompts["remote"]
    assert "agentchatroom.mcp_bridge" in workbuddy_prompts["remote"]

    codex_prompts = result["profiles"]["codex"]["onboarding_prompts"]
    assert "请为 Codex 接入名为 `agentchatroom`" in codex_prompts["local"]
    assert "AGENTCHATROOM_SOFTWARE_KEY" in codex_prompts["local"]
    assert 'AGENTCHATROOM_SOFTWARE_KEY = "codex"' in codex_prompts["local"]
    assert (
        f"AGENTCHATROOM_PROJECT_PATH = {json.dumps(str(tmp_path))}"
        in codex_prompts["local"]
    )
    assert "room_join" not in codex_prompts["local"]
    assert "无法自动配置时" not in codex_prompts["local"]
    assert "[mcp_servers.agentchatroom]" in codex_prompts["local"]
    assert "bearer_token_env_var" in codex_prompts["http"]
