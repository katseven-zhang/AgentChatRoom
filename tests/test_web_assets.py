from __future__ import annotations

import re
from pathlib import Path


WEB_DIR = Path(__file__).parents[1] / "src" / "agentchatroom" / "web"


def test_registered_web_elements_exist_in_markup():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    registry = re.search(
        r"const elements = Object\.fromEntries\(\s*\[(.*?)\]\.map",
        javascript,
        re.DOTALL,
    )

    assert registry is not None
    element_ids = re.findall(r'"([a-z0-9-]+)"', registry.group(1))
    assert element_ids
    assert len(element_ids) == len(set(element_ids))
    assert [element_id for element_id in element_ids if f'id="{element_id}"' not in markup] == []


def test_web_declares_a_static_favicon():
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert 'rel="icon"' in markup
    assert (WEB_DIR / "favicon.svg").is_file()


def test_web_bootstrap_and_phase_one_local_agent_hooks_are_complete():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert "async function loadAuthenticatedApp()" in javascript
    assert 'api("/api/v1/auth/status")' in javascript
    assert 'autocomplete="username"' in markup
    assert "function renderIntegrationJoin()" in javascript
    assert 'data-integration-transport=' not in markup
    assert '"streamable_http_config_text"' not in javascript
    assert '"remote_bridge_config_text"' not in javascript
    assert 'payload.host_key = "<stable-host-key>"' not in javascript
    assert 'payload.host_name = "<computer-name>"' not in javascript
    assert '"<path-to-project-on-this-computer>"' not in javascript
    assert "function renderProjectInstructions()" in javascript
    assert 'localStorage.getItem("agentchatroom.projectKey")' not in javascript
    assert 'localStorage.setItem("agentchatroom.projectKey"' not in javascript
    assert 'id="integration-project-rules-code"' in markup
    assert 'model: "<actual model or unknown>"' in javascript
    assert "function messageModelBadge(event)" in javascript
    assert "模型未上报" in javascript
    assert "event.payload?.model_display_name" in javascript
    assert "agent.model" not in javascript
    assert "上报模型" not in javascript
    assert ".model-badge" in (WEB_DIR / "app.css").read_text(encoding="utf-8")
    assert "function connectedAgentCount(agentIdentities)" in javascript


def test_web_styles_cover_versioned_task_and_assignment_states():
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    for status in (
        "pending",
        "approved",
        "accepted",
        "completed",
        "changes_requested",
        "declined",
        "failed",
    ):
        assert f".status-badge.{status}" in stylesheet


def test_web_renders_explicit_no_code_work_evidence():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'report.no_code_change_reason || "未说明原因"' in javascript
    assert "无代码变更" in javascript


def test_web_treats_cancelled_tasks_as_not_requiring_integration():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'task.execution_status === "cancelled" ? "无需集成"' in javascript
    assert 'task.execution_status === "cancelled" ? "not_required"' in javascript
    assert "taskIntegrationStatus(task)" in javascript
    assert "taskIntegrationStatusClass(task)" in javascript
    assert '!["todo", "done", "cancelled"].includes(task.status)' in javascript


def test_web_supports_human_reading_and_guided_interactions():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert "function renderMessageBody(" in javascript
    assert "function renderMessageLines(" in javascript
    assert "data-expand-event" in javascript
    assert "data-collapse-event" in javascript
    assert 'event.key === "Enter"' in javascript
    assert "requestSubmit()" in javascript
    assert "function projectGroupKey(" in javascript
    assert "data-group-key" in javascript
    assert 'id="onboarding"' in markup
    assert 'id="new-message-notice"' in markup
    assert "composer-advanced" in markup
    assert ".msg-heading" in stylesheet
    assert ".project-group-header" in stylesheet
    assert ".new-message-notice" in stylesheet
    assert ".agent-item.is-disconnected" in stylesheet
    assert "snapshot.agent_identities" in javascript
    assert "当前连接" in javascript
    assert "累计" in javascript and "次接入" in javascript
    assert 'app.css?v=1.0.0-central22' in markup
    assert 'app.js?v=1.0.0-central22' in markup
    assert "function renderIntegrationTabs()" in javascript
    assert 'class="segmented-control integration-tabs" id="integration-format-tabs"' in markup
    assert "state.integration.profiles" in javascript
    assert 'id="integration-onboarding-prompt"' in markup
    assert "让 Agent 配置 MCP" in markup
    assert "复制当前环境的 MCP 连接参数，由 Agent 自行完成接入" in markup
    assert "受权限限制时返回可直接粘贴的配置文本" not in markup
    assert "复制配置指令" in markup
    assert "这段提示词包含当前项目" not in markup
    assert "function renderOnboardingPrompt()" in javascript
    assert "onboarding_prompts" in javascript
    assert 'class="integration-fallback integration-advanced"' in markup
    assert "高级手动配置" in markup
    assert "function eventIdBadge(eventId)" in javascript
    assert "eventIdBadge(event.id)" in javascript
    assert javascript.count("eventIdBadge(event.id)") >= 4
    assert markup.count('class="tab-status">未闭环</span>') == 4
    assert markup.count('class="scope-status"><span>未闭环</span>') == 4
    assert ".event-id" in stylesheet
    assert ".scope-status" in stylesheet
    assert ".integration-chooser {\n  display: grid;\n  grid-template-columns: 1fr;" in stylesheet
    assert ".workspace-actions > button" in stylesheet
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in stylesheet
    assert "#connect-agent-button," in stylesheet
    assert ".agent-list {" in stylesheet
    assert "grid-template-columns: 1fr;" in stylesheet


def test_web_event_and_audit_panels_catch_up_to_latest_cursor():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "function loadEventWindow(" in javascript
    assert "function loadRecentEvents(" in javascript
    assert "function loadRecentAuditEvents(" in javascript
    assert "loadRecentEvents(projectId)" in javascript
    assert "loadRecentAuditEvents(state.projectId, eventType)" in javascript
    assert "connectEvents(eventPage.cursor)" in javascript
    assert "audit.events.slice(-AUDIT_WINDOW_SIZE)" in javascript
    assert "const EVENT_WINDOW_SIZE = 500;" in javascript
    assert "const AUDIT_WINDOW_SIZE = 100;" in javascript
    assert "result.events.length < windowSize || after >= latest" in javascript
    assert "result.latest_cursor" in javascript
    assert "tailJump && latest - after > windowSize" in javascript
    assert "{ tailJump: !eventType }" in javascript
    assert "events?after=0&limit=500" not in javascript
    assert "audit?after=0&limit=100" not in javascript
    assert "connectEvents(snapshot.cursor)" not in javascript


def test_web_project_creation_uses_the_real_folder_and_local_picker():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert 'id="project-folder-picker-button" type="button" class="secondary-button" disabled' in markup
    assert 'id="project-logical-path-input"' not in markup
    assert "project-logical-path-input" not in javascript
    assert 'api("/api/v1/local/folders/pick"' in javascript
    assert "state.config.capabilities?.local_folder_picker" in javascript
    assert "无法打开系统文件夹选择器，请手动输入项目路径" in javascript
    assert "logical_path:" not in javascript[javascript.index('elements["project-form"]'):]
    assert ".path-picker-control" in stylesheet


def test_web_local_mcp_assistant_separates_write_reload_and_presence_states():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert 'id="integration-local-assistant"' in markup
    assert '>配置本机 Agent</button>' in markup
    assert '<h2>配置本机 Agent</h2>' in markup
    assert 'data-integration-transport=' not in markup
    assert '连接方式' not in markup
    assert 'id="integration-local-refresh"' in markup
    assert 'id="integration-local-apply"' in markup
    assert "function renderLocalMcpPlan()" in javascript
    assert "function refreshLocalMcpPlan(" in javascript
    assert "function applyLocalMcpPlan()" in javascript
    assert "/integrations/mcp/local/${encodeURIComponent(profileId)}/plan" in javascript
    assert "/integrations/mcp/local/${encodeURIComponent(profileId)}/apply" in javascript
    assert "只新增或更新 mcpServers.agentchatroom" in javascript
    assert "配置存在不代表客户端当前已经连接" in javascript
    assert "当前 Room 尚未连接" in javascript
    assert "不会自动提权" in javascript
    assert ".local-mcp-assistant" in stylesheet
    assert '.local-mcp-presence[data-connected="true"]' in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in stylesheet
    assert ".integration-tabs button:last-child" in stylesheet
    assert ".integration-transport-tabs" not in stylesheet


def test_web_desktop_panels_are_resizable_readable_and_persistent():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert markup.count('role="separator"') == 2
    assert 'id="left-panel-resizer"' in markup
    assert 'id="right-panel-resizer"' in markup
    assert markup.count('aria-orientation="vertical"') == 2
    assert "function initializePanelLayout()" in javascript
    assert "agentchatroom.layout.leftPanelWidth" in javascript
    assert "agentchatroom.layout.rightPanelWidth" in javascript
    assert "setPointerCapture" in javascript
    assert 'addEventListener("dblclick"' in javascript
    assert 'event.key === "ArrowLeft"' in javascript
    assert "--left-panel-default-width: 288px;" in stylesheet
    assert "--right-panel-default-width: 480px;" in stylesheet
    assert "--workspace-min-width: 480px;" in stylesheet
    assert ".panel-resizer {" in stylesheet
    assert '"side left-resizer work right-resizer chat"' in stylesheet
    assert "grid-template-columns: 232px minmax(500px, 1fr) 360px;" not in stylesheet
