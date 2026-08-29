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


def test_web_bootstrap_and_remote_bridge_hooks_are_complete():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert "async function loadAuthenticatedApp()" in javascript
    assert 'api("/api/v1/auth/status")' in javascript
    assert 'autocomplete="username"' in markup
    assert "function renderIntegrationJoin()" in javascript
    assert '"streamable_http_config_text"' in javascript
    assert 'data-integration-transport="http"' in markup
    assert 'payload.host_key = "<stable-host-key>"' in javascript
    assert 'payload.host_name = "<computer-name>"' in javascript
    assert '"<path-to-project-on-this-computer>"' in javascript
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
    assert 'app.css?v=1.0.0-central16' in markup
    assert 'app.js?v=1.0.0-central16' in markup
    assert "function renderIntegrationTabs()" in javascript
    assert 'class="segmented-control integration-tabs" id="integration-format-tabs"' in markup
    assert "state.integration.profiles" in javascript
    assert 'id="integration-onboarding-prompt"' in markup
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
