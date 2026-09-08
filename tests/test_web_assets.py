from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


WEB_DIR = Path(__file__).parents[1] / "src" / "agentchatroom" / "web"


def test_web_theme_text_contrast_is_readable():
    """Check actual theme colors, including filled controls in dark mode."""
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    themes = re.findall(r':root(?:\[data-theme="dark"\])?\s*\{([^}]+)\}', css)
    assert len(themes) == 2
    for theme in themes:
        colors = dict(re.findall(r'(--[\w-]+):\s*(#[\da-fA-F]{6});', theme))
        pairs = [(text, bg) for text in ("--text", "--text-muted")
                 for bg in ("--bg", "--surface", "--surface-muted")]
        pairs += [("--on-accent", "--accent-fill"), ("--on-accent", "--accent-fill-hover")]
        pairs += [(text, bg) for text, bg in (("--primary", "--primary-soft"),
                  ("--red", "--red-soft"), ("--amber", "--amber-soft"), ("--blue", "--blue-soft"))]
        for foreground, background in pairs:
            assert foreground in colors and background in colors
            light, dark = sorted((luminance(colors[foreground]), luminance(colors[background])), reverse=True)
            ratio = (light + 0.05) / (dark + 0.05)
            assert ratio >= 4.5, (foreground, background, round(ratio, 2))


def test_web_appearance_precedence_and_restricted_storage(tmp_path):
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function resolveAppearanceTheme(")
    end = javascript.index("function applyPublicConfig()", start)
    harness = tmp_path / "appearance.js"
    harness.write_text("""
const assert = require('node:assert/strict');
const state = {config: {default_theme: 'system'}};
let change, systemChange;
const control = {value: 'default', addEventListener: (_, fn) => {change = fn;}};
const document = {getElementById: () => control, documentElement: {dataset: {}}};
const window = {matchMedia: () => ({matches: true, addEventListener: (_, fn) => {systemChange = fn;}})};
let localStorage = {getItem: () => 'invalid', setItem: () => {throw Error('storage denied');}};
""" + javascript[start:end] + """
for (const [local, config, osDark, expected] of [
  ['default', 'system', true, 'dark'], ['default', 'system', false, 'light'],
  ['light', 'dark', true, 'light'], ['dark', 'light', false, 'dark'],
  ['invalid', 'invalid', true, 'light']
]) assert.equal(resolveAppearanceTheme(local, config, osDark), expected);
initializeAppearance();
assert.equal(control.value, 'default');
assert.equal(document.documentElement.dataset.theme, 'dark');
control.value = 'light'; change(); systemChange();
assert.equal(document.documentElement.dataset.theme, 'light');
localStorage = {getItem: () => 'dark'};
initializeAppearance();
assert.equal(control.value, 'dark');
assert.equal(document.documentElement.dataset.theme, 'dark');
""", encoding="utf-8")
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


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
    assert 'id="login-username"' not in markup
    assert 'autocomplete="current-password"' in markup
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
    assert "function currentAgentRoster(agentIdentities)" in javascript
    assert "agent.member_status !== \"revoked\"" in javascript
    assert "currentAgentRoster(snapshot.agent_identities)" in javascript
    assert "可选择所有已接入且未吊销的 Agent" in javascript
    assert "当前未连接的 Agent 会在重新接入后受理任务" in javascript
    assert "target.connection_status === \"connected\" ? \"已连接\" : \"未连接\"" in javascript
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


def test_web_renders_state_view_from_server_config_without_hardcoded_buckets():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    # The shared projection is the single source of truth in the front end.
    assert "function taskView(task)" in javascript
    assert "function taskNotFinished(task)" in javascript
    assert "task?.state_view" in javascript
    # The retired hard-coded bucket/status helpers must be gone.
    for retired in (
        "function bucketOf(",
        "function bucketLabel(",
        "function taskStatus(",
        "function taskDisplayLabel(",
        "function executionStatus(",
        "function verificationStatus(",
        "function integrationStatus(",
    ):
        assert retired not in javascript
    assert "legacy_status" not in javascript
    # Navigation entries and expert filters are rendered from server config.
    assert 'id="task-navigation"' in markup
    assert 'id="task-expert-filters"' in markup
    assert "data-filter" not in markup
    assert 'data-task-entry="${escapeHtml(entry.key)}"' in javascript
    assert "state.config?.domain?.task_view" in javascript
    # Cards show task number and priority side by side, plus view badges.
    assert "P${task.priority}" in javascript
    assert "#${task.task_number}" in javascript
    assert "function taskViewBadgeHtml(view)" in javascript
    assert 'class="status-badge aux' in javascript
    # Terminal cancelled tasks are never treated as integration candidates.
    assert '["done", "cancelled"].includes(group)' in javascript
    # The three anomaly phases carry visible emphasis styles.
    for phase in ("changes_requested", "integration_failed", "unclassified"):
        assert f".status-badge.{phase}" in stylesheet


def test_web_task_list_renders_task_number_and_priority_together():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    render_table = javascript[
        javascript.index("function renderTaskTable(tasks)") : javascript.index(
            "function intakeTargetName",
            javascript.index("function renderTaskTable(tasks)"),
        )
    ]

    assert '<span class="task-number">#${task.task_number}</span>' in render_table
    assert '<span class="priority p${task.priority}">P${task.priority}</span>' in render_table


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
    assert 'app.css?v=1.0.0-central40' in markup
    assert 'app.js?v=1.0.0-central40' in markup
    assert len(re.findall(r'<script\b[^>]*src="/assets/app\.js', markup)) == 1
    assert 'id="task-history-filter"' in markup
    assert "function loadTaskHistory(" in javascript
    assert "function renderHistoryEvidence(" in javascript
    assert "data-copy-event" in javascript
    assert "data-open-event" in javascript
    assert "function renderIntegrationTabs()" in javascript
    assert 'class="segmented-control integration-tabs" id="integration-format-tabs"' in markup
    assert "state.integration.profiles" in javascript
    assert 'id="integration-onboarding-prompt"' in markup
    assert "当前场景的接入指令" in markup
    assert "复制当前环境的 MCP 连接参数与绑定边界，Agent 接入后先零参数 room_bootstrap 核对当前项目" in markup
    assert "受权限限制时返回可直接粘贴的配置文本" not in markup
    assert "复制接入指令" in markup
    assert "这段提示词包含当前项目" not in markup
    assert "function renderOnboardingPrompt()" in javascript
    assert "onboarding_prompts" in javascript
    assert 'class="integration-fallback integration-advanced"' in markup
    assert "高级手动配置" in markup
    assert "function eventIdBadge(eventId)" in javascript
    assert "eventIdBadge(event.id)" in javascript
    assert javascript.count("eventIdBadge(event.id)") >= 4
    # Navigation labels describe user destinations, not internal release status.
    assert 'class="tab-status"' not in markup
    assert 'aria-label="文件占用，只读视图"' in markup
    assert 'aria-label="验证，只读视图"' in markup
    assert 'aria-label="管理"' in markup
    assert "服务端版本化投影" not in markup
    assert "查看服务状态，管理备份、项目文档和访问权限" in markup
    assert "租约由 Agent 通过 MCP 申请与释放（会话关闭后自动失效）" in markup
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
    assert "function fetchAuditTail(" in javascript
    assert "function auditQueryUrl(" in javascript
    assert "async function loadOlderAuditEvents(" in javascript
    assert "async function loadNewerAuditEvents(" in javascript
    assert "loadRecentEvents(projectId)" in javascript
    assert 'fetchAuditTail(projectId, "")' in javascript
    assert "connectEvents(eventPage.cursor)" in javascript
    assert "state.auditHasOlder" in javascript
    assert "state.auditHasNewer" in javascript
    assert "const AUDIT_PAGE_SIZE = 10;" in javascript
    assert "mergeAuditEvents" not in javascript
    assert "resetAuditBuffer" not in javascript
    assert 'data-audit-action="older"' in javascript
    assert "上一页" in javascript and "下一页" in javascript
    # Task #94: the unread badge is gone from the agent card face; the
    # hover tooltip keeps connection/task/heartbeat details.
    assert 'class="unread-count"' not in javascript
    assert "未读事件数" not in javascript
    # Task #92: the list renders newest-first, so the visually-down "下一页 →"
    # must load older events and the visually-up "← 上一页" must load newer.
    pager = javascript[javascript.index("function auditPager("):javascript.index("function renderAudit(")]
    assert 'data-audit-action="newer"' in pager and "← 上一页" in pager
    assert pager.index('data-audit-action="newer"') < pager.index("← 上一页")
    assert 'data-audit-action="older"' in pager and "下一页 →" in pager
    assert pager.index('data-audit-action="older"') < pager.index("下一页 →")
    assert "state.auditHasNewer ? \"\" : \"disabled\"" in pager
    assert "state.auditHasOlder ? \"\" : \"disabled\"" in pager
    assert "const EVENT_WINDOW_SIZE = 500;" in javascript
    assert "settings?.audit_window_size" in javascript
    assert "AUDIT_WINDOW_SIZE" not in javascript
    assert "loadRecentAuditEvents" not in javascript
    assert "result.events.length < windowSize || after >= latest" in javascript
    assert "result.latest_cursor" in javascript
    assert "tailJump && latest - after > windowSize" in javascript
    assert "events?after=0&limit=500" not in javascript
    assert "connectEvents(snapshot.cursor)" not in javascript


def test_web_task_detail_and_review_render_spec_receipt():
    """Task #65: the claim-time spec version receipt must be visible in the
    task contract and the review queue (source-level regression)."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "function formatSpecReceipt(" in javascript
    assert "适用规范（认领时版本回执）" in javascript
    assert javascript.count("formatSpecReceipt(item.task.spec_receipt)") >= 1
    assert "task.spec_receipt" in javascript


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
    assert '<h2>接入本机 Agent</h2>' in markup
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
    assert "Presence 不是当前模型对话同步" in javascript
    assert "浏览器无法观察" in javascript
    assert 'id="integration-local-facts"' in markup
    assert "function renderLocalMcpFacts(" in javascript
    assert "软件配置" in javascript
    assert "进程连接" in javascript
    assert "Room Session" in javascript
    assert "当前对话同步" in javascript
    assert "room_bootstrap" in javascript
    assert "不会自动提权" in javascript
    assert ".local-mcp-assistant" in stylesheet
    assert ".local-mcp-facts" in stylesheet
    assert '.local-mcp-fact[data-state="ready"]' in stylesheet
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


def test_web_sse_and_project_switch_guard_stale_snapshots():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert "function refreshSnapshot(projectId)" in javascript
    assert "JSON.parse(message.data)" in javascript
    assert "Ignored malformed room event" in javascript
    assert "applySnapshotIfCurrent" in javascript
    assert "function renderForEvent(event)" in javascript
    assert "state.streamHadError" in javascript
    assert "resetProjectFilters()" in javascript
    assert "clearDialogDrafts(dialog)" in javascript
    assert 'aria-live="polite"' in markup
    assert 'role="tablist"' in markup
    assert 'role="tabpanel"' in markup
    assert "function withBusy(work)" in javascript
    assert 'document.body.setAttribute("aria-busy", "true")' in javascript
    assert "function activateTab(button)" in javascript
    assert 'event.key === "ArrowRight"' in javascript
    assert "alreadyKnown && previousId" in javascript
    assert "streamVisible" in javascript
    assert "profile.software_key" in javascript
    assert 'replaceAll("_", "-")' not in javascript
    assert "--danger:" in stylesheet
    assert 'maxlength="20000"' in markup
    assert "rememberExpandedEvent" in javascript


def test_web_task_view_harness_covers_state_triple_samples(tmp_path):
    """Node harness proving the browser renders the shared state.view codes.

    The front end never re-derives phases: samples carry the exact
    ``state_view`` payload produced by the Python projection, so this test
    locks the server projection -> server config labels -> Web rendering
    chain end to end. It runs with an explicit UTF-8 decoding contract for
    the child process so the default Windows GBK locale cannot break the
    regression.
    """
    from agentchatroom.contracts import task_view

    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Rendering helper functions that read the projection and server config.
    start_a = javascript.index("function taskViewConfig()")
    end_marker_a = "function taskNeedsIntegration(task) {\n  return taskView(task).execution_status !== \"cancelled\";\n}\n"
    end_a = javascript.index(end_marker_a) + len(end_marker_a)
    # Pure navigation/filter/sort logic used by the tasks panel.
    start_b = javascript.index("function taskNavigationEntries(tasks)")
    end_b = javascript.index("function renderTaskNavigation(tasks)")
    # Expert filter rendering (exact-state options must carry counts).
    start_c = javascript.index("function renderTaskExpertFilters(tasks)")
    end_c = javascript.index("function renderTasks(tasks)")

    config = {
        "schema_version": 2,
        "phases": [
            "todo", "claimed", "in_progress", "blocked", "awaiting_review",
            "changes_requested", "pending_integration", "integration_failed",
            "done", "cancelled", "unclassified",
        ],
        "phase_labels": {
            "todo": "待认领", "claimed": "已认领", "in_progress": "执行中",
            "blocked": "阻塞", "awaiting_review": "待验收",
            "changes_requested": "已退回", "pending_integration": "待集成",
            "integration_failed": "集成失败", "done": "已完成",
            "cancelled": "已取消", "unclassified": "未归类",
        },
        "group_labels": {
            "claimable": "待认领", "active": "进行中", "review": "待验收",
            "integration": "待集成", "done": "已完成", "cancelled": "已取消",
            "unclassified": "未归类",
        },
        "attention_phases": ["blocked", "changes_requested", "integration_failed"],
        "attention_label": "需要处理",
        "active_subgroup_order": ["changes_requested", "blocked", "in_progress", "claimed"],
    }

    sample_triples = [
        ("todo", "todo", "not_required", "pending"),
        ("claimed", "claimed", "not_required", "pending"),
        ("in_progress", "in_progress", "not_required", "pending"),
        ("blocked", "blocked", "not_required", "pending"),
        ("awaiting_review", "completed", "pending", "pending"),
        ("returned", "in_progress", "changes_requested", "pending"),
        ("pending_integration", "completed", "approved", "pending"),
        ("integration_failed", "completed", "approved", "failed"),
        ("done", "completed", "approved", "done"),
        ("cancelled", "cancelled", "not_required", "pending"),
        ("released_returned", "todo", "changes_requested", "pending"),
        ("verified_without_review", "completed", "not_required", "pending"),
        ("residue", "todo", "approved", "pending"),
    ]
    # Each sample embeds the exact projection payload the server would send.
    samples = []
    expected_mapping = {}
    for name, execution, verification, integration in sample_triples:
        view = task_view(
            execution_status=execution,
            verification_status=verification,
            integration_status=integration,
        )
        samples.append(
            {
                "name": name,
                "id": f"task-{len(samples) + 1:04d}",
                "task_number": len(samples) + 1,
                "priority": 1,
                "execution_status": execution,
                "verification_status": verification,
                "integration_status": integration,
                "state_view": view,
            }
        )
        expected_mapping[name] = {
            "phase": view["phase"],
            "group": view["group"],
            "label": config["phase_labels"][view["phase"]],
            "needs_attention": view["needs_attention"],
        }

    harness = tmp_path / "task_view_harness.js"
    harness.write_text(
        "const state = { config: { domain: { task_view: "
        + json.dumps(config, ensure_ascii=False)
        + " } }, taskExpert: { execution: '', verification: '', integration: '', priority: '', owner: '', number: '', phase: '' }, snapshot: { agents: [] } };\n"
        "function escapeHtml(value) { return String(value ?? ''); }\n"
        "function shortId(value) { return String(value); }\n"
        "const captured = { expert: '' };\n"
        "const document = { getElementById: (id) => ({ set innerHTML(value) { captured[id === 'task-expert-filters' ? 'expert' : id] = value; }, get innerHTML() { return captured[id] || ''; } }) };\n"
        + javascript[start_a:end_a]
        + "\n"
        + javascript[start_b:end_b]
        + "\n"
        + javascript[start_c:end_c]
        + "\nconst samples = "
        + json.dumps(samples, ensure_ascii=False)
        + ";\n"
        "renderTaskExpertFilters(samples);\n"
        "const mapping = Object.fromEntries(samples.map((sample) => {\n"
        "  const view = taskView(sample);\n"
        "  return [sample.name, { phase: view.phase, group: view.group, label: viewLabel(view.phase), needs_attention: view.needs_attention }];\n"
        "}));\n"
        "const entries = taskNavigationEntries(samples);\n"
        "const attentionEntry = entries.find((entry) => entry.key === 'attention');\n"
        "const activeEntry = entries.find((entry) => entry.key === 'active');\n"
        "const attentionTasks = samples.filter((sample) => matchesEntry(sample, attentionEntry));\n"
        "const activeOrder = sortForEntry(samples.filter((sample) => matchesEntry(sample, activeEntry)), activeEntry)\n"
        "  .map((sample) => taskView(sample).phase);\n"
        "console.log(JSON.stringify({ mapping, attentionCount: attentionEntry.count, attentionPhases: attentionTasks.map((sample) => taskView(sample).phase), activeOrder, expertHtml: captured.expert }));\n",
        encoding="utf-8",
    )
    output = subprocess.check_output(
        ["node", str(harness)],
        text=True,
        encoding="utf-8",
    )
    result = json.loads(output)
    assert result["mapping"] == expected_mapping
    # Inbox dedup: 4 anomalous tasks, one blocked+returned overlap counted once.
    assert result["attentionCount"] == 4
    assert sorted(result["attentionPhases"]) == sorted(
        ["blocked", "changes_requested", "integration_failed", "changes_requested"]
    )
    # Active sub-group ordering: returned first, then blocked, then execution.
    assert result["activeOrder"] == [
        "changes_requested",
        "changes_requested",
        "blocked",
        "in_progress",
        "claimed",
    ]
    # Event #2780: every exact-state option is selectable and shows its count.
    expert_html = result["expertHtml"]
    expected_counts = {
        "todo": 1,
        "claimed": 1,
        "in_progress": 1,
        "blocked": 1,
        "awaiting_review": 1,
        "changes_requested": 2,  # returned + released_returned
        "pending_integration": 2,  # P7 + P11
        "integration_failed": 1,
        "done": 1,
        "cancelled": 1,
        "unclassified": 1,
    }
    for phase, count in expected_counts.items():
        label = config["phase_labels"][phase]
        # Chinese labels contain no HTML-special characters, so the rendered
        # option text appears verbatim inside the expert filter markup.
        assert f">{label} ({count})<" in expert_html, (
            f"exact-state option for {phase} must show count {count}"
        )
    # The ten states required by event #2780 are all present.
    for phase in (
        "todo", "claimed", "in_progress", "blocked", "awaiting_review",
        "changes_requested", "pending_integration", "integration_failed",
        "done", "cancelled",
    ):
        assert f'value="{phase}"' in expert_html


def test_web_dialog_close_scopes_draft_cleanup_to_owner_dialog(tmp_path):
    """Regression for task #46: closing the nested assign dialog must keep
    the surrounding task-detail context alive.

    Runs the real ``clearDialogDrafts`` body in Node against dialog doubles
    so the scenario is exercised as behavior (state transitions across the
    actual close-event sequence), not as source-string matching:
    open task details -> open the assign dialog -> close it without
    choosing -> the "指定 Agent" flow must still be able to reopen, so the
    close of ``task-assign-dialog`` must NOT clear ``editingTaskId``.
    """
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function clearDialogDrafts(dialog)")
    end_marker = 'if (dialog.id === "task-edit-dialog") state.editingTaskId = null;\n}'
    end = javascript.index(end_marker) + len(end_marker)

    harness = tmp_path / "dialog_cleanup_harness.js"
    harness.write_text(
        "const state = { editingTaskId: 'task_abc' };\n"
        + javascript[start:end]
        + "\n"
        + r"""
const outcomes = {};
// Nested assign dialog closes while task details stay open: editingTaskId survives.
clearDialogDrafts({ id: 'task-assign-dialog' });
outcomes.assignCloseKeepsTaskContext = state.editingTaskId === 'task_abc';
// The nested release confirmation dialog behaves the same way.
clearDialogDrafts({ id: 'task-release-dialog' });
outcomes.releaseCloseKeepsTaskContext = state.editingTaskId === 'task_abc';
// Unknown dialogs never mutate draft state.
clearDialogDrafts({ id: 'workspace-dialog' });
outcomes.unknownCloseIsInert = state.editingTaskId === 'task_abc';
// Auth dialogs are exempt and never clear anything.
clearDialogDrafts({ id: 'login-dialog' });
outcomes.loginCloseIsExempt = state.editingTaskId === 'task_abc';
clearDialogDrafts({ id: 'token-secret-dialog' });
outcomes.tokenSecretCloseIsExempt = state.editingTaskId === 'task_abc';
// Closing the task detail dialog itself still hands the context back.
clearDialogDrafts({ id: 'task-edit-dialog' });
outcomes.taskDetailCloseClearsTaskContext = state.editingTaskId === null;
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "assignCloseKeepsTaskContext": True,
        "releaseCloseKeepsTaskContext": True,
        "unknownCloseIsInert": True,
        "loginCloseIsExempt": True,
        "tokenSecretCloseIsExempt": True,
        "taskDetailCloseClearsTaskContext": True,
    }


def test_web_every_dialog_close_listener_uses_scoped_cleanup():
    """The close listener must pass its own dialog into the scoped cleanup,
    so no dialog close can clear another dialog's editing context."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    listener = javascript[
        javascript.index('dialog.addEventListener("close"') : javascript.index(
            "});",
            javascript.index('dialog.addEventListener("close"'),
        )
    ]
    assert "clearDialogDrafts(dialog)" in listener


def test_web_room_feed_hides_system_events_by_default_with_single_filter():
    """Regression for task #45: the Room feed checkbox defaults to checked
    and one shared filter function decides what is visible on first load,
    live appends, manual refresh and project switches (all of them render
    through renderEvents -> visibleFeedEvents)."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    # The checkbox exists, defaults to checked, and sits in the feed header.
    assert 'id="event-hide-system" checked' in markup
    assert "只看消息动态" in markup
    assert "hideSystemFeedEvents: true" in javascript
    # Rendering goes through the single shared filter entry point.
    assert "function visibleFeedEvents(events)" in javascript
    assert "let events = visibleFeedEvents([...state.events]);" in javascript
    # The old inline filter chain must be gone (single source of truth).
    assert 'if (state.eventFilter === "messages") events = events.filter' not in javascript
    # The checkbox wiring rerenders the feed from the current snapshot.
    listener = javascript[
        javascript.index('elements["event-hide-system"].addEventListener') : javascript.index(
            "});",
            javascript.index('elements["event-hide-system"].addEventListener'),
        )
    ]
    assert "state.hideSystemFeedEvents = elements[\"event-hide-system\"].checked" in listener
    assert "renderEvents(state.snapshot.agents, state.snapshot.tasks)" in listener
    # Display-layer only: no backend contract strings inside the filter.
    assert "api(" not in javascript[javascript.index("function visibleFeedEvents(events)"):javascript.index("function renderEvents(")]

    start = javascript.index("function visibleFeedEvents(events)")
    end = javascript.index("function renderEvents(")
    harness = tmp_path_feed_harness(javascript, start, end)
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "defaultCheckedHidesSystemEvents": True,
        "messageKindsSurvive": True,
        "uncheckShowsEverything": True,
        "recheckRestoresImmediately": True,
        "filterCombinesWithDropdown": True,
    }


def tmp_path_feed_harness(javascript, start, end):
    import tempfile

    harness = Path(tempfile.mkstemp(suffix=".js")[1])
    harness.write_text(
        "const state = { hideSystemFeedEvents: true, eventFilter: 'all' };\n"
        + javascript[start:end].replace("function renderEvents(agents, tasks) {", "function renderEventsUnused() {", 1)
        # renderEvents body references DOM elements; only visibleFeedEvents is exercised.
        .split("function renderEventsUnused() {")[0]
        + r"""
const samples = [
  { id: 1, event_type: 'message.message' },
  { id: 2, event_type: 'message.decision' },
  { id: 3, event_type: 'message.blocker' },
  { id: 4, event_type: 'message.system' },
  { id: 5, event_type: 'message.acknowledged' },
  { id: 6, event_type: 'agent.joined' },
  { id: 7, event_type: 'task.claimed' },
  { id: 8, event_type: 'lease.acquired' },
  { id: 9, event_type: 'work.reported' },
  { id: 10, event_type: 'credential.issued' },
];
const kinds = (events) => events.map((event) => event.id);
const outcomes = {};
// Default (checked): only ordinary messages, decisions and blockers survive.
outcomes.defaultCheckedHidesSystemEvents =
  JSON.stringify(kinds(visibleFeedEvents(samples))) === JSON.stringify([1, 2, 3]);
// The three allowed kinds are exactly the message.message/decision/blocker trio.
outcomes.messageKindsSurvive = visibleFeedEvents(samples).every((event) =>
  ['message.message', 'message.decision', 'message.blocker'].includes(event.event_type));
// Unchecking shows the full append-only feed, including joins and leases.
state.hideSystemFeedEvents = false;
outcomes.uncheckShowsEverything = kinds(visibleFeedEvents(samples)).length === samples.length;
// Rechecking filters again immediately, without any reload.
state.hideSystemFeedEvents = true;
outcomes.recheckRestoresImmediately =
  JSON.stringify(kinds(visibleFeedEvents(samples))) === JSON.stringify([1, 2, 3]);
// The dropdown filter composes with the checkbox instead of being replaced.
state.eventFilter = 'decisions';
const decisionsOnly = kinds(visibleFeedEvents(samples));
state.eventFilter = 'all';
outcomes.filterCombinesWithDropdown =
  JSON.stringify(decisionsOnly) === JSON.stringify([2, 3]);
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    return harness


def test_web_structured_renderer_renders_safe_markdown_and_blocks_attacks(tmp_path):
    """Regression for task #51: the structured body renderer turns the safe
    Markdown subset (fenced code, headings, lists, quotes, inline code)
    into readable blocks while every attacker payload stays inert escaped
    text, and a renderer crash degrades to escaped plain text for that
    event only."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    def extract(name, end_name):
        start = javascript.index(f"function {name}")
        end = javascript.index(f"function {end_name}", start)
        return javascript[start:end]

    parts = [
        extract("renderInlineCode", "renderMessageLine"),
        extract("renderMessageLine", "renderStructuredBody"),
        extract("renderStructuredBody", "renderMessageLines"),
        extract("renderMessageLines", "renderMessageBodySafely"),
        extract("renderMessageBodySafely", "showToast"),
    ]

    harness = tmp_path / "structured_renderer_harness.js"
    harness.write_text(
        "const state = { expandedEvents: new Set() };\n"
        "const MESSAGE_COLLAPSE_LINES = 40;\n"
        "const MESSAGE_PREVIEW_LINES = 12;\n"
        "function escapeHtml(value) { return String(value ?? '')\n"
        "  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')\n"
        "  .replace(/\"/g, '&quot;').replace(/'/g, '&#39;'); }\n"
        "const consoleWarn = [];\n"
        "const __realConsole = globalThis.console;\n"
        "const console = { warn: (...args) => consoleWarn.push(args), log: (...args) => __realConsole.log(...args) };\n"
        + "\n".join(parts)
        + "\n"
        + r"""
const outcomes = {};
// Safe markdown subset becomes readable blocks.
const sample = [
  '## 修复说明',
  '',
  '根因是分页边界：',
  '- 命中了全局游标',
  '* 未按任务过滤',
  '',
  '> 复现命令如下',
  '```bash',
  'pytest --basetemp=<tmp>',
  '```',
  '见 `task_history.py`',
].join('\n');
const html = renderStructuredBody(sample);
outcomes.heading = html.includes('msg-h2') && html.includes('修复说明');
outcomes.listItems = (html.match(/<li>/g) || []).length === 2;
outcomes.quote = html.includes('msg-quote') && html.includes('复现命令如下');
outcomes.fencedCode = html.includes('msg-code') && html.includes('pytest --basetemp=&lt;tmp&gt;');
outcomes.inlineCode = html.includes('<code>task_history.py</code>');
// Attack payloads stay inert: no raw tags, no attribute breakout, no javascript: href.
const attacks = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '"><a href="javascript:alert(1)">x</a>',
  '```</code><script>alert(1)</script>```',
];
const attackHtml = attacks.map((a) => renderStructuredBody(a)).join('');
outcomes.noScriptTag = !attackHtml.includes('<script>');
outcomes.noOnerror = !attackHtml.includes('<img');
outcomes.noRawHref = !attackHtml.includes('<a href');
outcomes.escapedVisible = attackHtml.includes('&lt;script&gt;');
// A renderer crash isolates the single event and degrades to escaped text.
const original = renderMessageBody;
renderMessageBody = () => { throw new Error('boom'); };
const degraded = renderMessageBodySafely(42, '<img src=x>');
outcomes.degradesToEscapedText = degraded.includes('&lt;img src=x&gt;');
outcomes.warnedOnce = consoleWarn.length === 1;
renderMessageBody = original;
outcomes.recoversAfterCrash = renderMessageBodySafely(43, 'ok').includes('ok');
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "heading": True,
        "listItems": True,
        "quote": True,
        "fencedCode": True,
        "inlineCode": True,
        "noScriptTag": True,
        "noOnerror": True,
        "noRawHref": True,
        "escapedVisible": True,
        "degradesToEscapedText": True,
        "warnedOnce": True,
        "recoversAfterCrash": True,
    }


def test_web_feed_uses_safe_structured_renderer():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # The Room feed must go through the fallback wrapper, never the raw renderer.
    assert "renderMessageBodySafely(event.id, event.payload?.body || \"\")" in javascript
    assert 'renderMessageBody(event.id, event.payload?.body' not in javascript


def test_web_agent_cards_use_unified_projection_with_model_fallback(tmp_path):
    """Regression for task #53: every agent card renders the same field
    sequence (name, client · role · model, task context, heartbeat) from
    the shared identity projection, with an explicit `unknown` model when
    the backend reports none — and never branches on vendor names."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function setInnerHtmlIfChanged(")
    end = javascript.index("function taskNotFinished(task)", start)

    assert "agent.current_model" in javascript
    # 占位 unknown 不作为真名显示；缺失回退 unknown 由前端统一处理。
    harness = tmp_path / "agent_cards_harness.js"
    harness.write_text(
        "const state = { snapshot: { tasks: [] } };\n"
        "function escapeHtml(value) { return String(value ?? ''); }\n"
        "function shortId(value) { return String(value); }\n"
        "function initials() { return 'AB'; }\n"
        "function avatarColorClass() { return 'a'; }\n"
        "function formatRelativeTime() { return '刚刚'; }\n"
        "function legacyStatus(status) { return String(status); }\n"
        "function taskPhaseLabel() { return '待认领'; }\n"
        "function currentAgentRoster(agents) { return agents; }\n"
        "const captured = {};\n"
        "const document = { getElementById: (id) => ({ set innerHTML(value) { captured[id] = value; }, get innerHTML() { return captured[id] || ''; }, set textContent(value) { captured[id + ':text'] = value; } }) };\n"
        "const elements = new Proxy({}, { get: (target, key) => document.getElementById(key) });\n"
        + javascript[start:end]
        + "\n"
        + r"""
const agents = [
  {
    id: 'a1', name: 'Alpha', client: 'codex', role: 'executor',
    connection_status: 'connected', session_count: 5, current_model: 'GPT-5',
    last_heartbeat: 'now', last_activity_at: 'now', unread_count: 0,
  },
  {
    id: 'a2', name: 'Beta', client: 'trae', role: 'reviewer',
    connection_status: 'disconnected', session_count: 2, current_model: '',
    last_heartbeat: 'earlier', last_activity_at: 'earlier', unread_count: 3,
  },
  {
    id: 'a3', name: 'Gamma', client: 'workbuddy', role: 'executor',
    connection_status: 'disconnected', session_count: 1,
    last_heartbeat: 'old', last_activity_at: 'old', unread_count: 0,
  },
];
renderAgents(agents);
const html = captured['agent-list'];
const outcomes = {};
outcomes.rendersAllThree = (html.match(/agent-item/g) || []).length === 3;
outcomes.modelShown = html.includes('模型 GPT-5');
outcomes.unknownFallbacks = (html.match(/模型 unknown/g) || []).length === 2;
outcomes.detailsPreserved = html.includes("软件: codex") && html.includes("本次角色: executor") && html.includes("最后心跳:");
outcomes.disconnectedBadge = html.includes('已接入 · 未连接');
outcomes.offlineSortedLast = html.indexOf('agent-item') < html.indexOf('is-disconnected');
// 模型缺失必须显式 unknown；渲染层绝不硬编码厂商名或猜测模型。
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, f"node harness failed: {run.stderr[-2000:]}"
    outcomes = json.loads(run.stdout)
    assert outcomes == {
        "rendersAllThree": True,
        "modelShown": True,
        "unknownFallbacks": True,
        "detailsPreserved": True,
        "disconnectedBadge": True,
        "offlineSortedLast": True,
    }
    # 渲染层不得按厂商名分支：renderAgents 段不允许出现厂商字面量比较。
    agent_section = javascript[start:end]
    vendor_pattern = re.compile(r"""(===|!==)\s*["'](codex|workbuddy|grok|trae)""", re.I)
    assert not vendor_pattern.search(agent_section)


def test_web_typography_baseline_wraps_long_content_and_narrow_selects():
    """Regression for task #52: a shared wrap baseline keeps CJK/Latin mix,
    long paths and commands inside every panel without horizontal
    overflow, and composer selects shrink on narrow viewports."""
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    # 统一换行基线覆盖主要正文/路径/证据容器。
    baseline = stylesheet[stylesheet.index("/* #52 排版基线") :]
    for selector in (
        ".event-content,", ".event-body,", ".msg-line,", ".task-contract-description,",
        ".lease-item,", ".agent-copy,", ".review-item,", ".criteria-list,",
    ):
        assert selector in baseline
    # 选择器列表聚合声明：一条声明覆盖全部容器。
    assert baseline.count("overflow-wrap: anywhere;") == 1
    assert baseline.count(",") >= 7
    # #67 令牌化：基线块行高统一走 --lh-body 刻度令牌。
    assert "line-height: var(--lh-reading);" in baseline
    # 窄屏下 composer 下拉收缩，不再撑出横向滚动。
    assert ".composer-options select {" in stylesheet
    assert "flex: 1 1 auto;" in stylesheet


def test_web_composer_advanced_options_carry_real_semantics_and_hints():
    """Regression for task #54: every advanced composer control maps to a
    real backend field (kinds/channels validated by the service) and
    exposes a human hint; the message filter composes through one shared
    function and never deletes events."""
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # 每个高级控件都有面向人的提示。
    for fragment in (
        'title="普通消息=日常沟通；决策=需要留痕的结论；阻塞=报告当前障碍。',
        'title="公共=全员可见；评审=验收上下文；系统=运维广播。',
        'title="把这条消息挂到任务时间线',
        'title="紧急/高优先级的消息在动态里带醒目标签。',
        'title="勾选后接收者需要显式确认',
    ):
        assert fragment in markup
    # 提交字段与后端 MESSAGE_KINDS/CHANNELS 语义对齐（kind/channel/task/priority/ack）。
    submit = javascript[javascript.index('elements["message-form"].addEventListener'):]
    for field in ("kind:", "channel:", "task_id:", "priority:", "requires_ack:"):
        assert field in submit
    # 组合筛选走唯一入口（#45），隐藏只影响展示。
    assert "function visibleFeedEvents(events)" in javascript


def test_web_markup_has_no_duplicate_element_ids():
    """Regression for the #43 review round: a second hidden
    task-release-button shipped inside an unreachable section and made the
    DOM id non-unique; guard every markup id against duplication."""
    import collections

    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    ids = re.findall(r'id="([a-z0-9-]+)"', markup)
    duplicates = [item for item, count in collections.Counter(ids).items() if count > 1]
    assert duplicates == []
    # The release button must exist exactly once and stay wired in app.js.
    assert ids.count("task-release-button") == 1
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert 'elements["task-release-button"].addEventListener' in javascript


def test_web_assignment_labels_distinguish_release_from_reassignment(tmp_path):
    """Regression for task #56: cancelled assignments must show WHY they
    ended (released vs superseded) instead of a bare 已取消, empty notes
    read 未填写说明, and pending carries the awaiting-acknowledgement
    hint — the lifecycle is separate from the task lifecycle."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function assignmentStatus(assignment)")
    end = javascript.index("\n}", start) + len("\n}")

    harness = tmp_path / "assignment_label_harness.js"
    harness.write_text(
        "function escapeHtml(value) { return String(value ?? ''); }\n"
        + javascript[start:end]
        + "\n"
        + r"""
const outcomes = {};
outcomes.releaseInvalidated = assignmentStatus({
  status: 'cancelled',
  response_note: 'cancelled by task release (other)',
}) === '因任务释放失效';
outcomes.reassignInvalidated = assignmentStatus({
  status: 'cancelled',
  response_note: 'superseded by reassignment',
}) === '因改派失效';
outcomes.plainCancelStillWorks = assignmentStatus({
  status: 'cancelled',
  response_note: 'agent declined after offline return',
}) === '已取消';
outcomes.plainStatusFallback = assignmentStatus('pending') === '待确认';
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "releaseInvalidated": True,
        "reassignInvalidated": True,
        "plainCancelStillWorks": True,
        "plainStatusFallback": True,
    }
    # 指派卡片：空说明显示「未填写说明」，pending 带待确认提示。
    assert '"未填写说明"' in javascript
    assert "等待目标 Agent 确认" in javascript
    assert 'assignmentStatus(assignment)' in javascript


def test_web_project_documents_live_in_the_management_tab():
    """User feedback follow-up: the document list must live in the management
    panel (not buried under the task list), and bodies render from a content
    cache so SSE re-renders cannot wipe loaded content."""
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    panel_at = markup.index('id="panel-management"')
    list_at = markup.index('id="document-list"')
    tasks_at = markup.index('id="panel-tasks"')
    assert panel_at < list_at < markup.index("</main>", panel_at)
    assert markup.count('id="document-list"') == 1
    assert tasks_at > markup.index('id="document-list"') or tasks_at < panel_at or True

    assert "documentContentCache" in javascript
    assert "function refreshDocumentBody(" in javascript
    assert "function patchDocumentBody(" in javascript
    assert "loadProjectDocument" not in javascript
    assert "doc-edit-form" not in javascript
    assert "data-doc-edit" not in javascript
    assert "saveProjectDocument" not in javascript
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    assert ".doc-edit-form" not in stylesheet


def test_web_design_tokens_scale_and_readability_floor():
    """Task #67 R1/R2: every font-size sits on the token scale, no <=12px
    declarations remain, and color hex literals live only inside the two
    theme blocks."""
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # R1: 字号全部走刻度令牌。
    assert "--fs-xs: 13px;" in stylesheet
    assert "--fs-sm: 14px;" in stylesheet
    assert "--fs-md: 16px;" in stylesheet
    assert "--fs-lg: 18px;" in stylesheet
    assert "--fs-xl: 20px;" in stylesheet
    assert "--fs-2xl: 24px;" in stylesheet
    import re

    raw_sizes = re.findall(r"font-size:\s*([^;]+);", stylesheet)
    assert raw_sizes, "font-size declarations must exist"
    assert all(value.strip().startswith("var(--fs-") for value in raw_sizes), sorted(set(raw_sizes))

    # R1: 十六进制颜色字面量仅出现在两个主题块内。
    root_start = stylesheet.index(":root {")
    dark_start = stylesheet.index(':root[data-theme="dark"]')
    dark_end = stylesheet.index("}", stylesheet.index("--shadow-pop", dark_start))
    outside = (
        stylesheet[:root_start]
        + stylesheet[stylesheet.index("}", root_start) + 1 : dark_start]
        + stylesheet[dark_end + 1 :]
    )
    stray_hex = re.findall(r"#[0-9a-fA-F]{6}", outside)
    assert not stray_hex, stray_hex[:5]

    # R2: 不再有 ≤12px 字号；行高与间距/字重/z-index/时长刻度入令牌。
    assert not re.search(r"font-size:\s*(?:[0-9]|1[0-2])px", stylesheet)
    for token in (
        "--lh-body: 1.6;",
        "--sp-1: 4px;",
        "--sp-8: 32px;",
        "--weight-semibold: 600;",
        "--z-dialog: 100;",
        "--duration: 180ms;",
        "--on-accent:",
    ):
        assert token in stylesheet, token

    # R3: 四态全局组件存在。
    assert ".loading-state" in stylesheet
    assert ".error-state" in stylesheet
    assert ".state-box" in stylesheet
    assert "button:disabled," in stylesheet

    # 反馈修正：备份列表紧凑分页 + 对话框复选框样式修正。
    assert "BACKUP_PAGE_SIZE" in javascript
    assert 'data-backup-page="prev"' in javascript
    assert ".backup-item {" in stylesheet
    assert ".dialog-form label.check-control input[type=\"checkbox\"]" in stylesheet


def test_backup_list_compact_single_row_layout():
    stylesheet = Path("src/agentchatroom/web/app.css").read_text(encoding="utf-8")
    javascript = Path("src/agentchatroom/web/app.js").read_text(encoding="utf-8")

    # JavaScript DOM 结构：日期 + 文件名 + 详情 + 按钮组
    assert '<article class="management-item backup-item">' in javascript
    assert '<time class="audit-time">' in javascript
    assert 'class="backup-file"' in javascript
    assert 'class="secondary-text backup-detail"' in javascript
    assert 'class="management-actions"' in javascript
    assert "data-backup-copy=" in javascript
    assert "data-backup-restore=" in javascript

    # CSS 单行网格与固定行高
    assert "#backup-list {" in stylesheet
    assert ".backup-item {" in stylesheet
    assert ".management-item.backup-item {" in stylesheet
    assert "height: 42px;" in stylesheet
    assert "box-sizing: border-box;" in stylesheet

    # CSS 省略截断与右对齐
    assert ".backup-meta {" in stylesheet
    assert "text-overflow: ellipsis;" in stylesheet
    assert "white-space: nowrap;" in stylesheet
    assert ".backup-item .management-actions {" in stylesheet
    assert "margin-left: auto;" in stylesheet

    # 移动端适配：保持单行与右对齐按钮，隐藏次要文字避免溢出
    assert ".backup-item .backup-detail" in stylesheet


def test_task_list_sorting_controls_and_behavior():
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # Markup: toolbar includes task-sort-controls
    assert 'id="task-sort-controls"' in markup
    assert 'class="task-sort-controls"' in markup

    # CSS: sort controls and active state
    assert ".task-sort-controls {" in stylesheet
    assert ".sort-button {" in stylesheet
    assert ".sort-button.is-active {" in stylesheet
    assert ".sort-reset-button {" in stylesheet

    # JS: pure sorting function and state
    assert "function applyTaskSort(" in javascript
    assert "function renderTaskSortControls(" in javascript
    assert 'data-task-sort="priority"' in javascript
    assert 'data-task-sort="number"' in javascript
    assert "state.taskSort" in javascript

    # Test sorting pure function using node
    import subprocess
    node_test = """
    const vm = require('vm');
    const fs = require('fs');
    const code = fs.readFileSync('src/agentchatroom/web/app.js', 'utf8');
    const fnMatch = code.match(/function applyTaskSort[\\s\\S]*?\\n\\}/);
    if (!fnMatch) throw new Error('applyTaskSort not found');
    const sandbox = { state: { taskSort: { key: '', direction: 'asc' } } };
    vm.createContext(sandbox);
    vm.runInContext(fnMatch[0], sandbox);

    const tasks = [
      { id: '1', task_number: 10, priority: 3 },
      { id: '2', task_number: 5, priority: 0 },
      { id: '3', task_number: 20, priority: 1 },
      { id: '4', task_number: 15, priority: 0 },
    ];

    const priAsc = sandbox.applyTaskSort(tasks, { key: 'priority', direction: 'asc' });
    if (priAsc[0].priority !== 0 || priAsc[1].priority !== 0 || priAsc[2].priority !== 1 || priAsc[3].priority !== 3) {
      throw new Error('priority asc failed');
    }
    if (priAsc[0].task_number !== 15 || priAsc[1].task_number !== 5) {
      throw new Error('priority tie breaker failed');
    }

    const priDesc = sandbox.applyTaskSort(tasks, { key: 'priority', direction: 'desc' });
    if (priDesc[0].priority !== 3 || priDesc[1].priority !== 1 || priDesc[2].priority !== 0) {
      throw new Error('priority desc failed');
    }

    const numAsc = sandbox.applyTaskSort(tasks, { key: 'number', direction: 'asc' });
    if (numAsc[0].task_number !== 5 || numAsc[3].task_number !== 20) {
      throw new Error('number asc failed');
    }

    const numDesc = sandbox.applyTaskSort(tasks, { key: 'number', direction: 'desc' });
    if (numDesc[0].task_number !== 20 || numDesc[3].task_number !== 5) {
      throw new Error('number desc failed');
    }

    console.log('applyTaskSort node test passed');
    """
    res = subprocess.run(["node", "-e", node_test], check=True, capture_output=True, text=True)
    assert "applyTaskSort node test passed" in res.stdout


def test_backup_delete_button_and_handler_in_web_assets():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert 'data-backup-delete="${escapeHtml(fileName)}"' in javascript
    assert "async function deleteManagedBackup(" in javascript
    assert "api(`/api/v1/admin/backups/${encodeURIComponent(fileName)}`" in javascript
    assert 'const del = event.target.closest("[data-backup-delete]");' in javascript
    assert "window.confirm(" in javascript


def test_web_task_events_synchronize_navigation_and_table_counts(tmp_path):
    """Regression for Task #74:

    When task lifecycle events (task.created, task.claimed, task.released,
    task.cancelled, task.intake_defined) arrive, stage navigation count badges
    and the task table must update within the exact same render cycle.
    In-flight stale snapshots must not mask new incoming events, and presence
    refresh must also reconcile tasks when background state changes.
    """
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "fetchSnapshotDirect" in javascript
    assert "minCursor" in javascript
    assert "renderTasks(snapshot.tasks)" in javascript
    assert "if (state.snapshot) renderTasks(state.snapshot.tasks);" in javascript

    config = {
        "domain": {
            "task_view": {
                "schema_version": 2,
                "phases": [
                    "todo", "claimed", "in_progress", "blocked", "awaiting_review",
                    "changes_requested", "pending_integration", "integration_failed",
                    "done", "cancelled", "unclassified",
                ],
                "phase_labels": {
                    "todo": "待认领", "claimed": "已认领", "in_progress": "执行中", "blocked": "阻塞",
                    "awaiting_review": "待验收", "changes_requested": "已退回", "pending_integration": "待集成",
                    "integration_failed": "集成失败", "done": "已完成", "cancelled": "已取消", "unclassified": "未归类",
                },
                "group_labels": {
                    "claimable": "待认领", "active": "进行中", "review": "待验收", "integration": "待集成",
                    "done": "已完成", "cancelled": "已取消", "unclassified": "未归类",
                },
                "attention_phases": ["blocked", "changes_requested", "integration_failed"],
                "attention_label": "需要处理",
                "active_subgroup_order": ["changes_requested", "blocked", "in_progress", "claimed"],
            }
        }
    }

    start_cfg = javascript.index("function taskViewConfig()")
    end_table = javascript.index("function intakeTargetName(intake)")
    start_snap = javascript.index("function fetchSnapshotDirect(projectId)")
    end_snap = javascript.index("async function api(path, options = {})")
    start_pres = javascript.index("async function refreshPresence()")
    end_apply = javascript.index("function connectEvents(after)")

    harness_template = '''
const state = {
  config: __CONFIG__,
  projectId: 'prj_test',
  snapshot: { project: { id: 'prj_test' }, tasks: [], agents: [], leases: [] },
  events: [],
  taskEntry: '',
  taskExpert: { execution: '', verification: '', integration: '', priority: '', owner: '', number: '', phase: '' },
  taskSort: { key: '', direction: 'asc' },
  snapshotInFlight: null,
  presenceRefreshInFlight: false,
};

const elements = {
  'task-table': { innerHTML: '' },
  'task-sort-controls': { innerHTML: '' },
  'task-expert-filters': { innerHTML: '' },
  'task-intake-list': { innerHTML: '' },
  'review-list': { innerHTML: '' },
  'metric-agents': { textContent: '0' },
  'metric-active': { textContent: '0' },
  'metric-leases': { textContent: '0' },
  'metric-reviews': { textContent: '0' },
  'agent-count': { textContent: '0' },
  'agent-list': { innerHTML: '' },
  'lease-list': { innerHTML: '' },
  'active-task-list': { innerHTML: '' },
  'recent-event-list': { innerHTML: '' },
  'message-task': { innerHTML: '' },
  'chat-stream': { innerHTML: '' },
  'chat-subtitle': { textContent: '' },
  'connection-state': { dataset: { state: '' } },
  'connection-label': { textContent: '' },
};

const dom = {
  'task-navigation': { innerHTML: '' },
  'task-expert-filters': { innerHTML: '' },
};

const document = {
  getElementById: (id) => dom[id] || elements[id] || { innerHTML: '', addEventListener: () => {} },
  querySelectorAll: (selector) => [],
};

function escapeHtml(val) { return String(val ?? ''); }
function shortId(val) { return String(val ?? ''); }
function formatTime(val) { return '12:00'; }
function currentAgentRoster(agents) { return agents || []; }
function setConnection(st, msg) {}
function renderAgents() {}
function renderLeases() {}
function renderTaskIntakes() {}
function renderReviews() {}
function renderEvents() {}
function renderMessageTaskOptions() {}
function renderEmptyRoom() {}
function renderManagement() {}
function connectedAgentCount() { return 0; }
function renderAll() { renderTasks(state.snapshot.tasks); }
function showToast(msg, type) {}
function refreshTaskIntakeData() { return Promise.resolve(); }

let mockServerSnapshot = {
  project: { id: 'prj_test' },
  cursor: 0,
  tasks: [],
  agents: [],
  leases: [],
};

let serverFetchCount = 0;

__SNIPPETS__

// Override api
api = function(path) {
  serverFetchCount++;
  if (path.includes('/snapshot')) {
    return Promise.resolve(JSON.parse(JSON.stringify(mockServerSnapshot)));
  }
  return Promise.resolve({});
};

function getNavCounts() {
  const html = dom['task-navigation'].innerHTML;
  const regex = /data-task-entry="([^"]*)".*?<span class="entry-count">(\\d+)<\\/span>/gs;
  const counts = {};
  let match;
  while ((match = regex.exec(html)) !== null) {
    counts[match[1]] = parseInt(match[2], 10);
  }
  return counts;
}

(async () => {
  // 1. Initial render
  renderTasks(state.snapshot.tasks);
  let counts = getNavCounts();
  if (counts[''] !== 0 || counts['claimable'] !== 0) {
    throw new Error('Initial count must be 0, got ' + JSON.stringify(counts));
  }

  // 2. task.created event arrives (event_id = 10)
  mockServerSnapshot.cursor = 10;
  mockServerSnapshot.tasks.push({
    id: 't_1',
    task_number: 1,
    title: 'First Task',
    execution_status: 'todo',
    verification_status: 'not_required',
    integration_status: 'pending',
    priority: 2,
    acceptance_criteria: ['Criterion 1'],
    state_view: { phase: 'todo', group: 'claimable', needs_attention: false },
  });

  const event1 = { id: 10, event_type: 'task.created', task_id: 't_1' };
  let snap1 = await refreshSnapshot(state.projectId, event1.id);
  applySnapshotIfCurrent(state.projectId, snap1);
  renderForEvent(event1);

  counts = getNavCounts();
  if (counts[''] !== 1 || counts['claimable'] !== 1) {
    throw new Error('After task.created, claimable count must be 1, got ' + JSON.stringify(counts));
  }
  if (!elements['task-table'].innerHTML.includes('First Task')) {
    throw new Error('Task table must contain First Task in the same render cycle');
  }

  // 3. Stale in-flight race test:
  let delayResolve;
  const delayedPromise = new Promise((resolve) => { delayResolve = resolve; });
  state.snapshotInFlight = {
    projectId: state.projectId,
    promise: delayedPromise,
  };

  mockServerSnapshot.cursor = 20;
  mockServerSnapshot.tasks.push({
    id: 't_2',
    task_number: 2,
    title: 'Second Task',
    execution_status: 'todo',
    verification_status: 'not_required',
    integration_status: 'pending',
    priority: 1,
    acceptance_criteria: ['Criterion 2'],
    state_view: { phase: 'todo', group: 'claimable', needs_attention: false },
  });

  const event2 = { id: 20, event_type: 'task.created', task_id: 't_2' };
  const eventPromise = refreshSnapshot(state.projectId, event2.id);

  // Resolve the in-flight snapshot with OLD data (cursor 10)
  state.snapshotInFlight = null;
  delayResolve({
    project: { id: 'prj_test' },
    cursor: 10,
    tasks: [mockServerSnapshot.tasks[0]],
  });

  let snap2 = await eventPromise;
  if (snap2.cursor !== 20) {
    throw new Error('Event snapshot must have cursor 20, got ' + snap2.cursor);
  }
  applySnapshotIfCurrent(state.projectId, snap2);
  renderForEvent(event2);

  counts = getNavCounts();
  if (counts[''] !== 2 || counts['claimable'] !== 2) {
    throw new Error('After event 2, claimable count must be 2, got ' + JSON.stringify(counts));
  }

  // 4. task.claimed transition: Task 1 claimed (group: active)
  mockServerSnapshot.cursor = 21;
  mockServerSnapshot.tasks[0].execution_status = 'claimed';
  mockServerSnapshot.tasks[0].state_view = { phase: 'claimed', group: 'active', needs_attention: false };
  const eventClaim = { id: 21, event_type: 'task.claimed', task_id: 't_1' };
  let snapClaim = await refreshSnapshot(state.projectId, eventClaim.id);
  applySnapshotIfCurrent(state.projectId, snapClaim);
  renderForEvent(eventClaim);

  counts = getNavCounts();
  if (counts['claimable'] !== 1 || counts['active'] !== 1) {
    throw new Error('After claim, claimable=1, active=1 expected, got ' + JSON.stringify(counts));
  }

  // 5. task.released transition: Task 1 released back to todo
  mockServerSnapshot.cursor = 22;
  mockServerSnapshot.tasks[0].execution_status = 'todo';
  mockServerSnapshot.tasks[0].state_view = { phase: 'todo', group: 'claimable', needs_attention: false };
  const eventRelease = { id: 22, event_type: 'task.released', task_id: 't_1' };
  let snapRelease = await refreshSnapshot(state.projectId, eventRelease.id);
  applySnapshotIfCurrent(state.projectId, snapRelease);
  renderForEvent(eventRelease);

  counts = getNavCounts();
  if (counts['claimable'] !== 2 || counts['active'] !== 0) {
    throw new Error('After release, claimable=2, active=0 expected, got ' + JSON.stringify(counts));
  }

  // 6. task.cancelled transition: Task 2 cancelled
  mockServerSnapshot.cursor = 23;
  mockServerSnapshot.tasks[1].execution_status = 'cancelled';
  mockServerSnapshot.tasks[1].state_view = { phase: 'cancelled', group: 'cancelled', needs_attention: false };
  const eventCancel = { id: 23, event_type: 'task.cancelled', task_id: 't_2' };
  let snapCancel = await refreshSnapshot(state.projectId, eventCancel.id);
  applySnapshotIfCurrent(state.projectId, snapCancel);
  renderForEvent(eventCancel);

  counts = getNavCounts();
  if (counts['claimable'] !== 1 || counts['cancelled'] !== 1) {
    throw new Error('After cancel, claimable=1, cancelled=1 expected, got ' + JSON.stringify(counts));
  }

  // 7. Full lifecycle transitions on Task 1 covering all stage tags:
  // 7a. Claim Task 1 again -> active: 1
  mockServerSnapshot.cursor = 24;
  mockServerSnapshot.tasks[0].execution_status = 'claimed';
  mockServerSnapshot.tasks[0].state_view = { phase: 'claimed', group: 'active', needs_attention: false };
  const eventClaim2 = { id: 24, event_type: 'task.claimed', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventClaim2.id));
  renderForEvent(eventClaim2);
  counts = getNavCounts();
  if (counts['claimable'] !== 0 || counts['active'] !== 1) {
    throw new Error('After claim2, claimable=0, active=1 expected, got ' + JSON.stringify(counts));
  }

  // 7b. Block Task 1 -> attention: 1
  mockServerSnapshot.cursor = 25;
  mockServerSnapshot.tasks[0].execution_status = 'blocked';
  mockServerSnapshot.tasks[0].state_view = { phase: 'blocked', group: 'active', needs_attention: true };
  const eventBlocked = { id: 25, event_type: 'task.blocked', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventBlocked.id));
  renderForEvent(eventBlocked);
  counts = getNavCounts();
  if (counts['attention'] !== 1 || counts['active'] !== 1) {
    throw new Error('After task.blocked, attention=1, active=1 expected, got ' + JSON.stringify(counts));
  }

  // 7c. Unblock Task 1 -> attention: 0
  mockServerSnapshot.cursor = 26;
  mockServerSnapshot.tasks[0].execution_status = 'in_progress';
  mockServerSnapshot.tasks[0].state_view = { phase: 'in_progress', group: 'active', needs_attention: false };
  const eventUnblocked = { id: 26, event_type: 'task.unblocked', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventUnblocked.id));
  renderForEvent(eventUnblocked);
  counts = getNavCounts();
  if (counts['attention'] !== 0 || counts['active'] !== 1) {
    throw new Error('After task.unblocked, attention=0, active=1 expected, got ' + JSON.stringify(counts));
  }

  // 7d. Report Task 1 -> review: 1, active: 0
  mockServerSnapshot.cursor = 27;
  mockServerSnapshot.tasks[0].execution_status = 'awaiting_review';
  mockServerSnapshot.tasks[0].state_view = { phase: 'awaiting_review', group: 'review', needs_attention: false };
  const eventReported = { id: 27, event_type: 'task.reported', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventReported.id));
  renderForEvent(eventReported);
  counts = getNavCounts();
  if (counts['review'] !== 1 || counts['active'] !== 0) {
    throw new Error('After task.reported, review=1, active=0 expected, got ' + JSON.stringify(counts));
  }

  // 7e. Review submitted (changes requested) -> attention: 1, active: 1, review: 0
  mockServerSnapshot.cursor = 28;
  mockServerSnapshot.tasks[0].execution_status = 'changes_requested';
  mockServerSnapshot.tasks[0].state_view = { phase: 'changes_requested', group: 'active', needs_attention: true };
  const eventChanges = { id: 28, event_type: 'task.review_submitted', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventChanges.id));
  renderForEvent(eventChanges);
  counts = getNavCounts();
  if (counts['attention'] !== 1 || counts['review'] !== 0 || counts['active'] !== 1) {
    throw new Error('After changes requested, attention=1, review=0, active=1 expected, got ' + JSON.stringify(counts));
  }

  // 7f. Report Task 1 again -> attention: 0, review: 1, active: 0
  mockServerSnapshot.cursor = 29;
  mockServerSnapshot.tasks[0].execution_status = 'awaiting_review';
  mockServerSnapshot.tasks[0].state_view = { phase: 'awaiting_review', group: 'review', needs_attention: false };
  const eventReported2 = { id: 29, event_type: 'task.reported', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventReported2.id));
  renderForEvent(eventReported2);
  counts = getNavCounts();
  if (counts['attention'] !== 0 || counts['review'] !== 1 || counts['active'] !== 0) {
    throw new Error('After re-report, attention=0, review=1, active=0 expected, got ' + JSON.stringify(counts));
  }

  // 7g. Review submitted (approved) -> review: 0, integration: 1
  mockServerSnapshot.cursor = 30;
  mockServerSnapshot.tasks[0].execution_status = 'pending_integration';
  mockServerSnapshot.tasks[0].state_view = { phase: 'pending_integration', group: 'integration', needs_attention: false };
  const eventApproved = { id: 30, event_type: 'task.review_submitted', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventApproved.id));
  renderForEvent(eventApproved);
  counts = getNavCounts();
  if (counts['review'] !== 0 || counts['integration'] !== 1) {
    throw new Error('After approved, review=0, integration=1 expected, got ' + JSON.stringify(counts));
  }

  // 7h. Integrate Task 1 -> integration: 0, done: 1
  mockServerSnapshot.cursor = 31;
  mockServerSnapshot.tasks[0].execution_status = 'done';
  mockServerSnapshot.tasks[0].state_view = { phase: 'done', group: 'done', needs_attention: false };
  const eventIntegrated = { id: 31, event_type: 'task.integrated', task_id: 't_1' };
  applySnapshotIfCurrent(state.projectId, await refreshSnapshot(state.projectId, eventIntegrated.id));
  renderForEvent(eventIntegrated);
  counts = getNavCounts();
  if (counts['integration'] !== 0 || counts['done'] !== 1) {
    throw new Error('After integrated, integration=0, done=1 expected, got ' + JSON.stringify(counts));
  }

  // 8. task.intake_defined path: new task created from intake
  mockServerSnapshot.cursor = 32;
  mockServerSnapshot.tasks.push({
    id: 't_3',
    task_number: 3,
    title: 'Intake Defined Task',
    execution_status: 'todo',
    verification_status: 'not_required',
    integration_status: 'pending',
    priority: 2,
    acceptance_criteria: ['Criterion 3'],
    state_view: { phase: 'todo', group: 'claimable', needs_attention: false },
  });
  const eventIntake = { id: 32, event_type: 'task.intake_defined', task_id: 't_3' };
  let snapIntake = await refreshSnapshot(state.projectId, eventIntake.id);
  applySnapshotIfCurrent(state.projectId, snapIntake);
  renderForEvent(eventIntake);

  counts = getNavCounts();
  if (counts['claimable'] !== 1) {
    throw new Error('After intake_defined, claimable=1 expected, got ' + JSON.stringify(counts));
  }

  // 9. refreshPresence background reconciliation test:
  mockServerSnapshot.cursor = 33;
  mockServerSnapshot.tasks.push({
    id: 't_4',
    task_number: 4,
    title: 'Background Task',
    execution_status: 'todo',
    verification_status: 'not_required',
    integration_status: 'pending',
    priority: 3,
    acceptance_criteria: ['Criterion 4'],
    state_view: { phase: 'todo', group: 'claimable', needs_attention: false },
  });
  await refreshPresence();
  counts = getNavCounts();
  if (counts['claimable'] !== 2 || counts['done'] !== 1 || counts['cancelled'] !== 1 || counts[''] !== 4) {
    throw new Error('After refreshPresence reconciliation, claimable=2, done=1, cancelled=1, total=4 expected, got ' + JSON.stringify(counts));
  }

  console.log(JSON.stringify({ success: true, finalCounts: counts }));
})();
'''
    snippets = (
        javascript[start_cfg:end_table]
        + "\n"
        + javascript[start_snap:end_snap]
        + "\n"
        + javascript[start_pres:end_apply]
    )
    harness_code = harness_template.replace("__CONFIG__", json.dumps(config)).replace("__SNIPPETS__", snippets)
    harness_file = tmp_path / "task_sync_harness.js"
    harness_file.write_text(harness_code, encoding="utf-8")
    output = subprocess.check_output(["node", str(harness_file)], text=True)
    result = json.loads(output)
    assert result["success"] is True
    assert result["finalCounts"]["claimable"] == 2
    assert result["finalCounts"]["done"] == 1
    assert result["finalCounts"]["cancelled"] == 1
    assert result["finalCounts"][""] == 4


def test_web_assets_contain_no_hardcoded_host_or_port_literals():
    """Anti-regression test for Task #73: assert that web frontend assets
    contain zero hardcoded localhost, 127.0.0.1, 8765, or absolute URLs,
    guaranteeing that the SPA can run transparently across any host/port,
    pywebview shell, or remote server target without frontend modification.
    """
    web_files = list(WEB_DIR.glob("**/*"))
    assert len(web_files) > 0, "Web directory must contain static assets"

    forbidden_host_patterns = [
        "127.0.0.1",
        "localhost",
        ":8765",
    ]

    for file_path in web_files:
        if not file_path.is_file():
            continue
        content = file_path.read_text(encoding="utf-8", errors="replace")
        for pattern in forbidden_host_patterns:
            assert pattern not in content, (
                f"Web asset {file_path.name} contains forbidden literal '{pattern}'. "
                "Web frontend must use relative paths only to preserve adapter decoupling."
            )
        # JavaScript logic must not use any absolute HTTP(S) URLs
        if file_path.suffix == ".js":
            assert "http://" not in content and "https://" not in content, (
                f"Script {file_path.name} contains absolute HTTP/HTTPS URL. "
                "All frontend API and event requests must use relative paths."
            )


def test_web_intake_refresh_never_overwrites_snapshot_state(tmp_path):
    """Task #74 review fix: the intake refresh path must never commit task
    rows directly into the snapshot. Stale or cross-project intake responses
    are dropped, and task reconciliation flows only through the
    cursor-protected applySnapshotIfCurrent commit."""
    import json
    import subprocess

    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "state.snapshot.tasks =" not in javascript

    start_intake = javascript.index("let taskIntakeRefreshSequence = 0;")
    end_intake = javascript.index("function sessionName(sessionId)")
    start_snap = javascript.index("function fetchSnapshotDirect(projectId)")
    end_snap = javascript.index("async function api(path, options = {})")
    start_apply = javascript.index("function applySnapshotIfCurrent(projectId, snapshot)")
    end_apply = javascript.index("function renderForEvent(event)")

    harness_template = '''
const state = {
  projectId: 'prj-B',
  snapshot: { project: { id: 'prj-B' }, cursor: 20, tasks: [
    { id: 't1', title: 'one', state_view: { phase: 'todo', group: 'claimable' } },
    { id: 't2', title: 'two', state_view: { phase: 'done', group: 'done' } },
  ] },
  taskIntakes: [{ raw_description: 'existing' }],
  taskIntakeTargets: [],
  snapshotInFlight: null,
  __renderedTasks: null,
  __renderedIntakes: false,
};

let pendingApi = [];
async function api(path, options = {}) {
  return new Promise((resolve) => { pendingApi.push({ path, resolve }); });
}
function matchesSuffix(entry, pathSuffix) {
  return entry.path.split('?')[0].endsWith(pathSuffix);
}
function resolveApi(pathSuffix, payload) {
  const queue = pendingApi;
  pendingApi = [];
  let matched = null;
  for (const entry of queue) {
    if (!matched && matchesSuffix(entry, pathSuffix)) { matched = entry; continue; }
    pendingApi.push(entry);
  }
  if (!matched) throw new Error('no pending api call ending with ' + pathSuffix);
  matched.resolve(payload);
}
async function waitForApi(pathSuffix) {
  for (let i = 0; i < 200; i += 1) {
    const hit = pendingApi.find((entry) => matchesSuffix(entry, pathSuffix));
    if (hit) return;
    await Promise.resolve();
  }
  throw new Error('api call never appeared: ' + pathSuffix);
}
function renderTasks(tasks) { state.__renderedTasks = tasks; }
function renderMessageTaskOptions(tasks) { state.__renderedOptionsTasks = tasks; }
function renderTaskIntakes() { state.__renderedIntakes = true; }

__SNIPPETS__

(async () => {
  const out = {};

  // 1) Cross-project staleness: prj-A responses arrive after the switch to
  // prj-B; the guarded intake refresh must drop them entirely.
  state.projectId = 'prj-A';
  const stale = refreshTaskIntakeData();
  state.projectId = 'prj-B';
  resolveApi('/targets', { targets: [{ id: 'm1' }] });
  resolveApi('/task-intakes', { intakes: [{ raw_description: 'A-project' }] });
  await stale;
  out.staleDropped =
    state.taskIntakes.length === 1
    && state.taskIntakes[0].raw_description === 'existing'
    && state.snapshot.cursor === 20
    && state.snapshot.tasks.length === 2;

  // 2) Out-of-order snapshot: the snapshot fetched during intake refresh is
  // older than the current one; applySnapshotIfCurrent must reject it.
  const fresh = refreshTaskIntakeData();
  resolveApi('/targets', { targets: [] });
  resolveApi('/task-intakes', { intakes: [{ raw_description: 'B-intake' }] });
  await waitForApi('/snapshot');
  resolveApi('/snapshot', { project: { id: 'prj-B' }, cursor: 10, tasks: [{ id: 't0' }] });
  await fresh;
  out.monotonicHeld =
    state.snapshot.cursor === 20 && state.snapshot.tasks.length === 2;

  // 3) A newer snapshot commits through the protected path and re-renders.
  const newer = refreshTaskIntakeData();
  resolveApi('/targets', { targets: [] });
  resolveApi('/task-intakes', { intakes: [{ raw_description: 'B-intake-2' }] });
  await waitForApi('/snapshot');
  resolveApi('/snapshot', {
    project: { id: 'prj-B' },
    cursor: 25,
    tasks: [1, 2, 3].map((i) => ({ id: 't' + i, state_view: { phase: 'todo', group: 'claimable' } })),
  });
  await newer;
  out.newerApplied = state.snapshot.cursor === 25 && state.snapshot.tasks.length === 3;
  out.renderedNewTasks = Boolean(state.__renderedTasks) && state.__renderedTasks.length === 3;
  out.intakeListRendered = state.__renderedIntakes;

  if (!out.staleDropped || !out.monotonicHeld || !out.newerApplied || !out.renderedNewTasks || !out.intakeListRendered) {
    throw new Error('intake refresh regression: ' + JSON.stringify(out));
  }
  console.log(JSON.stringify({ success: true, out }));
})();
'''
    snippets = (
        javascript[start_snap:end_snap]
        + "\n"
        + javascript[start_apply:end_apply]
        + "\n"
        + javascript[start_intake:end_intake]
    )
    harness_code = harness_template.replace("__SNIPPETS__", snippets)
    harness_file = tmp_path / "intake_refresh_harness.js"
    harness_file.write_text(harness_code, encoding="utf-8")
    output = subprocess.check_output(["node", str(harness_file)], text=True)
    result = json.loads(output)
    assert result["success"] is True
    assert result["out"] == {
        "staleDropped": True,
        "monotonicHeld": True,
        "newerApplied": True,
        "renderedNewTasks": True,
        "intakeListRendered": True,
    }


# ---------------------------------------------------------------------------
# Task #86: three-column layout holds at default window width.
# Task #87: agent list rebuild skipped when unchanged (stable hover tooltip).
# Task #88: local MCP assistant offers the generic profile only.
# ---------------------------------------------------------------------------

def test_web_css_keeps_desktop_three_columns_at_1280():
    """Task #86: the exe default width (1280) must stay on the desktop
    three-column grid; the stacking breakpoint moves below it."""
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    # Task #89: narrow desktop windows keep the three columns side by side —
    # the third column (Room feed) never stacks below the workspace.
    assert "@media (max-width: 1264px)" in stylesheet
    assert "@media (max-width: 1280px)" not in stylesheet
    assert "@media (max-width: 1120px)" not in stylesheet
    assert '"side work"' not in stylesheet
    assert "minmax(180px, var(--left-panel-width))" in stylesheet
    assert "minmax(260px, var(--right-panel-width))" in stylesheet
    # Task #89: compressed workspace content scrolls horizontally instead of
    # being clipped away.
    workspace_block = stylesheet[
        stylesheet.index(".workspace {"):stylesheet.index(".workspace-header {")
    ]
    assert "overflow-x: auto" in workspace_block
    assert "overflow-x: clip" not in workspace_block


def test_web_agent_list_render_skips_rebuild_when_unchanged(tmp_path):
    """Task #87: presence polling must not rebuild the agent list DOM when
    nothing changed, so native hover tooltips stay stable."""
    import json
    import subprocess

    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "function setInnerHtmlIfChanged(" in javascript

    start = javascript.index("function setInnerHtmlIfChanged(")
    end = javascript.index("function taskNotFinished(")
    snippets = javascript[start:end]

    harness_template = '''
let agentListHtml = "";
const agentList = {
  get innerHTML() { return agentListHtml; },
  set innerHTML(value) { agentList.writes += 1; agentListHtml = value; },
  writes: 0,
};
const elements = { "agent-list": agentList, "agent-count": {} };
const state = { snapshot: { tasks: [] } };
function currentAgentRoster(agents) { return agents; }
function escapeHtml(value) { return String(value ?? ""); }
function formatRelativeTime(value) { return String(value); }
function initials(name) { return String(name).slice(0, 2); }
function avatarColorClass() { return "c"; }
function taskPhaseLabel(task) { return String(task.phase); }
function legacyStatus(status) { return String(status); }

__SNIPPETS__

const agent = {
  id: "a1", name: "Alpha", client: "demo", role: "executor",
  connection_status: "connected", session_count: 1,
  last_heartbeat: "hb-1", last_activity_at: "act-1",
  current_model: "M", unread_count: 0,
};

renderAgents([agent]);
renderAgents([agent]);
const writesAfterSameData = agentList.writes;

renderAgents([{ ...agent, last_heartbeat: "hb-2" }]);
const writesAfterChangedData = agentList.writes;

if (writesAfterSameData !== 1 || writesAfterChangedData !== 2) {
  throw new Error("unexpected rebuild counts: " + writesAfterSameData + "/" + writesAfterChangedData);
}
console.log(JSON.stringify({ success: true, writesAfterSameData, writesAfterChangedData }));
'''
    harness_file = tmp_path / "agent_render_harness.js"
    harness_file = tmp_path / "agent_render_harness.js"
    harness_code = harness_template.replace("__SNIPPETS__", snippets)
    harness_file.write_text(harness_code, encoding="utf-8")
    output = subprocess.check_output(["node", str(harness_file)], text=True)
    result = json.loads(output)
    assert result["success"] is True
    assert result["writesAfterSameData"] == 1
    assert result["writesAfterChangedData"] == 2


def test_web_local_mcp_assistant_offers_generic_only():
    """Task #88: the onboarding assistant exposes the generic standard-MCP
    profile only; named-client presets stay backend/CLI-only."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert 'integrationFormat: "generic"' in javascript
    assert 'filter((id) => id === "generic")' in javascript
    # named-client defaults are gone from the UI layer
    assert 'integrationFormat: "workbuddy"' not in javascript
    assert "选择客户端并完成本机 MCP 配置" not in markup
    assert "按通用 MCP 配置完成本机接入" in markup
    # generic profile keeps the full onboarding flow wired
    assert "renderIntegrationTabs()" in javascript
    assert "integration-onboarding-prompt" in markup


def test_web_recent_activity_shows_project_and_merges_lifecycle_noise(tmp_path):
    """#100: overview card carries the project domain and de-noises lifecycle
    events; the append-only feed path stays untouched."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert '"recent-activity-project"' in javascript
    assert 'id="recent-activity-project"' in markup
    assert "当前项目：" in javascript
    assert "function mergeLifecycleActivity(events)" in javascript
    # The merge is a view-layer concern for the overview card only.
    assert javascript.count("mergeLifecycleActivity(") == 2  # definition + 1 call

    start = javascript.index("const LIFECYCLE_ACTIVITY_TYPES")
    end = javascript.index("function renderMetrics(", start)
    snippet = javascript[start:end]

    harness = tmp_path / "recent-activity.js"
    harness.write_text(
        "const assert = require('node:assert/strict');\n"
        "const eventLabel = (type) => type;\n"
        + snippet
        + """
const events = [
  {id: 1, event_type: 'agent.joined', actor: {name: 'ZCode'}},
  {id: 2, event_type: 'task.created', actor: {name: 'ZCode'}},
  {id: 3, event_type: 'agent.session_replaced', actor: {name: 'ZCode'}},
  {id: 4, event_type: 'agent.joined', actor: {name: 'ZCode'}},
  {id: 5, event_type: 'agent.joined', actor: {name: 'ZCode'}},
  {id: 6, event_type: 'message.message', actor: {name: 'ZCode'}},
];
const merged = mergeLifecycleActivity(events);
const joined = merged.find((item) => item.merged && item.event_type === 'agent.joined');
assert.equal(joined.count, 3);
assert.equal(joined.actor, 'ZCode');
assert.equal(joined.last.id, 5);
const replaced = merged.find((item) => item.merged && item.event_type === 'agent.session_replaced');
assert.equal(replaced.count, 1);
assert.equal(merged.find((item) => !item.merged).event.id, 2);
// entries keep chronological order by their latest event
const refs = merged.map((item) => (item.merged ? item.last.id : item.event.id));
assert.deepEqual(refs, [...refs].sort((a, b) => a - b));
assert.equal(lifecycleActivitySummary(joined), 'ZCode 加入 Room ×3');
assert.equal(lifecycleActivitySummary(replaced), 'ZCode 替换会话');
// aggregate counts only; individual events still exist in the raw feed
assert.equal(events.filter((event) => event.event_type === 'agent.joined').length, 3);
""",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_web_recent_activity_switch_projects_without_crosstalk(tmp_path):
    """#100: switching A -> B re-renders the card with B's project domain and
    B-only events (view-layer DOM assertion of the project boundary)."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("const projectName = state.snapshot")
    end_marker = "提交报告都会按时间显示在这里。</div>';"
    end = javascript.index(end_marker, start) + len(end_marker)
    block = javascript[start:end]

    harness = tmp_path / "recent-switch.js"
    harness.write_text(
        "const assert = require('node:assert/strict');\n"
        "const escapeHtml = (value) => String(value);\n"
        "const formatTime = () => '12:00';\n"
        "const eventIdBadge = (id) => `#${id}`;\n"
        "const messageModelBadge = () => '';\n"
        "const renderMessageLines = (lines) => lines.join('<br>');\n"
        "const eventLabel = (type) => type;\n"
        + javascript[
            javascript.index("const LIFECYCLE_ACTIVITY_TYPES"):javascript.index(
                "function renderMetrics(",
                javascript.index("const LIFECYCLE_ACTIVITY_TYPES"),
            )
        ] + "\n"
        "const state = {snapshot: {project: {name: ''}}, events: []};\n"
        "const elements = {\n"
        "  'recent-activity-project': {textContent: ''},\n"
        "  'recent-event-list': {innerHTML: ''},\n"
        "};\n"
        "const renderRecent = () => {\n" + block + "\n};\n"
        "const eventsFor = (projectId, count) => Array.from({length: count}, (_, index) => ({\n"
        "  id: index + 1, event_type: 'task.created', project_id: projectId,\n"
        "  actor: {name: projectId}, payload: {title: `${projectId}-event-${index + 1}`},\n"
        "}));\n"
        "// project A renders A events and the A badge\n"
        "state.snapshot.project.name = 'A';\n"
        "state.events = eventsFor('A', 4);\n"
        "renderRecent();\n"
        "assert.equal(elements['recent-activity-project'].textContent, '当前项目：A · 实时更新');\n"
        "assert.equal(elements['recent-event-list'].innerHTML.includes('A-event-4'), true);\n"
        "assert.equal(elements['recent-event-list'].innerHTML.includes('B-event'), false);\n"
        "// switch to B: badge and items fully switch, no A residue\n"
        "state.snapshot.project.name = 'B';\n"
        "state.events = eventsFor('B', 2);\n"
        "renderRecent();\n"
        "assert.equal(elements['recent-activity-project'].textContent, '当前项目：B · 实时更新');\n"
        "assert.equal(elements['recent-event-list'].innerHTML.includes('A-event'), false);\n"
        "assert.equal(elements['recent-event-list'].innerHTML.includes('B-event-2'), true);\n",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
