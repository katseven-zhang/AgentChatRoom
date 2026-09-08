from __future__ import annotations

import json
import pytest

from agentchatroom.config import Settings
from agentchatroom.integrations import build_mcp_integration


def test_onboarding_modes_separate_configuration_from_existing_connections(tmp_path):
    project = {'name': 'Room B', 'root_path': str(tmp_path), 'id': 'do-not-copy', 'project_key': 'private-key'}
    result = build_mcp_integration(Settings(data_dir=tmp_path / 'data'), project=project)
    for profile in result['profiles'].values():
        modes = profile['onboarding_modes']
        assert set(modes) == {'first_setup', 'add_project', 'reconnect'}
        assert modes['first_setup'] == profile['onboarding_prompts']
        for mode in ('add_project', 'reconnect'):
            assert set(modes[mode]) == {'local', 'http', 'remote'}
            for prompt in modes[mode].values():
                assert 'Room B' in prompt
                assert json.dumps(str(tmp_path), ensure_ascii=False) in prompt
                assert '调用零参数 `room_bootstrap`' in prompt
                assert '不创建或修改软件身份' in prompt
                assert '不新增同名连接器' in prompt
                assert 'do-not-copy' not in prompt and 'private-key' not in prompt
                assert 'mcpServers' not in prompt
                assert 'AGENTCHATROOM_SOFTWARE_KEY' not in prompt
                assert 'paste-issued-agent-token' not in prompt
        assert '独立 MCP 连接上下文' in modes['add_project']['local']
        assert '不重发结果未知的写操作' in modes['reconnect']['local']


def test_invalid_onboarding_mode_is_rejected():
    from agentchatroom.integrations import build_onboarding_prompt
    with pytest.raises(ValueError, match='onboarding mode'):
        build_onboarding_prompt(profile_id='generic', profile={}, transport='local', config_text='', mode='typo')


def test_frontend_onboarding_mode_selection():
    import subprocess
    from pathlib import Path
    result = subprocess.run(
        ['node', str(Path(__file__).with_name('onboarding_modes.cjs'))],
        capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    assert result.returncode == 0, result.stderr


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
    assert "X-AgentChatRoom-Software-Key" in result["streamable_http_json_text"]
    assert "acr." not in result["streamable_http_json_text"]
    assert "bearer_token_env_var" in result["codex_streamable_http_toml"]
    assert "AGENTCHATROOM_AGENT_TOKEN" in result["grok_streamable_http_toml"]
    assert "X-AgentChatRoom-Software-Key" in result["grok_streamable_http_toml"]
    assert "<paste-issued-agent-token>" in result["profiles"]["workbuddy"][
        "streamable_http_config_text"
    ]
    assert "X-AgentChatRoom-Software-Key" in result["profiles"]["workbuddy"][
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
    # #98: the prompt must now REQUIRE a first zero-arg room_bootstrap and a
    # project check instead of staying silent about the binding step.
    assert "工作区与 Room 绑定边界" in prompt
    assert "调用零参数 `room_bootstrap`" in prompt
    assert "root_path" in prompt
    assert "不能被上一个项目静默带入另一个工作区" in prompt
    assert "针对当前显示的工作区/Project「AgentChatRoom」" in prompt
    assert "立即停止消息、任务、文件占用等一切写操作" in prompt
    assert "不要填写、猜测或复制任何项目/会话标识或凭据" in prompt
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


def test_frozen_build_uses_exe_mcp_dispatch_everywhere(tmp_path, monkeypatch):
    """Task #85/#88 follow-up: in a packaged (frozen) run, every generated MCP
    snippet — including per-profile onboarding prompts — must dispatch through
    `agentchatroom.exe mcp`, never `python -m agentchatroom.mcp_server`."""
    import sys

    from agentchatroom.config import Settings
    from agentchatroom.integrations import build_mcp_integration

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    integration = build_mcp_integration(
        Settings(data_dir=tmp_path),
        project={"root_path": str(tmp_path)},
    )

    top_args = integration["args"]
    assert top_args == ["mcp"]

    for profile_id, profile in integration["profiles"].items():
        config_text = profile["local_config_text"]
        # In this non-frozen test process sys.executable is pytest's python,
        # so only the dispatch args are the frozen-regression surface here.
        assert '"-m"' not in config_text, profile_id
        assert '"mcp"' in config_text, profile_id
        prompt = profile["onboarding_prompts"]["local"]
        assert " -m " not in prompt, profile_id
        assert '"mcp"' in prompt, profile_id


def test_generated_mcp_configs_contain_no_terminal_launch_chain(tmp_path):
    """#101: generated configs must never imply a cmd/terminal launch chain."""
    forbidden = [
        ".cmd",
        ".bat",
        "cmd /c",
        "cmd.exe",
        "powershell",
        "pwsh",
        "windows terminal",
        "bash -c",
        "start ",
    ]
    settings = Settings(data_dir=tmp_path / "data")
    result = build_mcp_integration(
        settings, python_executable=tmp_path / "runtime" / "python.exe"
    )
    payloads = [
        json.dumps(result["generic_json"], ensure_ascii=False),
        json.dumps(result["remote_bridge_json"], ensure_ascii=False),
        json.dumps(result["streamable_http_json"], ensure_ascii=False),
    ]
    for profile in result["profiles"].values():
        for key in ("config_text", "remote_bridge_config_text"):
            value = profile.get(key)
            if value:
                payloads.append(value)
    for payload in payloads:
        lowered = payload.lower()
        for marker in forbidden:
            assert marker not in lowered, f"found {marker!r} in generated config"


def test_onboarding_prompt_states_lifecycle_and_pin_semantics(tmp_path):
    """#101/#97: the handoff prompt states no auto-start and the pin caveat."""
    settings = Settings(data_dir=tmp_path / "data")
    project = {
        "id": "project_prompt",
        "name": "AgentChatRoom",
        "project_key": "sample-project",
        "root_path": str(tmp_path),
    }
    result = build_mcp_integration(settings, project=project)
    prompt = result["onboarding_prompt"]
    assert "不会启动 AgentChatRoom 后台服务" in prompt
    assert "service_unavailable" in prompt
    assert "用户已显式启动" in prompt
    assert "直接 HTTP MCP" in prompt
    assert "AGENTCHATROOM_PROJECT_PATH" in prompt
    assert "兜底" in prompt
    # #98: every transport's prompt carries the same binding boundary.
    for transport, text in result["profiles"]["generic"]["onboarding_prompts"].items():
        assert "工作区与 Room 绑定边界" in text, transport
        assert "调用零参数 `room_bootstrap`" in text, transport
        assert "root_path" in text, transport
        assert "立即停止消息、任务、文件占用等一切写操作" in text, transport
        assert "「AgentChatRoom」" in text, transport
        assert "生效顺序" in text, transport
